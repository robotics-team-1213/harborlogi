from dataclasses import replace
from datetime import timedelta
import json
from pathlib import Path
import tempfile
import unittest

from harbor.process import ProcessBinding, bind_plan
from harbor.rosy_http import RosyClient
from harbor.workflow import ArmRetreat, BusinessLedger, MoveEvidence, PlacementEvidence, RetreatEvidence
from test_policy import NOW, yard, constraints
from harbor.model import OutboundRequest
from harbor.outbound import plan_outbound


SOURCES = {"placement_sources": ("installed-camera",), "retreat_sources": ("fleet-retreat",)}


def move_arms():
    plan = plan_outbound(yard(), OutboundRequest("outbound", 3), constraints(), now=NOW)
    return tuple((m.primary_arm, *m.alternate_arms) for m in plan.moves)


def synthetic_evidence(evidence_id, mission, cargo, location, *, attempt="attempt-1", revision=3, when=NOW,
                       arms=("A", "B"), placement=None, retreat=None, arm=None):
    """Synthetic camera placement + platform retreat facts; no producer exists yet (D-856 P1)."""
    facts = tuple(ArmRetreat(a, "zone-" + a, True, when, 4, True) for a in arms)
    if arm:
        facts = (replace(facts[0], **arm),) + facts[1:]
    return MoveEvidence(
        replace(PlacementEvidence(evidence_id + "-p", mission, attempt, revision, "installed-camera", when,
                                  cargo, location), **(placement or {})),
        replace(RetreatEvidence(evidence_id + "-r", mission, attempt, revision, "fleet-retreat", when, facts),
                **(retreat or {})))


def bound_moves():
    plan = plan_outbound(yard(), OutboundRequest("outbound", 3), constraints(), now=NOW)
    bindings = tuple(ProcessBinding(m.cargo_id, m.source, m.destination, "recipe", "a" * 64,
                                   "cell", "b" * 64, "workcell", "installed", "c" * 64) for m in plan.moves)
    return bind_plan(plan, bindings)


class FakeFleet:
    def __init__(self):
        self.calls = []
        self.attempt = "attempt-1"
        self.status = "GOAL_CONFIRMED"
        self.fail_post = False
        self.mission = None

    def __call__(self, method, path, body):
        self.calls.append((method, path, body))
        if path.endswith("/compile"):
            return {"candidate": {"kind": "cell_job", "recipe_sha256": "a" * 64, "cell_sha256": "b" * 64},
                    "process_artifact_digest": "c" * 64, "summary": {"transfer_count": 1}}
        if path.endswith("/proposals"):
            if self.fail_post:
                raise TimeoutError("response lost")
            self.mission = f"mission-{sum(p.endswith('/proposals') for _, p, _ in self.calls)}"
            return {"proposal": {"proposal_id": self.mission, "state": "RESOLVED", "request_key": body["request_key"]},
                    "mission": {"mission_id": self.mission, "status": "PROPOSED", "process_artifact_digest": "c" * 64}}
        if method == "GET":
            return {"job": {"mission_id": self.mission, "status": self.status,
                            "steps": [{"attempt_id": self.attempt}]}}
        return {"job": {"mission_id": "trip-1"}}


