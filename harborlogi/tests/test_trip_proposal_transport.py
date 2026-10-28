"""D-908 / API v1.329: Harbor proposes once, never admits, reconciles by the original key."""
from pathlib import Path
import tempfile
import unittest
from urllib.parse import unquote

from harbor.rosy_http import FleetIdentityError, RosyClient
from harbor.transport import TransportLedger, TripIntent

PRINCIPAL = "harbor-service"


class ProposalFleet:
    """Fake Fleet: records every call; ``admit()`` is the named operator's act, outside Harbor."""

    def __init__(self, principal=PRINCIPAL):
        self.calls, self.proposals, self.jobs = [], {}, {}
        self.principal, self.lose_response, self.job_status = principal, False, "READY"

    def __call__(self, method, path, body):
        if path == "/api/fleet/session":
            return {"principal_id": self.principal, "role": "service"}
        self.calls.append((method, path))
        if (method, path) == ("POST", "/api/fleet/trip-proposals"):
            key = body["request_key"]
            if key not in self.proposals:
                intent = TripIntent(key, tuple(body["candidate_robot_ids"]), body["steps"][0]["to"], tuple(body["steps"][0]["via"]))
                self.proposals[key] = {"proposal_id": f"p-{len(self.proposals) + 1}", "proposal_principal_id": self.principal,
                    "request_key": key, "request_digest": intent.digest, "proposal_digest": "a" * 64,
                    "candidate_robot_ids": body["candidate_robot_ids"], "steps": body["steps"], "status": "PROPOSED",
                    "dispatch_generation": None, "approved_by": None, "mission_id": None}
            if self.lose_response:
                raise TimeoutError("response lost after commit")
            return {"proposal": self.proposals[key]}
        if path.startswith("/api/fleet/trip-proposals?request_key="):
            return {"proposal": self.proposals[unquote(path.split("=", 1)[1])]}
        if path.startswith("/api/fleet/trip-jobs?request_key="):
            return {"job": self.jobs[unquote(path.split("=", 1)[1])]}
        raise AssertionError(f"unexpected call {method} {path}")

    def admit(self, key, approver="operator-1"):
        proposal = self.proposals[key]
        proposal.update(status="ADMITTED", dispatch_generation=1, approved_by=approver, mission_id="trip-1")
        self.jobs[key] = {"mission_id": "trip-1", "request_key": key, "request_digest": proposal["request_digest"],
            "robot_id": proposal["candidate_robot_ids"][0], "status": self.job_status, "current_step_index": 0,
            "steps": [{"step_index": 0, "step": proposal["steps"][0], "status": self.job_status,
                       "action_id": None, "attempt_id": None, "reason": None}]}

    def posted(self):
        return [call for call in self.calls if call[0] == "POST"]


def client_for(fleet):
    client = RosyClient(transport=fleet)
    client.expected_principal = PRINCIPAL
    client.expected_role = "service"
    return client


class TripProposalTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "business.sqlite3"
        self.ledger, self.fleet = TransportLedger(self.path), ProposalFleet()
        self.client = client_for(self.fleet)
        self.intent = TripIntent("harbor-trip:1", ("robot-1",), "dest")
        self.ledger.prepare_proposal(self.intent)

    def test_propose_then_wait_for_operator_then_read_admitted_job(self):
        self.assertEqual(self.ledger.propose(self.intent.request_key, self.client)["state"], "PROPOSED")
        self.assertEqual(self.ledger.reconcile_proposal(self.intent.request_key, self.client)["state"], "PROPOSED")
        self.fleet.admit(self.intent.request_key)
        row = self.ledger.reconcile_proposal(self.intent.request_key, self.client)
        self.assertEqual((row["state"], row["mission_id"], row["job_status"]), ("ADMITTED", "trip-1", "READY"))
        self.fleet.job_status = "GOAL_CONFIRMED"
        job = self.fleet.jobs[self.intent.request_key]
        job.update(status="GOAL_CONFIRMED", current_step_index=1)
        job["steps"][0].update(status="GOAL_CONFIRMED", action_id="a1", attempt_id="a1")
        self.assertEqual(self.ledger.reconcile_proposal(self.intent.request_key, self.client)["state"], "GOAL_CONFIRMED")
        self.assertEqual(len(self.fleet.posted()), 1)
        self.assertFalse(any("admit" in path for _, path in self.fleet.calls))

    def test_client_has_no_admit_call(self):
        self.assertFalse([name for name in dir(RosyClient) if "admit" in name])

    def test_lost_response_is_reconciled_by_the_same_key_never_resent(self):
        self.fleet.lose_response = True
        with self.assertRaises(TimeoutError):
            self.ledger.propose(self.intent.request_key, self.client)
        self.assertEqual(self.ledger.read_proposal(self.intent.request_key)["state"], "UNCERTAIN")
        with self.assertRaises(ValueError):
            self.ledger.propose(self.intent.request_key, self.client)
        self.assertEqual(self.ledger.reconcile_proposal(self.intent.request_key, self.client)["state"], "PROPOSED")
        self.assertEqual(len(self.fleet.posted()), 1)
        self.assertEqual(list(self.fleet.proposals), [self.intent.request_key])

    def test_restart_during_submission_becomes_uncertain(self):
        self.ledger._set_proposal(self.ledger.read_proposal(self.intent.request_key), "SUBMITTING")
        self.assertEqual(TransportLedger(self.path).read_proposal(self.intent.request_key)["state"], "UNCERTAIN")

    def test_principal_mismatch_is_refused_and_leaves_uncertain(self):
        fleet = ProposalFleet(principal="someone-else")
        with self.assertRaises(FleetIdentityError):
            self.ledger.propose(self.intent.request_key, client_for(fleet))
        self.assertEqual(fleet.posted(), [])
        self.assertEqual(self.ledger.read_proposal(self.intent.request_key)["state"], "UNCERTAIN")

    def test_proposal_principal_in_body_must_match(self):
        self.ledger.propose(self.intent.request_key, self.client)
        self.fleet.proposals[self.intent.request_key]["proposal_principal_id"] = "other"
        with self.assertRaises(ValueError):
            self.ledger.reconcile_proposal(self.intent.request_key, self.client)

    def test_self_admitted_or_different_body_is_refused(self):
        self.ledger.propose(self.intent.request_key, self.client)
        self.fleet.admit(self.intent.request_key, approver=PRINCIPAL)
        with self.assertRaises(ValueError):
            self.ledger.reconcile_proposal(self.intent.request_key, self.client)
        self.fleet.proposals[self.intent.request_key].update(approved_by="operator-1", steps=[{"to": "x", "via": []}])
        with self.assertRaises(ValueError):
            self.ledger.reconcile_proposal(self.intent.request_key, self.client)

    def test_same_key_with_other_contents_conflicts(self):
        with self.assertRaises(ValueError):
            self.ledger.prepare_proposal(TripIntent("harbor-trip:1", ("robot-1",), "elsewhere"))

    def test_wire_body_has_no_cargo_fields(self):
        sent = []
        client = RosyClient(transport=lambda m, p, b: sent.append(b) or self.fleet(m, p, b))
        client.trip_propose(("robot-1",), self.intent.steps, request_key="k")
        self.assertEqual(set(sent[-1]), {"candidate_robot_ids", "request_key", "steps"})


if __name__ == "__main__":
    unittest.main()
