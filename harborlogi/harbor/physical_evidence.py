"""Map one public, source-bound Fleet closure proof into existing business facts."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import math
import re

from harbor.model import YARD_ARMS, aware_time, identifier, identifiers, revision
from harbor.process import ArmTransferBinding
from harbor.workflow import ArmRetreat, MoveEvidence, PlacementEvidence, RetreatEvidence


def _counter(value):
    if type(value) is not int or not 0 <= value < 2**63:
        raise ValueError("Expected an exact source counter/timestamp")
    return value


def _capture(value):
    seconds, remainder = divmod(_counter(value), 1_000_000_000)
    return datetime.fromtimestamp(seconds, timezone.utc) + timedelta(microseconds=remainder // 1000)


def _unique_fields(pairs):
    fields = dict(pairs)
    if len(fields) != len(pairs):
        raise ValueError("Duplicate physical proof field")
    return fields


@dataclass(frozen=True)
class RetreatBinding:
    arm: str
    workcell_id: str
    instance_id: str
    zone_id: str
    joint_names: tuple[str, ...]

    def __post_init__(self):
        if self.arm not in YARD_ARMS:
            raise ValueError("Retreat binding names one yard arm A/B")
        for value in (self.workcell_id, self.instance_id, self.zone_id):
            identifier(value)
        identifiers(self.joint_names)
        if not 1 <= len(self.joint_names) <= 32:
            raise ValueError("Expected an installed ordered joint list")


@dataclass(frozen=True)
class PhysicalEvidenceAdapter:
    placement_source: str
    retreat_source: str
    policy_digest: str
    arms: tuple[RetreatBinding, ...]

    def __post_init__(self):
        identifiers((self.placement_source, self.retreat_source))
        if not isinstance(self.policy_digest, str) or re.fullmatch(r"[0-9a-f]{64}", self.policy_digest) is None:
            raise ValueError("Expected the installed physical policy digest")
        if (not isinstance(self.arms, tuple) or not self.arms
                or not all(isinstance(arm, RetreatBinding) for arm in self.arms)
                or len({arm.arm for arm in self.arms}) != len(self.arms)
                or len({(arm.workcell_id, arm.instance_id) for arm in self.arms}) != len(self.arms)):
            raise ValueError("Expected distinct installed arm and owner correspondence")

    def read(self, client, binding, *, request_key, mission_id, attempt_id, plan_revision, required_arms, now):
        """Read only the original job/proof; never admit, submit observations or call a device."""
        if not isinstance(binding, ArmTransferBinding):
            raise ValueError("Physical evidence requires an ARM transfer binding")
        revision(plan_revision)
        aware_time(now)
        identifiers(required_arms)
        by_arm = {arm.arm: arm for arm in self.arms}
        if not required_arms or not set(required_arms) <= set(by_arm):
            raise ValueError("Every related arm needs an installed measurement binding")
        primary = (binding.workcell_id, binding.instance_id)
        if binding.yard_arm is not None:
            installed = by_arm.get(binding.yard_arm)
            if installed is None or (installed.workcell_id, installed.instance_id) != primary:
                raise ValueError("Yard arm differs from the transfer's installed owner")
        elif primary not in {(by_arm[name].workcell_id, by_arm[name].instance_id) for name in required_arms}:
            raise ValueError("Primary transfer owner is absent from the required arm bindings")
        job = binding.check(client.arm_job(mission_id)["job"], request_key)
        if job["mission_id"] != mission_id or job["resource_policy_digest"] != self.policy_digest:
            raise ValueError("Physical job differs from the original mission or installed policy")
        response = client.physical_evidence(mission_id)
        if not isinstance(response, dict) or set(response) != {"receipt", "evidence_document", "released_zone_ids"}:
            raise ValueError("Expected the closed physical evidence response")
        receipt = binding.check_receipt(job, response["receipt"])
        if receipt["attempt_id"] != attempt_id:
            raise ValueError("Physical evidence belongs to another attempt")
        raw = response["evidence_document"]
        if (not isinstance(raw, str) or len(raw.encode()) > 1024 * 1024
                or sha256(raw.encode()).hexdigest() != receipt["evidence_digest"]):
            raise ValueError("Original physical evidence bytes differ from the receipt")
        try:
            proof = json.loads(raw, object_pairs_hook=_unique_fields)
            return self._adapt(proof, receipt, response["released_zone_ids"], binding, plan_revision, required_arms, now)
        except (KeyError, TypeError, AttributeError, OverflowError, OSError) as error:
            raise ValueError("Incomplete physical evidence") from error

    def _adapt(self, proof, receipt, zones, binding, plan_revision, required_arms, now):
        if not isinstance(proof, dict) or set(proof) != {"context", "samples"}:
            raise ValueError("Expected the original closed proof document")
        context, samples = proof["context"], proof["samples"]
        if (context["phase"] != "POST_ACTION" or context["action_id"] != receipt["action_id"]
                or context["attempt_id"] != receipt["attempt_id"]
                or not isinstance(samples, list) or len(samples) != 2):
            raise ValueError("Physical proof does not bind the successful attempt")
        if not isinstance(zones, list) or not 1 <= len(zones) <= 32 or len(set(zones)) != len(zones):
            raise ValueError("Expected original released zone references")
        installed = {(arm.workcell_id, arm.instance_id): arm for arm in self.arms}
        targets = {(p["workcell_id"], p["instance_id"]): (p["journal_id"], p["owner_session_id"])
                   for p in context["owners"]}
        if not targets or len(targets) != len(context["owners"]) or not set(targets) <= set(installed):
            raise ValueError("Unknown or duplicate original owner scope")
        boundary, validated = _counter(context["not_before_ns"]), _counter(receipt["validated_at_ns"])
        previous, latest = {}, None
        for sample in samples:
            if (sample["producer_id"] != self.placement_source or sample["object_ref"] != binding.object_ref
                    or sample["policy_digest"] != self.policy_digest or sample["object_pose"] is None
                    or (sample["action_id"], sample["attempt_id"]) != (receipt["action_id"], receipt["attempt_id"])):
                raise ValueError("Source observation differs from the installed transfer")
            owners = {(p["workcell_id"], p["instance_id"]): p for p in sample["owners"]}
            if set(owners) != set(targets) or len(owners) != len(sample["owners"]):
                raise ValueError("Physical participant coverage is incomplete")
            times = [sample["object_observed_at_ns"]]
            for scope, owner in owners.items():
                joint = owner["joints"]
                expected = installed[scope]
                if ((owner["journal_id"], owner["owner_session_id"]) != targets[scope]
                        or tuple(joint["joint_names"]) != expected.joint_names
                        or len(joint["positions_rad"]) != len(expected.joint_names)
                        or not all(type(v) in (float, int) and math.isfinite(v) for v in joint["positions_rad"])):
                    raise ValueError("Joint measurement differs from the installed owner and order")
                identifier(joint["sample_id"])
                sequence = _counter(joint["sequence"])
                if scope in previous and (joint["sample_id"] == previous[scope][0] or sequence <= previous[scope][1]):
                    raise ValueError("Original joint samples are not distinct and ordered")
                previous[scope] = (joint["sample_id"], sequence)
                times.extend((owner["links_observed_at_ns"], owner["gripper_observed_at_ns"], joint["observed_at_ns"]))
            for stamp in times:
                if not boundary < _counter(stamp) <= validated or not timedelta(0) <= now - _capture(stamp) <= timedelta(seconds=5):
                    raise ValueError("Original physical capture is stale, future or outside the successful attempt")
            if latest is not None and (sample["evidence_id"] == latest["evidence_id"]
                                       or _counter(sample["sequence"]) <= _counter(latest["sequence"])):
                raise ValueError("Original observations are not distinct and ordered")
            latest = sample
        retreats = []
        for name in required_arms:
            arm = next(arm for arm in self.arms if arm.arm == name)
            owner = owners.get((arm.workcell_id, arm.instance_id))
            if owner is None or arm.zone_id not in zones:
                raise ValueError("Closure does not release every related arm's installed zone")
            joints = owner["joints"]
            # The immutable Fleet closure proves original release/clearance, not current zone availability.
            retreats.append(ArmRetreat(name, arm.zone_id, True, tuple(joints["positions_rad"]),
                                      _capture(joints["observed_at_ns"]), joints["sequence"], True))
        placement = PlacementEvidence(latest["evidence_id"], receipt["mission_id"], receipt["attempt_id"],
            plan_revision, self.placement_source, _capture(latest["object_observed_at_ns"]), binding.cargo_id, binding.destination)
        retreat_time = min(fact.pose_observed_at for fact in retreats)
        retreat = RetreatEvidence(receipt["receipt_id"], receipt["mission_id"], receipt["attempt_id"],
                                  plan_revision, self.retreat_source, retreat_time, tuple(retreats))
        return MoveEvidence(placement, retreat)
