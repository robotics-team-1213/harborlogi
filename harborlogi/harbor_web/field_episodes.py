"""Field episodes: assignment, CLEAR confirmation, departure intent and its inbound trip (ADR 0029)."""
from datetime import timedelta

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from harbor.model import aware_time
from harbor_web.models import Order, Audit, FieldEpisode, FieldConfirmation, DepartureIntent
from harbor_web.intake_loop import prepare_trip
from harbor_web.service_common import Conflict, _domain, _role, _save


@transaction.atomic
def assign_episode(actor, *, order_id, vehicle_id, assigned_to):
    _role(actor, 'operator')
    _role(assigned_to, 'field')
    order = Order.objects.get(pk=order_id)
    if order.kind != 'INBOUND':
        raise ValidationError('Field load confirmation applies only to inbound orders.')
    return _save(actor, FieldEpisode(created_by=actor, order=order, vehicle_id=vehicle_id, assigned_to=assigned_to), 'episode.assign')


@transaction.atomic
def reassign_episode(actor, *, episode_id, expected_version, assigned_to):
    _role(actor, 'operator')
    _role(assigned_to, 'field')
    episode = FieldEpisode.objects.get(pk=episode_id)
    if episode.version != expected_version or episode.state == 'REASSIGNED':
        raise Conflict('Episode changed; reload before reassigning.')
    if DepartureIntent.objects.filter(episode=episode).exists():
        raise Conflict('An episode with a departure intent cannot be reassigned.')
    if not FieldEpisode.objects.filter(pk=episode.pk, version=expected_version).exclude(state='REASSIGNED').update(
            state='REASSIGNED', version=expected_version + 1):
        raise Conflict('Episode changed; reload before reassigning.')
    replacement = _save(actor, FieldEpisode(created_by=actor, order=episode.order, vehicle_id=episode.vehicle_id,
                                           assigned_to=assigned_to), 'episode.assign')
    Audit.objects.create(actor=actor, action='episode.reassign', object_type='FieldEpisode', object_id=str(episode.pk),
                         detail={'replacement_episode_id': replacement.pk})
    return replacement


def _fresh(observed_at, now):
    _domain(lambda: aware_time(observed_at))
    _domain(lambda: aware_time(now))
    if not timedelta(0) <= now - observed_at <= timedelta(minutes=5):
        raise ValidationError('Confirmation must be within five minutes and cannot be in the future.')


@transaction.atomic
def confirm_episode(actor, *, episode_id, expected_version, outcome, observed_at, now=None):
    _role(actor, 'field')
    episode = FieldEpisode.objects.get(pk=episode_id)
    if episode.assigned_to_id != actor.pk:
        raise PermissionDenied
    if episode.version != expected_version or episode.state != 'OPEN':
        raise Conflict('Episode changed or was already confirmed.')
    _fresh(observed_at, timezone.now() if now is None else now)
    obj = FieldConfirmation(created_by=actor, actor=actor, episode=episode, outcome=outcome, observed_at=observed_at, version=expected_version)
    obj.full_clean()
    state = {'CLEAR': 'CONFIRMED', 'MISMATCH': 'MISMATCH', 'UNABLE': 'UNABLE'}[outcome]
    if not FieldEpisode.objects.filter(pk=episode.pk, version=expected_version, state='OPEN', assigned_to=actor).update(version=expected_version + 1, state=state):
        raise Conflict('Episode changed or was already confirmed.')
    return _save(actor, obj, 'episode.confirm')


@transaction.atomic
def prepare_departure(actor, *, episode_id, confirmation_id, now=None):
    _role(actor, 'operator')
    episode = FieldEpisode.objects.select_related('order').get(pk=episode_id)
    confirmation = FieldConfirmation.objects.select_related('actor').get(pk=confirmation_id)
    if confirmation.episode_id != episode.pk:
        raise Conflict('Confirmation belongs to another episode.')
    existing = DepartureIntent.objects.filter(episode=episode).first()
    if existing:
        if (existing.confirmation_id != confirmation.pk or existing.vehicle_id != episode.vehicle_id
                or existing.cargo_id != episode.order.cargo_id or existing.actor_id != actor.pk):
            raise Conflict('Departure intent identity differs.')
        return existing
    if (episode.state != 'CONFIRMED' or confirmation.outcome != 'CLEAR'
            or confirmation.actor_id != episode.assigned_to_id or episode.version != confirmation.version + 1):
        raise ValidationError('Current assigned field user must explicitly confirm CLEAR.')
    _role(confirmation.actor, 'field')
    now = timezone.now() if now is None else now
    _fresh(confirmation.observed_at, now)
    intent = _save(actor, DepartureIntent(created_by=actor, actor=actor, episode=episode, confirmation=confirmation,
                                          vehicle_id=episode.vehicle_id, cargo=episode.order.cargo), 'departure.prepare')
    # ADR 0029: the trip declares the cargo code; the executor proposes it and a named operator admits it in ROSY.
    _domain(lambda: prepare_trip(intent, now=now))
    return intent
