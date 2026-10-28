"""Public immutable proof maps to business facts without inventing a measured pose."""
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from types import SimpleNamespace
import unittest

from harbor.process import ArmTransferBinding


NOW = datetime(2030, 1, 1, tzinfo=timezone.utc)
STAMP = int(NOW.timestamp()) * 1_000_000_000
BINDING = ArmTransferBinding("cargo-1", "yard-1", "outbound", "cell-1", "arm-a",
                             "object-1", "source-1", "target-1", "A")


def proof_fixture():
    """Synthetic unit input; the integration harness must produce its own real HTTP proof."""
    owner = dict(workcell_id="cell-1", instance_id="arm-a", journal_id="journal-1", owner_session_id="session-1")
    context = dict(action_id="action-1", attempt_id="attempt-1", phase="POST_ACTION",
                   not_before_ns=STAMP - 400_000_000, owners=[owner])
    samples = []
    for sequence, age in ((11, 300_000_000), (12, 100_000_000)):
        stamp = STAMP - age
        joints = dict(sample_id=f"joint-source-{sequence}", sequence=sequence, observed_at_ns=stamp,
                      joint_names=["joint1", "joint2"], positions_rad=[0.1, -0.2])
        measured_owner = {**owner, "model_digest": "d" * 64, "calibration_revision": "cal-1",
            "transform_revision": "tf-1", "links_observed_at_ns": stamp,
            "links": {"link0": dict(bounds=dict(min_m=[1., 1., 1.], max_m=[1.1, 1.1, 1.1]),
                                   linear_velocity_m_s=[0., 0., 0.], angular_velocity_rad_s=[0., 0., 0.])},
            "gripper_observed_at_ns": stamp, "gripper_state": "OPEN", "held_state": "NONE",
            "held_object_ref": None, "joints": joints}
        samples.append(dict(evidence_id=f"observation-{sequence}", producer_id="camera-1",
            evaluator_revision="evaluator-1", sequence=sequence, action_id="action-1", attempt_id="attempt-1",
            frame_id="world", policy_digest="c" * 64, catalog_digest="a" * 64, bindings_digest="b" * 64,
            object_ref="object-1", object_origin_id="object-origin", object_observed_at_ns=stamp,
            object_pose=dict(position_m=[0.1, 0.2, 0.3], orientation_xyzw=[0., 0., 0., 1.]),
            owners=[measured_owner]))
    document = json.dumps(dict(context=context, samples=samples), sort_keys=True, separators=(",", ":"))
    request = BINDING.proposal_request("command-1:0")
    transfer = {key: value for key, value in request.items() if key != "request_key"}
    job = dict(action_kind="ARM_TRANSFER", mission_id="mission-1", request_key=request["request_key"],
        transfer=transfer, request_digest=sha256(json.dumps(transfer, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        status="GOAL_CONFIRMED", resource_policy_digest="c" * 64, authority_epoch=3, dispatch_generation=5)
    receipt = dict(receipt_id="physical-record-1", mission_id="mission-1", action_id="action-1",
        attempt_id="attempt-1", request_digest="e" * 64, workcell_id="cell-1", instance_id="arm-a",
        policy_digest="c" * 64, evidence_digest=sha256(document.encode()).hexdigest(), object_ref="object-1",
        source="source-1", target="target-1", authority_epoch=3, dispatch_generation=5,
        validated_at_ns=STAMP - 50_000_000, physical_status="AT_TARGET_AND_CLEAR")
    return job, dict(receipt=receipt, evidence_document=document, released_zone_ids=["shared-zone"])


def changed_document(response, change):
    updated = deepcopy(response)
    document = json.loads(updated["evidence_document"])
    change(document)
    updated["evidence_document"] = json.dumps(document, sort_keys=True, separators=(",", ":"))
    updated["receipt"]["evidence_digest"] = sha256(updated["evidence_document"].encode()).hexdigest()
    return updated


class PhysicalEvidenceTests(unittest.TestCase):
    def setUp(self):
        from harbor.physical_evidence import PhysicalEvidenceAdapter, RetreatBinding
        self.adapter = PhysicalEvidenceAdapter(placement_source="camera-1", retreat_source="fleet-1",
            policy_digest="c" * 64, arms=(RetreatBinding("A", "cell-1", "arm-a", "shared-zone",
                                                       ("joint1", "joint2")),))
        self.job, self.response = proof_fixture()
        self.calls = []
        self.client = SimpleNamespace(arm_job=self.job_read, physical_evidence=self.proof_read)

    def job_read(self, mission):
        self.calls.append(("job", mission))
        return {"job": deepcopy(self.job)}

    def proof_read(self, mission):
        self.calls.append(("proof", mission))
        return deepcopy(self.response)

    def read(self, **changes):
        args = dict(request_key="command-1:0", mission_id="mission-1", attempt_id="attempt-1",
                    plan_revision=7, required_arms=("A",), now=NOW)
        return self.adapter.read(self.client, BINDING, **{**args, **changes})

    def test_maps_original_source_facts_and_keeps_placement_separate_from_retreat(self):
        evidence = self.read()
        self.assertEqual(evidence.placement.evidence_id, "observation-12")
        self.assertEqual(evidence.placement.source_id, "camera-1")
        self.assertEqual(evidence.placement.observed_at, NOW - timedelta(milliseconds=100))
        self.assertEqual((evidence.placement.cargo_id, evidence.placement.location), ("cargo-1", "outbound"))
        self.assertEqual(evidence.retreat.evidence_id, "physical-record-1")
        self.assertEqual(evidence.retreat.source_id, "fleet-1")
        self.assertEqual(evidence.retreat.observed_at, NOW - timedelta(milliseconds=100))
        arm = evidence.retreat.arms[0]
        self.assertEqual((arm.arm, arm.zone_id, arm.joint_positions, arm.pose_revision),
                         ("A", "shared-zone", (0.1, -0.2), 12))
        self.assertTrue(arm.claim_released and arm.outside_zone)
        self.assertEqual(asdict(self.read()), asdict(evidence))
        self.assertEqual(self.calls, [("job", "mission-1"), ("proof", "mission-1")] * 2)

    def test_proof_bytes_cannot_be_changed_even_if_the_latest_pose_looks_valid(self):
        doc = json.loads(self.response["evidence_document"])
        doc["samples"][1]["owners"][0]["joints"]["positions_rad"][0] = 0.2
        self.response["evidence_document"] = json.dumps(doc)
        with self.assertRaises(ValueError):
            self.read()

    def test_wrong_current_job_and_attempt_never_produce_business_evidence(self):
        for changes in ({"attempt_id": "other-attempt"}, {"mission_id": "other-mission"},
                        {"request_key": "other-key"}, {"required_arms": ("A", "B")}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.read(**changes)

    def test_stale_source_times_are_not_refreshed_by_receipt_or_read_time(self):
        with self.assertRaises(ValueError):
            self.read(now=NOW + timedelta(seconds=6))
        self.response["receipt"]["validated_at_ns"] = STAMP + 6_000_000_000
        with self.assertRaises(ValueError):
            self.read(now=NOW + timedelta(seconds=6))

    def test_missing_wrong_order_or_unbound_joint_sources_are_refused(self):
        mutations = [
            lambda d: d["samples"][1]["owners"][0].pop("joints"),
            lambda d: d["samples"][1]["owners"][0]["joints"]["joint_names"].reverse(),
            lambda d: d["samples"][1]["owners"][0].update(owner_session_id="other-session"),
            lambda d: d["samples"][1].update(producer_id="other-camera"),
            lambda d: d["context"].update(attempt_id="other-attempt"),
            lambda d: d["samples"][1]["owners"][0]["joints"].update(observed_at_ns=STAMP + 1),
            lambda d: d["samples"][1]["owners"][0]["joints"].update(sequence=11),
        ]
        for mutation in mutations:
            with self.subTest(mutation=mutations.index(mutation)):
                _, self.response = proof_fixture()
                self.response = changed_document(self.response, mutation)
                with self.assertRaises(ValueError):
                    self.read()

    def test_a_receipt_without_matching_original_released_zone_cannot_attest_retreat(self):
        self.response["released_zone_ids"] = ["different-zone"]
        with self.assertRaises(ValueError):
            self.read()

    def test_two_owner_arm_correspondence_cannot_be_swapped(self):
        from harbor.physical_evidence import RetreatBinding
        def add_second_owner(document):
            owner = {**document["context"]["owners"][0], "instance_id": "arm-b"}
            document["context"]["owners"].append(owner)
            for sample in document["samples"]:
                measured = deepcopy(sample["owners"][0])
                measured.update(owner)
                measured["joints"]["positions_rad"] = [0.8, 0.9]
                measured["joints"]["sample_id"] += "-b"
                sample["owners"].append(measured)
        self.response = changed_document(self.response, add_second_owner)
        original = self.adapter.arms[0]
        self.adapter = replace(self.adapter, arms=(original,
            RetreatBinding("B", "cell-1", "arm-b", "shared-zone", original.joint_names)))
        evidence = self.read(required_arms=("A", "B"))
        self.assertEqual(evidence.retreat.arms[1].joint_positions, (0.8, 0.9))
        self.adapter = replace(self.adapter, arms=(replace(original, instance_id="arm-b"),
            RetreatBinding("B", "cell-1", "arm-a", "shared-zone", original.joint_names)))
        with self.assertRaises(ValueError):
            self.read(required_arms=("A", "B"))

    def test_unnamed_binding_primary_owner_must_belong_to_required_arms(self):
        from harbor.physical_evidence import RetreatBinding
        unnamed = replace(BINDING, yard_arm="")
        args = dict(request_key="command-1:0", mission_id="mission-1", attempt_id="attempt-1",
                    plan_revision=7, required_arms=("A",), now=NOW)
        self.assertIsNotNone(self.adapter.read(self.client, unnamed, **args))
        self.calls.clear()
        self.adapter = replace(self.adapter, arms=(self.adapter.arms[0],
            RetreatBinding("B", "cell-1", "arm-b", "shared-zone", ("joint1", "joint2"))))
        with self.assertRaises(ValueError):
            self.adapter.read(self.client, unnamed, **{**args, "required_arms": ("B",)})
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
