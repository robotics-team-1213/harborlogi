from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import tempfile
import unittest

from harbor import process
from harbor.rosy_http import RosyClient
from harbor.workflow import BusinessLedger
from test_execution import SOURCES, synthetic_evidence
from test_policy import NOW


def binding():
    return process.ArmTransferBinding(cargo_id="outbound", source="yard", destination="stand",
        workcell_id="workcell", instance_id="installed", object_ref="physical-object",
        source_ref="source", target_ref="target")


class Fleet:
    def __init__(self):
        self.calls, self.job = [], None
        self.status = "PROPOSED"
        self.receipt_changes = {}

    def __call__(self, method, path, body):
        self.calls.append((method, path, body))
        if method == "POST":
            transfer = {k: v for k, v in body.items() if k != "request_key"}
            self.job = dict(mission_id="physical-1", action_kind="ARM_TRANSFER", proposal_principal_id="harbor",
                request_key=body["request_key"], request_digest=sha256(json.dumps(transfer, sort_keys=True,
                    separators=(",", ":")).encode()).hexdigest(), proposal_digest="b" * 64,
                transfer=transfer, resource_policy_digest="c" * 64, authority_epoch=2, dispatch_generation=3,
                approved_by="operator", reason=None)
        if path.endswith("/physical-receipt"):
            return {"receipt": {**dict(receipt_id="receipt-1", mission_id="physical-1", action_id="action-1",
                attempt_id="attempt-1", request_digest="e" * 64, workcell_id="workcell", instance_id="installed",
                policy_digest="c" * 64, evidence_digest="d" * 64, object_ref="physical-object", source="source",
                target="target", authority_epoch=2, dispatch_generation=3, validated_at_ns=2_000_000_000,
                physical_status="AT_TARGET_AND_CLEAR"), **self.receipt_changes}}
        return {"job": {**deepcopy(self.job), "status": self.status}}


class ArmExecutionTests(unittest.TestCase):
    def prepare(self, tmp):
        ledger = BusinessLedger(Path(tmp) / "business.sqlite3", **SOURCES)
        ledger.prepare("outbound", 3, (binding(),), outbound_id="outbound", outbound_stand="stand",
                       move_arms=(("A", "B"),))
        return ledger

    def test_submission_returns_the_same_bound_snapshot_as_the_saved_mission(self):
        fleet = Fleet()
        with tempfile.TemporaryDirectory() as tmp:
            ledger = self.prepare(tmp)
            result = ledger.submit('outbound', RosyClient(transport=fleet), principal='harbor')
            self.assertEqual(result.mission_id, ledger.read('outbound')['mission_id'])
            self.assertEqual(result.request_key, 'outbound:0')
            self.assertEqual(result.phase, 'awaiting_approval')
            self.assertIsNone(result.attempt_id)
            self.assertFalse(result.goal_confirmed)
            self.assertEqual(result.job['mission_id'], result.mission_id)
            self.assertEqual([(method, path) for method, path, _ in fleet.calls], [
                ('POST', '/api/fleet/arm-transfers/jobs'),
                ('GET', '/api/fleet/arm-transfers/jobs?request_key=outbound%3A0')])

    def test_confirmed_submission_requires_a_matching_receipt_without_reposting(self):
        fleet = Fleet()
        fleet.status = 'GOAL_CONFIRMED'
        fleet.receipt_changes = {'target': 'different-target'}
        with tempfile.TemporaryDirectory() as tmp:
            ledger = self.prepare(tmp)
            with self.assertRaises(ValueError):
                ledger.submit('outbound', RosyClient(transport=fleet), principal='harbor')
            self.assertEqual(ledger.read('outbound')['state'], 'HOLD')
            with self.assertRaises(ValueError):
                ledger.submit('outbound', RosyClient(transport=fleet), principal='harbor')
            self.assertEqual(sum(method == 'POST' for method, _, _ in fleet.calls), 1)

    def test_neutral_http_binding_keeps_business_completion_and_evidence_in_harbor(self):
        fleet = Fleet()
        with tempfile.TemporaryDirectory() as tmp:
            ledger, client = self.prepare(tmp), RosyClient(transport=fleet)
            mission = ledger.propose("outbound", client)
            self.assertEqual(mission, "physical-1")
            posted = fleet.calls[0]
            self.assertEqual(posted[:2], ("POST", "/api/fleet/arm-transfers/jobs"))
            self.assertEqual(set(posted[2]), {"workcell_id", "instance_id", "object_ref", "source", "target", "request_key"})
            self.assertNotIn("outbound", posted[2].values())
            fleet.status = "ACTION_SUCCEEDED"
            ledger.refresh("outbound", client, now=NOW)
            with self.assertRaises(ValueError):
                ledger.confirm("outbound", synthetic_evidence("early", mission, "outbound", "stand"), now=NOW)
            fleet.status = "GOAL_CONFIRMED"
            ledger.refresh("outbound", client, now=NOW)
            self.assertEqual(ledger.read("outbound")["state"], "WAITING")
            ledger.confirm("outbound", synthetic_evidence("placed", mission, "outbound", "stand"), now=NOW)
            self.assertEqual(ledger.read("outbound")["state"], "AWAITING_FINAL_EVIDENCE")
            ledger.finish("outbound", synthetic_evidence("final", mission, "outbound", "stand"), now=NOW)
            self.assertEqual(ledger.read("outbound")["state"], "COMPLETED")
        self.assertEqual(sum(method == "POST" for method, _, _ in fleet.calls), 1)
        self.assertFalse(any("admit" in path or "observations" in path or "compile" in path for _, path, _ in fleet.calls))

    def test_wrong_physical_receipt_or_request_never_confirms_business(self):
        for field, value in (("target", "wrong"), ("mission_id", "wrong"), ("policy_digest", "f" * 64),
                             ("dispatch_generation", True), ("physical_status", "BUSINESS_COMPLETED")):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as tmp:
                fleet = Fleet()
                ledger, client = self.prepare(tmp), RosyClient(transport=fleet)
                ledger.propose("outbound", client)
                fleet.status, fleet.receipt_changes = "GOAL_CONFIRMED", {field: value}
                with self.assertRaises(ValueError):
                    ledger.refresh("outbound", client, now=NOW)
                self.assertFalse(ledger.read("outbound")["goal_confirmed"])


if __name__ == "__main__":
    unittest.main()
