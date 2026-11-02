"""R2 rearrangement execution layer: standing authorization and automatic check (design 32).

The calculation is harbor.spatial.plan_rearrangement. This module only decides
whether a calculated plan may become a Command without a per-plan user
confirmation: the user confirms the authorization, never each plan (ADR0017
decision 4). Every violated condition leaves no Command and one recorded HOLD reason.
"""
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from harbor_web import commands, services
from harbor_web.command_models import ACTIVE_COMMAND_STATES
from harbor_web.coordination import serialized
from harbor_web.models import Audit, CargoFootprint, Command, Observation, Order, Plan, RearrangeAuthorization

DEFAULT_VALID_FOR = timezone.timedelta(hours=8)
HOLD_ACTION = 'rearrange.hold'


def outbound_pending():
    """An outbound order is pending until a Command for it has completed (orders have no close state)."""
    done = Command.objects.filter(state='COMPLETED').values('order_id')
    return Order.objects.filter(kind='OUTBOUND').exclude(pk__in=done).exists()


def pending_inbound_lengths():
    """Footprint lengths of waiting inbound orders whose latest plan found no legal place."""
    done = set(Command.objects.filter(state='COMPLETED').values_list('order_id', flat=True))
    lengths = []
    for order in Order.objects.filter(kind='INBOUND').exclude(pk__in=done).order_by('pk'):
        plan = Plan.objects.filter(order=order).order_by('-pk').first()
        footprint = CargoFootprint.objects.filter(cargo_id=order.cargo_id).order_by('-pk').first()
        if plan and footprint and plan.result.get('reason') == 'NO_LEGAL_PLACEMENT':
            lengths.append(footprint.length_cells)
    return tuple(lengths)


def rearrange_proposal(observation, options, *, now):
    from harbor.spatial import plan_rearrangement
    if options.get('proposal') is not None:
        return options['proposal']
    snapshot = services.spatial_snapshot(observation)
    return services._domain(lambda: plan_rearrangement(
        snapshot, services.spatial_policy(observation.condition.payload), services._outbound_schedule(),
        now=now, expected_revision=snapshot.revision, k=options['k'], max_moves=options['max_moves'],
        outbound_pending=options['outbound_pending'], requested=options['requested'],
        pending_inbound=options['pending_inbound']))


def plan_record(proposal, options):
    """Per-plan record: which authorization, B/N before and after, gain, move count."""
    score = proposal.optimization.score if proposal.optimization else None
    return dict(authorization_id=options.get('authorization_id'), automatic=bool(options.get('automatic')),
        blocked_before=score and score.blocked_pairs_before, blocked_after=score and score.blocked_pairs,
        unknown_before=score and score.unknown_pairs_before, unknown_after=score and score.unknown_pairs,
        gain=score and score.gain, move_count=len(proposal.moves))


def _options(**values):
    return dict(dict(requested=False, outbound_pending=outbound_pending(), pending_inbound=pending_inbound_lengths()), **values)


def current_authorization(now=None):
    now = timezone.now() if now is None else now
    return RearrangeAuthorization.objects.filter(disabled_at__isnull=True, valid_until__gt=now).order_by('-pk').first()


def _switch_off(actor, now):
    for old in RearrangeAuthorization.objects.filter(disabled_at__isnull=True):
        old.disabled_at, old.disabled_by = now, actor
        old.save()
        Audit.objects.create(actor=actor, action='rearrange.authorization.disable', object_type='RearrangeAuthorization',
                             object_id=str(old.pk))


@serialized
@transaction.atomic
def enable_authorization(actor, *, k=2, max_moves=6, valid_until=None, daily_limit=20, now=None):
    services._role(actor, 'operator')  # Same permission that confirms execution (prepare_command).
    now = timezone.now() if now is None else now
    valid_until = now + DEFAULT_VALID_FOR if valid_until is None else valid_until
    if valid_until <= now:
        raise ValidationError('The authorization must end in the future.')
    _switch_off(actor, now)
    item = RearrangeAuthorization(created_by=actor, k=k, max_moves=max_moves, valid_until=valid_until, daily_limit=daily_limit)
    item.full_clean()
    item.save()
    Audit.objects.create(actor=actor, action='rearrange.authorization.enable', object_type='RearrangeAuthorization',
                         object_id=str(item.pk), detail=dict(k=k, max_moves=max_moves, daily_limit=daily_limit,
                                                             valid_until=valid_until.isoformat()))
    return item


