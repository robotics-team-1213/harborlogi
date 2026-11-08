"""Named field-operator confirmation as a business evidence source (ADR 0023).

Until an automatic producer exists, a signed-in `field` or `operator` person can state that
they saw the cargo placed and each used arm waiting outside the zone. The statement is stored
as an immutable HumanAttestation bound to the ledger's current mission/attempt/step and later
handed to the BusinessLedger as human-source evidence; the ledger remains the only authority.
"""
from datetime import timedelta, timezone as dt_timezone
import os

from django import forms
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from harbor.ledger import ReadOnlyLedger, plan_payload
from harbor.workflow import HumanArmRetreat, MoveEvidence, PlacementEvidence, RetreatEvidence
from harbor_web import commands
from harbor_web.command_models import Command, HumanAttestation
from harbor_web.coordination import serialized
from harbor_web.service_common import Conflict, _role, _save

HUMAN_PLACEMENT_SOURCE = 'human-attested-placement'
HUMAN_RETREAT_SOURCE = 'human-attested-retreat'
FRESHNESS = timedelta(minutes=5)  # The ledger's human window.
# Offer a record only while it has this much ledger window left: the ledger re-reads the clock, and a record
# expiring between the two reads would turn a stale confirmation into an UNKNOWN reconciliation case.
LEDGER_MARGIN = timedelta(seconds=30)
ROLES = ('field', 'operator')
ARM_CLAIM = '구역 밖 대기 자세 육안 확인'


def human_evidence_enabled():
    """Whether the admin switch for the human evidence producer is on (the audited ADR0022 setting)."""
    from harbor_web.models import EvidenceProducerSetting
    setting = EvidenceProducerSetting.objects.filter(pk=1).first()
    return bool(setting and setting.human_confirmation)


def _stage(command):
    """The stage the ledger waits on, or None. Reads the ledger only; never writes."""
    path = os.environ.get('HARBOR_LEDGER_PATH', '')
    if not human_evidence_enabled() or not path or command.state not in commands.EXECUTING_STATES:
        return None
    try:
        row = ReadOnlyLedger(path).read(commands._key(command))
        commands._check_ledger_binding(command, row)
        payload = plan_payload(row)
    except (KeyError, ValueError, OSError, Conflict):
        return None
    if row['state'] == 'WAITING' and row['goal_confirmed']:
        move = payload['moves'][row['step_index']]
        return dict(phase='step', step_index=row['step_index'], mission_id=row['mission_id'],
                    attempt_id=row['attempt_id'], cargo_id=move['cargo_id'], location=move['destination'],
                    arms=tuple(payload['move_arms'][row['step_index']]), revision=payload['revision'])
    if row['state'] == 'AWAITING_FINAL_EVIDENCE' and row['outbound_mission_id'] and row['outbound_attempt_id']:
        return dict(phase='final', step_index=row['step_index'], mission_id=row['outbound_mission_id'],
                    attempt_id=row['outbound_attempt_id'], cargo_id=payload['outbound_id'],
                    location=payload['outbound_stand'], revision=payload['revision'],
                    arms=tuple(sorted({a for arms in payload['move_arms'] for a in arms})))
    return None


def awaiting(command):
    """The stage a person may attest now (form visible), or None."""
    stage = _stage(command)
    if stage is None or _live_record(command, stage) is not None:
        return None
    return stage


def _ids(record):
    return (f'human-{record.pk}:placement', f'human-{record.pk}:retreat')


def _live_record(command, stage):
    """A recorded, unexpired, unconsumed attestation already bound to this stage."""
    now = timezone.now()
    ledger = ReadOnlyLedger(os.environ['HARBOR_LEDGER_PATH'])
    for record in HumanAttestation.objects.filter(command=command).order_by('-pk'):
        if ((record.phase, record.step_index, record.mission_id, record.attempt_id, record.revision)
                == (stage['phase'], stage['step_index'], stage['mission_id'], stage['attempt_id'], stage['revision'])
                and set(record.arms) == set(stage['arms'])
                and timedelta(0) <= now - record.created_at <= FRESHNESS - LEDGER_MARGIN
                and not ledger.consumed(_ids(record))):
            return record
    return None