class ProcessTests(unittest.TestCase):
    def test_every_move_is_bound_without_partial_fallback(self):
        moves = bound_moves()
        self.assertEqual([m.cargo_id for m in moves], ["rehandle", "outbound", "rehandle"])
        plan = plan_outbound(yard(), OutboundRequest("outbound", 3), constraints(), now=NOW)
        with self.assertRaises(ValueError):
            bind_plan(plan, (moves[0],))

    def test_invalid_document_digest_is_rejected(self):
        with self.assertRaises(ValueError):
            replace(bound_moves()[0], recipe_digest="changed")

    def test_compile_validates_revision_and_single_step(self):
        for response in [
            {"candidate": {"kind": "cell_job", "recipe_sha256": "d" * 64, "cell_sha256": "b" * 64},
             "process_artifact_digest": "c" * 64, "summary": {"transfer_count": 1}},
            {"candidate": {"kind": "cell_job", "recipe_sha256": "a" * 64, "cell_sha256": "b" * 64},
             "process_artifact_digest": "c" * 64, "summary": {"transfer_count": 2}},
        ]:
            with self.subTest(response=response), self.assertRaises(ValueError):
                RosyClient(transport=lambda *args: response).compile(bound_moves()[0])

    def test_candidate_request_uses_key_and_pool_without_platform_imports(self):
        fleet = FakeFleet()
        RosyClient(transport=fleet).trip(("p2", "p3"), ({"to": "stand"},), request_key="trip-key")
        self.assertEqual(fleet.calls[-1][2], {"candidate_robot_ids": ["p2", "p3"],
                                              "request_key": "trip-key", "steps": [{"to": "stand"}]})


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "business.sqlite"
        self.ledger = BusinessLedger(self.path, **SOURCES)
        self.fleet = FakeFleet()
        self.client = RosyClient(transport=self.fleet)
        self.prepare()

    def prepare(self, revision=3):
        return self.ledger.prepare("request-1", revision, bound_moves(), outbound_id="outbound", outbound_stand="stand",
                                   move_arms=move_arms())

    def evidence(self, **changes):
        move = bound_moves()[self.ledger.read("request-1")["step_index"]]
        return synthetic_evidence(changes.pop("evidence_id", "evidence-1"), self.ledger.read("request-1")["mission_id"],
                                  move.cargo_id, move.destination, **changes)

    def start(self):
        self.ledger.propose("request-1", self.client)
        self.ledger.refresh("request-1", self.client, now=NOW)

    def test_acceptance_and_action_success_do_not_confirm_a_move(self):
        self.ledger.propose("request-1", self.client)
        self.assertEqual(self.ledger.read("request-1")["state"], "WAITING")
        self.fleet.status = "ACTION_SUCCEEDED"
        self.ledger.refresh("request-1", self.client, now=NOW)
        with self.assertRaises(ValueError):
            self.ledger.confirm("request-1", self.evidence(), now=NOW)
        self.assertEqual(self.ledger.read("request-1")["step_index"], 0)

    def test_wrong_attempt_revision_source_cargo_destination_or_time_never_advances(self):
        self.start()
        for changes in ({"attempt": "old"}, {"revision": 2}, {"placement": {"source_id": "planner"}},
                        {"placement": {"cargo_id": "wrong"}}, {"placement": {"location": "wrong"}},
                        {"when": NOW - timedelta(seconds=6)}, {"when": NOW + timedelta(seconds=1)}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.ledger.confirm("request-1", self.evidence(**changes), now=NOW)
        self.assertEqual(self.ledger.read("request-1")["step_index"], 0)

    def test_placement_and_retreat_are_separate_bound_facts(self):
        self.start()
        later = NOW + timedelta(seconds=1)
        for changes in ({"placement": {"attempt_id": "old"}}, {"retreat": {"attempt_id": "old"}},
                        {"retreat": {"mission_id": "other"}}, {"retreat": {"revision": 2}},
                        # Each fact must come from its own kind of source.
                        {"placement": {"source_id": "fleet-retreat"}}, {"retreat": {"source_id": "installed-camera"}},
                        {"retreat": {"observed_at": NOW - timedelta(seconds=6)}},
                        {"arm": {"claim_released": False}}, {"arm": {"outside_zone": False}},
                        {"arm": {"pose_observed_at": NOW - timedelta(seconds=6)}},
                        {"arm": {"pose_observed_at": later}},
                        {"arms": ()}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.ledger.confirm("request-1", self.evidence(**changes), now=NOW)
        with self.assertRaises(ValueError):
            self.ledger.confirm("request-1", self.evidence().placement, now=NOW)
        self.assertEqual(self.ledger.read("request-1")["step_index"], 0)
        # Every arm that acted must show retreat, including a second one Fleet reports.
        both = self.evidence(arms=("A", "B"))
        stuck = replace(both.retreat, arms=(both.retreat.arms[0], replace(both.retreat.arms[1], outside_zone=False)))
        with self.assertRaises(ValueError):
            self.ledger.confirm("request-1", replace(both, retreat=stuck), now=NOW)
        # Fleet switched to fallback arm B (ADR 0029, D3): only the arm that acted must show retreat.
        self.ledger.confirm("request-1", self.evidence(arms=("B",)), now=NOW)
        self.assertEqual(self.ledger.read("request-1")["step_index"], 1)

    def test_retreat_facts_reject_unlabelled_arm_and_implicit_verdicts(self):
        for changes in ({"arm": ""}, {"claim_released": 1}, {"outside_zone": None},
                        {"pose_observed_at": NOW.replace(tzinfo=None)}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(ArmRetreat("A", "zone-A", True, NOW, 4, True), **changes)
        with self.assertRaises(TypeError):  # joint values are no longer Harbor data (ADR 0029)
            ArmRetreat("A", "zone-A", True, (0.0,), NOW, 4, True)
        with self.assertRaises(ValueError):
            BusinessLedger(self.path, placement_sources=("same",), retreat_sources=("same",))

    def test_each_move_records_its_yard_arms(self):
        with self.assertRaises(ValueError):
            self.ledger.prepare("request-2", 3, bound_moves(), outbound_id="outbound", outbound_stand="stand",
                                move_arms=(("A",), ("S",), ("A",)))
        with self.assertRaises(ValueError):
            self.ledger.prepare("request-2", 3, bound_moves(), outbound_id="outbound", outbound_stand="stand",
                                move_arms=(("A",),))

    def test_one_stage_confirmation_allows_only_the_next_proposal(self):
        self.start()
        self.ledger.confirm("request-1", self.evidence(), now=NOW)
        row = self.ledger.read("request-1")
        self.assertEqual((row["state"], row["step_index"]), ("PLANNED", 1))
        self.assertIsNone(row["mission_id"])
        self.assertEqual(sum(p.endswith("/proposals") for _, p, _ in self.fleet.calls), 1)

    def test_duplicate_request_and_conflicting_contents(self):
        self.prepare()
        with self.assertRaises(ValueError):
            self.prepare(revision=4)
        self.ledger.propose("request-1", self.client)
        with self.assertRaises(ValueError):
            self.ledger.propose("request-1", self.client)
        self.assertEqual(sum(p.endswith("/proposals") for _, p, _ in self.fleet.calls), 1)

    def test_uncertain_post_is_durable_hold_without_retry(self):
        self.fleet.fail_post = True
        with self.assertRaises(TimeoutError):
            self.ledger.propose("request-1", self.client)
        restarted = BusinessLedger(self.path, **SOURCES)
        self.assertEqual(restarted.read("request-1")["state"], "HOLD")
        with self.assertRaises(ValueError):
            restarted.propose("request-1", self.client)

    def test_restart_requires_fresh_fleet_reconciliation(self):
        self.start()
        restarted = BusinessLedger(self.path, **SOURCES)
        with self.assertRaises(ValueError):
            restarted.confirm("request-1", self.evidence(), now=NOW)
        restarted.refresh("request-1", self.client, now=NOW)
        restarted.confirm("request-1", self.evidence(), now=NOW)

    def test_duplicate_evidence_cannot_confirm_a_later_move(self):
        self.start()
        self.ledger.confirm("request-1", self.evidence(), now=NOW)
        self.start()
        with self.assertRaises(ValueError):
            self.ledger.confirm("request-1", self.evidence(), now=NOW)
        self.assertEqual(self.ledger.read("request-1")["step_index"], 1)

    def test_stale_fleet_reconciliation_cannot_advance(self):
        self.start()
        now = NOW + timedelta(seconds=6)
        with self.assertRaises(ValueError):
            self.ledger.confirm("request-1", self.evidence(when=now), now=now)

    def test_late_post_response_does_not_undo_restart_hold(self):
        fleet = self.fleet
        def transport(method, path, body):
            if path.endswith("/proposals"):
                BusinessLedger(self.path, **SOURCES)
            return fleet(method, path, body)
        with self.assertRaises(ValueError):
            self.ledger.propose("request-1", RosyClient(transport=transport))
        row = self.ledger.read("request-1")
        self.assertEqual(row["state"], "HOLD")
        self.assertEqual(row["mission_id"], "mission-1")

    def test_compiler_artifact_change_is_not_silently_accepted(self):
        fleet = self.fleet
        def transport(method, path, body):
            result = fleet(method, path, body)
            if path.endswith("/proposals"):
                result["mission"]["process_artifact_digest"] = "d" * 64
            return result
        with self.assertRaises(ValueError):
            self.ledger.propose("request-1", RosyClient(transport=transport))
        self.assertEqual(self.ledger.read("request-1")["state"], "HOLD")

    def test_all_moves_still_require_outbound_placement_before_business_completion(self):
        for index in range(3):
            self.start()
            self.ledger.confirm("request-1", self.evidence(evidence_id=f"e{index}"), now=NOW)
        self.assertEqual(self.ledger.read("request-1")["state"], "AWAITING_FINAL_EVIDENCE")
        placement = synthetic_evidence("final", "mission-2", "outbound", "stand")
        for wrong in (synthetic_evidence("final", "mission-2", "outbound", "temp"),
                      synthetic_evidence("final", "mission-2", "outbound", "stand", attempt="old"),
                      synthetic_evidence("final", "mission-3", "outbound", "stand"),
                      # Every arm Fleet reports for the outbound attempt must have retreated (ADR 0029, D3).
                      synthetic_evidence("final", "mission-2", "outbound", "stand", arm={"outside_zone": False}),
                      synthetic_evidence("final", "mission-2", "outbound", "stand", arms=())):
            with self.assertRaises(ValueError):
                self.ledger.finish("request-1", wrong, now=NOW)
        self.ledger.finish("request-1", placement, now=NOW)
        self.assertEqual(self.ledger.read("request-1")["state"], "COMPLETED")

    def test_race_late_refresh_cannot_overwrite_next_stage(self):
        self.start()
        evidence = self.evidence()
        def transport(method, path, body):
            result = self.fleet(method, path, body)
            self.ledger.confirm("request-1", evidence, now=NOW)
            return result
        with self.assertRaises(ValueError):
            self.ledger.refresh("request-1", RosyClient(transport=transport), now=NOW)
        row = self.ledger.read("request-1")
        self.assertEqual((row["state"], row["step_index"], row["mission_id"]), ("PLANNED", 1, None))

    def test_race_late_refresh_cannot_overwrite_completed_business(self):
        for index in range(2):
            self.start()
            self.ledger.confirm("request-1", self.evidence(evidence_id=f"e{index}"), now=NOW)
        self.start()
        evidence = self.evidence(evidence_id="e2")
        placement = synthetic_evidence("final", "mission-2", "outbound", "stand")
        def transport(method, path, body):
            result = self.fleet(method, path, body)
            self.ledger.confirm("request-1", evidence, now=NOW)
            self.ledger.finish("request-1", placement, now=NOW)
            return result
        with self.assertRaises(ValueError):
            self.ledger.refresh("request-1", RosyClient(transport=transport), now=NOW)
        self.assertEqual(self.ledger.read("request-1")["state"], "COMPLETED")

    def test_race_delayed_compile_never_submits_a_previous_stage(self):
        def transport(method, path, body):
            result = self.fleet(method, path, body)
            self.start()
            self.ledger.confirm("request-1", self.evidence(), now=NOW)
            return result
        with self.assertRaises(ValueError):
            self.ledger.propose("request-1", RosyClient(transport=transport))
        row = self.ledger.read("request-1")
        self.assertEqual((row["state"], row["step_index"], row["mission_id"]), ("PLANNED", 1, None))
        self.assertEqual(sum(p.endswith("/proposals") for _, p, _ in self.fleet.calls), 1)

    def test_race_old_refresh_cannot_replace_another_readers_new_attempt(self):
        self.start()
        other = BusinessLedger(self.path, **SOURCES)
        def transport(method, path, body):
            result = self.fleet(method, path, body)
            self.fleet.attempt, self.fleet.status = "attempt-2", "RUNNING"
            other.refresh("request-1", self.client, now=NOW)
            return result
        with self.assertRaises(ValueError):
            self.ledger.refresh("request-1", RosyClient(transport=transport), now=NOW)
        self.assertEqual(self.ledger.read("request-1")["attempt_id"], "attempt-2")
        self.assertEqual(self.ledger.read("request-1")["goal_confirmed"], 0)


if __name__ == "__main__":
    unittest.main()
