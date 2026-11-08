"""Fleet wire builders and validators; compatibility methods delegate here."""
import hashlib
import json
import re

from harbor.model import identifier


def trip_steps(intent):
    return [{"to": intent.destination, "via": list(intent.via)}]


def trip_digest(intent):
    """The only place Harbor computes a trip request_digest; same bytes as ROSY (D-908, D-996 at 161957814a).

    object_ref joins the body only when declared; an empty one keeps the original {candidates, steps}.
    """
    cargo = {"object_ref": intent.object_ref} if intent.object_ref else {}
    value = {"candidates": list(intent.candidates), "steps": trip_steps(intent), **cargo}
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=True, allow_nan=False).encode()).hexdigest()


def process_compile_request(binding):
    return {"recipe_id": binding.recipe_id, "recipe_digest": binding.recipe_digest,
            "cell_id": binding.cell_id, "cell_digest": binding.cell_digest}


def process_proposal_request(binding, request_key):
    identifier(request_key)
    if len(request_key) > 160 or any(ord(c) < 32 for c in request_key):
        raise ValueError("Invalid process request key")
    return {**process_compile_request(binding), "request_key": request_key,
            "workcell_id": binding.workcell_id, "instance_id": binding.instance_id}


def arm_request(binding, request_key):
    identifier(request_key)
    if len(request_key) > 160 or request_key != request_key.strip() or any(ord(c) < 32 for c in request_key):
        raise ValueError("Invalid physical transfer request key")
    return dict(request_key=request_key, workcell_id=binding.workcell_id, instance_id=binding.instance_id,
                object_ref=binding.object_ref, source=binding.source_ref, target=binding.target_ref)


def validate_arm_job(binding, job, request_key):
    request = arm_request(binding, request_key)
    transfer = {k: v for k, v in request.items() if k != "request_key"}
    digest = hashlib.sha256(json.dumps(transfer, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if (not isinstance(job, dict) or job.get("action_kind") != "ARM_TRANSFER"
            or job.get("request_key") != request_key or job.get("transfer") != transfer
            or job.get("request_digest") != digest or job.get("status") not in {
                "PROPOSED", "READY", "RUNNING", "ACTION_SUCCEEDED", "GOAL_CONFIRMED", "HOLD"}):
        raise ValueError("Fleet physical job does not match the bound Harbor move")
    identifier(job.get("mission_id"))
    return job


def validate_arm_receipt(binding, job, receipt):
    if not isinstance(receipt, dict):
        raise ValueError("A physical receipt is required")
    # No instance_id: Fleet may switch to a fallback arm, so the receipt's arm is Fleet's fact (ADR 0029, D3).
    expected = dict(mission_id=job["mission_id"], workcell_id=binding.workcell_id,
        object_ref=binding.object_ref, source=binding.source_ref, target=binding.target_ref,
        policy_digest=job["resource_policy_digest"], authority_epoch=job["authority_epoch"],
        dispatch_generation=job["dispatch_generation"], physical_status="AT_TARGET_AND_CLEAR")
    if job["status"] != "GOAL_CONFIRMED" or any(receipt.get(k) != v for k, v in expected.items()):
        raise ValueError("Physical receipt does not match the confirmed bound job")
    for key in ("authority_epoch", "dispatch_generation", "validated_at_ns"):
        if type(receipt.get(key)) is not int or not 0 <= receipt[key] < 2**63:
            raise ValueError("Physical receipt requires exact counters")
    # Receipt request_digest identifies the full grant; job request_digest identifies the transfer body (ADR 0006).
    for key in ("request_digest", "policy_digest", "evidence_digest"):
        if not isinstance(receipt.get(key), str) or not re.fullmatch(r"[0-9a-f]{64}", receipt[key]):
            raise ValueError("Invalid physical receipt digest")
    for key in ("receipt_id", "action_id", "attempt_id"):
        identifier(receipt.get(key))
    return receipt


def validate_trip_job(intent, job):
    if (not isinstance(job, dict) or job.get("request_key") != intent.request_key
            or job.get("request_digest") != intent.digest or job.get("robot_id") not in intent.candidates):
        raise ValueError("Fleet transport identity does not match the saved intent")
    identifier(job.get("mission_id"))
    if len(job["mission_id"]) > 192 or any(ord(c) < 32 for c in job["mission_id"]):
        raise ValueError("Invalid Fleet execution ID")
    steps = job.get("steps")
    if (not isinstance(steps, list) or len(steps) != 1 or not isinstance(steps[0], dict)
            or steps[0].get("step") != intent.steps[0] or steps[0].get("step_index") != 0
            or type(job.get("current_step_index")) is not int
            or job["current_step_index"] != (1 if job.get("status") == "GOAL_CONFIRMED" else 0)
            or job.get("status") not in {"READY", "RUNNING", "ACTION_SUCCEEDED", "GOAL_CONFIRMED", "HOLD"}):
        raise ValueError("Fleet transport snapshot does not match the requested step")
    action, attempt = steps[0].get("action_id"), steps[0].get("attempt_id")
    if action is not None:
        identifier(action)
    if action != attempt or job["status"] == "GOAL_CONFIRMED" and (
            attempt is None or steps[0].get("status") != "GOAL_CONFIRMED"):
        raise ValueError("Fleet transport attempt is not consistent")
    return job


def validate_trip_proposal(intent, proposal, principal=None):
    """Check a D-908 proposal against the saved intent; the proposer may never be its own approver."""
    if (not isinstance(proposal, dict) or proposal.get("request_key") != intent.request_key
            or proposal.get("request_digest") != intent.digest
            or proposal.get("candidate_robot_ids") != list(intent.candidates)
            or proposal.get("steps") != intent.steps
            or proposal.get("status") not in {"PROPOSED", "ADMITTED"}
            or proposal.get("object_ref", intent.object_ref or None) != (intent.object_ref or None)):
        raise ValueError("Fleet trip proposal does not match the saved intent")
    proposer = proposal.get("proposal_principal_id")
    if not isinstance(proposer, str) or not proposer or principal is not None and proposer != principal:
        raise ValueError("Fleet proposal principal differs")
    identifier(proposal.get("proposal_id"))
    admitted = (proposal.get("dispatch_generation"), proposal.get("approved_by"), proposal.get("mission_id"))
    if proposal["status"] == "PROPOSED":
        if any(value is not None for value in admitted):
            raise ValueError("A pending proposal cannot carry admission facts")
    else:
        if any(value is None for value in admitted):
            raise ValueError("An admitted proposal requires its approver, generation and job")
        if proposal["approved_by"] == proposer:
            raise ValueError("The proposer cannot approve its own trip")
        identifier(proposal["mission_id"])
    return proposal
