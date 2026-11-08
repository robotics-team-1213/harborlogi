"""Authenticated business evidence intake (design 24/27 P2, application side).

Stores submitted placement/retreat observations preserving their original identity —
evidence ids, source ids, observed times, revision — and binds them to the current
business execution for the standing executor. The wire transport and producer
credential issuance stay with the installation integrator (observation-acceptance.md);
this module is the intake store and adapter, never a producer and never an authorizer:
only the BusinessLedger's own binding rules turn a submission into business progress.
"""
from datetime import datetime, timedelta
import json
from pathlib import Path
from harbor.ledger import ReadOnlyLedger
import os

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from harbor.model import identifier
from harbor.workflow import ArmRetreat, MoveEvidence, PlacementEvidence, RetreatEvidence
from harbor_web.command_models import Command, EvidenceSubmission
from harbor_web import services

# Matches the BusinessLedger default freshness; the ledger remains the enforcer.
FRESHNESS = timedelta(seconds=5)
UNCONFIGURED_PLACEMENT = 'web-unconfigured-placement'
UNCONFIGURED_RETREAT = 'web-unconfigured-retreat'


def configured_sources():
    """Installer-registered evidence source ids from the environment, or None.

    Both sets must be nonempty, disjoint, and identifier-safe. Unset variables mean
    the deployment has no authenticated evidence sources yet: the ledger keeps the
    unconfigured placeholders that cannot authorize any submission.
    """
    placement = tuple(value.strip() for value in os.environ.get('HARBOR_PLACEMENT_SOURCES', '').split(',') if value.strip())
    retreat = tuple(value.strip() for value in os.environ.get('HARBOR_RETREAT_SOURCES', '').split(',') if value.strip())
    if not placement and not retreat:
        return None
    for value in placement + retreat:
        identifier(value)
    if not placement or not retreat or set(placement) & set(retreat):
        raise ValueError('Evidence sources need nonempty, disjoint placement and retreat sets')
    return placement, retreat


def ledger_sources():
    sources = configured_sources()
    if sources is None:
        return (UNCONFIGURED_PLACEMENT,), (UNCONFIGURED_RETREAT,)
    return sources


def _placement_from_json(data):
    if not isinstance(data, dict):
        raise ValueError('Expected a placement object')
    fields = dict(data)
    fields['observed_at'] = datetime.fromisoformat(fields['observed_at'])
    return PlacementEvidence(**fields)


def _retreat_from_json(data):
    if not isinstance(data, dict):
        raise ValueError('Expected a retreat object')
    fields = dict(data)
    fields['observed_at'] = datetime.fromisoformat(fields['observed_at'])
    arms = []
    for arm in fields.get('arms', ()):
        if not isinstance(arm, dict):
            raise ValueError('Expected an arm fact object')
        arm = dict(arm)
        arm['pose_observed_at'] = datetime.fromisoformat(arm['pose_observed_at'])
        arms.append(ArmRetreat(**arm))
    fields['arms'] = tuple(arms)
    return RetreatEvidence(**fields)


def submit(actor, *, command_id, placement, retreat):
    """Record one submitted observation pair after validating membership and shape."""
    services._role(actor, 'field')
    command = Command.objects.filter(pk=command_id).first()
    if command is None:
        raise ValidationError('Unknown command.')
    sources = configured_sources()
    if sources is None:
        raise ValidationError('EVIDENCE_SOURCES_NOT_CONFIGURED')
    try:
        placement_evidence = _placement_from_json(placement)
        retreat_evidence = _retreat_from_json(retreat)
    except (ValueError, TypeError, KeyError) as error:
        raise ValidationError('EVIDENCE_PAYLOAD_INVALID') from error
    if placement_evidence.source_id not in sources[0] or retreat_evidence.source_id not in sources[1]:
        raise ValidationError('EVIDENCE_SOURCE_NOT_REGISTERED')
    with transaction.atomic():
        record = EvidenceSubmission.objects.create(command=command, created_by=actor,
                                                   placement=placement, retreat=retreat)
    return record


