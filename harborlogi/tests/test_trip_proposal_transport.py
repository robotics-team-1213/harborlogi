"""D-908 / API v1.329: Harbor proposes once, never admits, reconciles by the original key."""
from hashlib import sha256
from pathlib import Path
import tempfile
import unittest
from urllib.error import HTTPError
from urllib.parse import unquote

from harbor.rosy_http import RosyClient
from harbor.transport import TransportLedger, TripIntent

PRINCIPAL = "harbor-service"


class ProposalFleet:
    """Fake Fleet; admit() is the named operator's act, outside Harbor."""

    def __init__(self):
        self.calls, self.proposals, self.jobs = [], {}, {}
        self.lose_response, self.unavailable = False, False

    def __call__(self, method, path, body):
        if path == "/api/fleet/session":
            return {"principal_id": PRINCIPAL, "role": "service"}
        self.calls.append((method, path))
        if self.unavailable:
            raise HTTPError(path, 503, "TRIP_PROPOSAL_UNAVAILABLE", {}, None)
        if (method, path) == ("POST", "/api/fleet/trip-proposals"):
            key = body["request_key"]
            if key not in self.proposals:
                step = body["steps"][0]
                digest = TripIntent(key, tuple(body["candidate_robot_ids"]), step["to"], tuple(step["via"]),
                                    body.get("object_ref", "")).digest
                self.proposals[key] = {"proposal_id": "p-1", "proposal_principal_id": PRINCIPAL, "request_key": key,
                    "request_digest": digest, "proposal_digest": "a" * 64, "status": "PROPOSED",
                    "candidate_robot_ids": body["candidate_robot_ids"], "steps": body["steps"],
                    "dispatch_generation": None, "approved_by": None, "mission_id": None,
                    **({"object_ref": body["object_ref"]} if "object_ref" in body else {})}
                self.bodies = getattr(self, "bodies", []) + [body]
            if self.lose_response:
                raise TimeoutError("response lost after commit")
            return {"proposal": self.proposals[key]}
        if path.startswith("/api/fleet/trip-proposals?request_key="):
            return {"proposal": self.proposals[unquote(path.split("=", 1)[1])]}
        if path.startswith("/api/fleet/trip-jobs?request_key="):
            return {"job": self.jobs[unquote(path.split("=", 1)[1])]}
        raise AssertionError(f"unexpected call {method} {path}")

    def admit(self, key, approver="operator-1", mission_id="trip-1"):
        proposal = self.proposals[key]
        proposal.update(status="ADMITTED", dispatch_generation=1, approved_by=approver, mission_id=mission_id)
        self.jobs[key] = {"mission_id": mission_id, "request_key": key, "request_digest": proposal["request_digest"],
            "robot_id": proposal["candidate_robot_ids"][0], "status": "READY", "current_step_index": 0,
            "steps": [{"step_index": 0, "step": proposal["steps"][0], "status": "READY",
                       "action_id": None, "attempt_id": None, "reason": None}]}

    def posted(self):
        return [call for call in self.calls if call[0] == "POST"]


class TripProposalTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.ledger, self.fleet = TransportLedger(Path(directory.name) / "b.sqlite3"), ProposalFleet()
        self.client = RosyClient(transport=self.fleet)
        self.client.expected_principal, self.client.expected_role = PRINCIPAL, "service"
        self.intent = TripIntent("harbor-trip:1", ("robot-1",), "dest")
        self.key = self.intent.request_key
        self.ledger.prepare(self.intent)

    def test_propose_waits_for_operator_then_links_the_job(self):
        self.ledger.propose(self.key, self.client)
        row = self.ledger.read(self.key)
        self.assertEqual((row["state"], row["mission_id"]), ("AWAITING_APPROVAL", None))
        self.ledger.reconcile_proposal(self.key, self.client)
        self.assertEqual(self.ledger.read(self.key)["state"], "AWAITING_APPROVAL")
        self.fleet.admit(self.key)
        self.ledger.reconcile_proposal(self.key, self.client)
        row = self.ledger.read(self.key)
        self.assertEqual((row["state"], row["mission_id"]), ("ACKNOWLEDGED", "trip-1"))
        self.assertEqual(len(self.fleet.posted()), 1)
        self.assertFalse([name for name in dir(RosyClient) if "admit" in name])

    def test_lost_response_is_reconciled_by_the_same_key_never_resent(self):
        self.fleet.lose_response = True
        with self.assertRaises(TimeoutError):
            self.ledger.propose(self.key, self.client)
        self.assertEqual(self.ledger.read(self.key)["state"], "UNCERTAIN")
        with self.assertRaises(ValueError):
            self.ledger.propose(self.key, self.client)
        self.ledger.reconcile_proposal(self.key, self.client)
        self.assertEqual(self.ledger.read(self.key)["state"], "AWAITING_APPROVAL")
        self.assertEqual(len(self.fleet.posted()), 1)

    def test_mismatched_proposals_are_refused(self):
        self.ledger.propose(self.key, self.client)
        proposal = self.fleet.proposals[self.key]
        for field, value in (("steps", [{"to": "x", "via": []}]), ("candidate_robot_ids", ["robot-2"]),
                             ("request_digest", "b" * 64), ("proposal_principal_id", "other")):
            with self.subTest(field=field):
                changed = dict(proposal, **{field: value})
                self.fleet.proposals[self.key] = changed
                with self.assertRaises(ValueError):
                    self.ledger.reconcile_proposal(self.key, self.client)
        self.fleet.proposals[self.key] = proposal

    def test_self_approval_and_other_job_are_refused(self):
        self.ledger.propose(self.key, self.client)
        self.fleet.admit(self.key, approver=PRINCIPAL)
        with self.assertRaises(ValueError):
            self.ledger.reconcile_proposal(self.key, self.client)
        self.fleet.admit(self.key)
        self.fleet.jobs[self.key]["mission_id"] = "trip-2"
        with self.assertRaises(ValueError):
            self.ledger.reconcile_proposal(self.key, self.client)

    def test_not_installed_is_503_and_leaves_uncertain(self):
        self.fleet.unavailable = True
        with self.assertRaises(HTTPError) as caught:
            self.ledger.propose(self.key, self.client)
        self.assertEqual(caught.exception.code, 503)
        self.assertEqual(self.ledger.read(self.key)["state"], "UNCERTAIN")

    def test_wire_body_has_no_cargo_fields(self):
        sent = []
        RosyClient(transport=lambda m, p, b: sent.append(b)).trip_propose(("robot-1",), self.intent.steps, request_key="k")
        self.assertEqual(set(sent[0]), {"candidate_robot_ids", "request_key", "steps"})

    def test_declared_cargo_rides_the_proposal_unread(self):
        intent = TripIntent("harbor-trip:2", ("robot-1",), "dest", object_ref="C-1")
        self.ledger.prepare(intent)
        self.ledger.propose(intent.request_key, self.client)
        self.assertEqual(self.fleet.bodies[-1]["object_ref"], "C-1")
        # object_ref joins the digest only when declared (ROSY 161957814a); without it the bytes are unchanged.
        self.assertNotEqual(intent.digest, TripIntent("x", ("robot-1",), "dest").digest)
        self.assertEqual(TripIntent("x", ("robot-1",), "dest").digest, sha256(
            b'{"candidates":["robot-1"],"steps":[{"to":"dest","via":[]}]}').hexdigest())
        self.assertEqual(TripIntent("k", ("rosy_01",), "B", object_ref="CNTR-0001").digest, sha256(
            b'{"candidates":["rosy_01"],"object_ref":"CNTR-0001","steps":[{"to":"B","via":[]}]}').hexdigest())
        self.assertEqual(self.ledger._intent(self.ledger.read(intent.request_key)).object_ref, "C-1")
        with self.assertRaises(ValueError):  # the same key cannot later declare another cargo
            self.ledger.prepare(TripIntent("harbor-trip:2", ("robot-1",), "dest", object_ref="C-2"))
        self.fleet.proposals[intent.request_key]["object_ref"] = "C-2"
        with self.assertRaises(ValueError):  # an echo naming another cargo is refused
            self.ledger.reconcile_proposal(intent.request_key, self.client)
        with self.assertRaises(ValueError):
            TripIntent("harbor-trip:3", ("robot-1",), "dest", object_ref=" C-1")


if __name__ == "__main__":
    unittest.main()