class AttestForm(forms.Form):
    expected_version = forms.IntegerField(min_value=1, widget=forms.HiddenInput)
    step_index = forms.IntegerField(min_value=0, widget=forms.HiddenInput)
    mission_id = forms.CharField(max_length=192, widget=forms.HiddenInput)
    attempt_id = forms.CharField(max_length=192, widget=forms.HiddenInput)
    phase = forms.ChoiceField(choices=[('step', 'step'), ('final', 'final')], widget=forms.HiddenInput)
    cargo_seen = forms.BooleanField(label='화물이 표시된 위치에 내려놓인 것을 눈으로 확인했습니다')
    arms = forms.MultipleChoiceField(label='쓴 팔의 퇴피 확인', widget=forms.CheckboxSelectMultiple)
    note = forms.CharField(label='메모(선택)', max_length=300, required=False)
    photo_ref = forms.CharField(label='사진 참조(선택)', max_length=200, required=False)

    def __init__(self, *args, required_arms=('A',), **kwargs):
        super().__init__(*args, **kwargs)
        self.required_arms = tuple(required_arms)
        self.fields['arms'].choices = [(arm, f'{arm} 팔 — {ARM_CLAIM}') for arm in self.required_arms]

    def clean_arms(self):
        if set(self.cleaned_data['arms']) != set(self.required_arms):
            raise ValidationError('쓴 팔을 모두 확인해야 합니다.')
        return sorted(self.cleaned_data['arms'])


def form_for(command, stage):
    return AttestForm(required_arms=stage['arms'], initial=dict(
        expected_version=command.version, step_index=stage['step_index'], mission_id=stage['mission_id'],
        attempt_id=stage['attempt_id'], phase=stage['phase']))


@serialized  # SQLite ignores select_for_update; one writer at a time keeps one live record per stage
def attest(actor, *, command_id, expected_version, phase, step_index, mission_id, attempt_id, arms,
           note='', photo_ref=''):
    """Store one attestation if it still matches the live ledger stage; otherwise reject."""
    _role(actor, *ROLES)
    with transaction.atomic():
        command = Command.objects.select_for_update().filter(pk=command_id).first()
        if command is None:
            raise ValidationError('Unknown command.')
        if command.version != expected_version:
            raise Conflict('Command changed.')
        stage = awaiting(command)
        if stage is None:
            raise Conflict('Nothing awaits a human confirmation.')
        if ((stage['phase'], stage['step_index'], stage['mission_id'], stage['attempt_id'])
                != (phase, step_index, mission_id, attempt_id) or set(arms) != set(stage['arms'])):
            raise Conflict('Stage or attempt changed.')
        return _save(actor, HumanAttestation(
            command=command, created_by=actor, phase=phase, step_index=step_index, mission_id=mission_id,
            attempt_id=attempt_id, revision=stage['revision'], cargo_id=stage['cargo_id'],
            location=stage['location'], arms=sorted(arms), note=note, photo_ref=photo_ref),
            'human_attestation.record')


def human_provider():
    """Executor evidence provider: the newest live attestation for the stage, as human-source evidence."""
    def evidence(command, row, payload, phase):
        stage = _stage(command)
        if stage is None or stage['phase'] != phase:
            return None
        record = _live_record(command, stage)
        if record is None:
            return None
        who = f'user-{record.created_by_id}'
        when = record.created_at.astimezone(dt_timezone.utc)
        placement_id, retreat_id = _ids(record)
        args = (record.mission_id, record.attempt_id, record.revision)
        return MoveEvidence(
            PlacementEvidence(placement_id, *args, HUMAN_PLACEMENT_SOURCE, when, record.cargo_id,
                              record.location, attested_by=who),
            RetreatEvidence(retreat_id, *args, HUMAN_RETREAT_SOURCE, when,
                            tuple(HumanArmRetreat(arm, who, True) for arm in record.arms)))
    return evidence


def attested_final_commands(command_ids):
    """Command pks whose outbound-final evidence came from a consumed human attestation."""
    path = os.environ.get('HARBOR_LEDGER_PATH', '')
    if not path:
        return set()
    try:
        ledger = ReadOnlyLedger(path)
        return {r.command_id for r in HumanAttestation.objects.filter(command_id__in=command_ids, phase='final')
                if ledger.consumed(_ids(r))}
    except Exception:
        return set()