@serialized
@transaction.atomic
def disable_authorization(actor, *, now=None):
    services._role(actor, 'operator')
    _switch_off(actor, timezone.now() if now is None else now)


@serialized
@transaction.atomic
def request_rearrangement(actor, *, observation_id, k=2, max_moves=6, now=None):
    """Manual R2: allows F-only plans, never executes without the normal user confirmation."""
    services._role(actor, 'operator')
    observation = Observation.objects.select_related('condition').get(pk=observation_id)
    order = Order.objects.create(created_by=actor, kind='REARRANGE')
    return services._calculate_plan(actor, order, observation, now=now, trigger='rearrange.request',
        rearrange=_options(k=k, max_moves=max_moves, requested=True, automatic=False))


def _record_hold(auth, reason):
    last = Audit.objects.filter(action=HOLD_ACTION).order_by('-pk').first()
    if last and last.detail.get('reason') == reason:
        return  # The idle check repeats every tick; record a reason once until it changes.
    Audit.objects.create(actor=None, action=HOLD_ACTION, object_type='RearrangeAuthorization',
                         object_id=str(auth.pk) if auth else '', detail=dict(reason=reason))


def _automatic_today(now):
    start = timezone.localtime(now).replace(hour=0, minute=0, second=0, microsecond=0)
    plans = Plan.objects.filter(order__kind='REARRANGE', created_at__gte=start, command__isnull=False)
    return sum(1 for plan in plans if plan.result.get('rearrangement', {}).get('automatic'))


@serialized
def auto_check(now=None):
    """Create one REARRANGE Plan and Command when every condition holds, else record why not.

    Returns the Command or the HOLD reason text. Called from the executor's idle tick.
    """
    now = timezone.now() if now is None else now
    auth = current_authorization(now)
    if auth is None:
        return _hold_with(None, 'AUTHORIZATION_OFF_OR_EXPIRED')
    if _automatic_today(now) >= auth.daily_limit:
        return _hold_with(auth, 'DAILY_LIMIT_REACHED')
    if outbound_pending():
        return _hold_with(auth, 'OUTBOUND_PENDING')
    if Command.objects.filter(state__in=ACTIVE_COMMAND_STATES).exists():
        return _hold_with(auth, 'OTHER_COMMAND_ACTIVE')
    observation = Observation.objects.select_related('condition').order_by('-pk').first()
    if observation is None or observation.payload.get('schema') != 'grid-placement-v1':
        return _hold_with(auth, 'SPATIAL_OBSERVATION_REQUIRED')
    last = Plan.objects.filter(order__kind='REARRANGE').order_by('-pk').first()
    if last and last.observation_id == observation.pk and last.queue_digest == services.queue_digest():
        return 'ALREADY_PLANNED'  # One automatic try per observation and queue state.
    options = _options(k=auth.k, max_moves=auth.max_moves, automatic=True, authorization_id=auth.pk)
    try:
        proposal = rearrange_proposal(observation, options, now=now)  # HOLD reasons: holding, stale, not worth.
    except ValidationError:
        return _hold_with(auth, 'SPATIAL_OBSERVATION_INVALID')
    if proposal.status != 'READY':
        return _hold_with(auth, proposal.reason)
    return _execute(auth, observation, dict(options, proposal=proposal), now)


def _hold_with(auth, reason):
    _record_hold(auth, reason)
    return reason


@transaction.atomic
def _create_plan(auth, observation, options, now):
    order = Order.objects.create(created_by=auth.created_by, kind='REARRANGE')
    return services._calculate_plan(auth.created_by, order, observation, now=now, trigger='rearrange.auto', rearrange=options)


def _execute(auth, observation, options, now):
    # The revision must still be the newest right before the Plan is made and again before submission.
    if Observation.objects.filter(source_id=observation.source_id, pk__gt=observation.pk).exists():
        return _hold_with(auth, 'OBSERVATION_REVISION_CHANGED')
    plan = _create_plan(auth, observation, options, now)
    gate = commands.capability(plan)
    if not gate['enabled']:
        return _hold_with(auth, gate['reason'])
    try:
        command = commands.prepare_command(auth.created_by, plan_id=plan.pk, expected_version=plan.order_version)
        return commands.submit_command(auth.created_by, command_id=command.pk, expected_version=command.version)
    except (ValidationError, services.Conflict, PermissionDenied):
        return _hold_with(auth, 'EXECUTION_INPUTS_UNAVAILABLE')