def intake_provider():
    """Executor evidence provider: the freshest matching unconsumed submission, or None.

    A submission binds only when its mission/attempt/revision match the stage being
    confirmed and its identity has not been consumed by the ledger. Anything else is
    skipped, not promoted: no submission ever fabricates a stage transition.
    """
    def evidence(command, row, payload, phase):
        sources = configured_sources()
        ledger_path = os.environ.get('HARBOR_LEDGER_PATH', '')
        if sources is None or not ledger_path:
            return None
        if phase == 'step':
            mission, attempt = row['mission_id'], row['attempt_id']
        elif phase == 'final':
            mission, attempt = row['outbound_mission_id'], row['outbound_attempt_id']
        else:
            raise ValueError(phase)
        if mission is None or attempt is None:
            return None
        now = timezone.now()
        submissions = EvidenceSubmission.objects.filter(command_id=command.pk).order_by('-received_at', '-pk')
        for submission in submissions:
            try:
                placement = _placement_from_json(submission.placement)
                retreat = _retreat_from_json(submission.retreat)
            except (ValueError, TypeError, KeyError):
                continue
            if (placement.mission_id != mission or placement.attempt_id != attempt
                    or retreat.mission_id != mission or retreat.attempt_id != attempt
                    or placement.revision != payload['revision']
                    or placement.source_id not in sources[0] or retreat.source_id not in sources[1]):
                continue
            if not timedelta(0) <= now - placement.observed_at <= FRESHNESS:
                continue
            if not all(timedelta(0) <= now - arm.pose_observed_at <= FRESHNESS for arm in retreat.arms):
                continue
            if ReadOnlyLedger(ledger_path).consumed((placement.evidence_id, retreat.evidence_id)):
                continue
            return MoveEvidence(placement, retreat)
        return None
    return evidence


def physical_provider():
    """Read original Fleet closure facts for a current step; final evidence stays separate."""
    from harbor.physical_evidence import PhysicalEvidenceAdapter, RetreatBinding
    from harbor.process import ArmTransferBinding
    sources = configured_sources()
    if sources is None or any(len(group) != 1 for group in sources):
        raise ValueError('Physical evidence requires one installed placement and retreat source')
    raw = os.environ.get('HARBOR_PHYSICAL_EVIDENCE', '')
    if not raw or len(raw) > 8192:
        raise ValueError('Physical evidence correspondence is not configured')
    config = json.loads(raw)
    if (not isinstance(config, dict) or set(config) != {'policy_digest', 'arms'}
            or not isinstance(config['arms'], list) or not 1 <= len(config['arms']) <= 2
            or any(not isinstance(arm, dict) or not isinstance(arm.get('joint_names'), list)
                   for arm in config['arms'])):
        raise ValueError('Expected the closed installed physical evidence correspondence')
    arms = tuple(RetreatBinding(**{**arm, 'joint_names': tuple(arm['joint_names'])}) for arm in config['arms'])
    adapter = PhysicalEvidenceAdapter(sources[0][0], sources[1][0], config['policy_digest'], arms)

    def evidence(command, row, payload, phase):
        if phase == 'final':
            return None
        if phase != 'step':
            raise ValueError(phase)
        if (row['state'] != 'WAITING' or not row['goal_confirmed']
                or payload.get('execution_kind') != 'ARM_TRANSFER'
                or row['mission_id'] is None or row['attempt_id'] is None):
            return None
        from harbor_web.commands import acquire_registry
        binding = ArmTransferBinding(**payload['moves'][row['step_index']])
        return adapter.read(acquire_registry().client, binding,
            request_key=f"{row['request_key']}:{row['step_index']}", mission_id=row['mission_id'],
            attempt_id=row['attempt_id'], plan_revision=payload['revision'],
            required_arms=tuple(payload['move_arms'][row['step_index']]), now=timezone.now())
    return evidence
