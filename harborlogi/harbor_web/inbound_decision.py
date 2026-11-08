"""Inbound placement decided by a person, scenario progress, and the yard those decisions would leave (KAN-117 · KAN-119).

The algorithm computes each inbound placement (ADR 0010); a person confirms it here before anything is handed
over (ADR 0012). A decision is an immutable audit row. It does not move cargo: the yard changes only when an
observation shows it. Until then the rearrangement preview reads the observed yard plus the decided placements.
"""
from django.core.exceptions import ValidationError
from django.db import transaction

from harbor_web.coordination import serialized
from harbor_web.models import Audit, Command, Order, Plan
from harbor_web.service_common import Conflict, _role

DECIDE_ACTION = 'plan.decide'
# Why the decision page hides its button (workspace.plan_changes), most actionable first.
BLOCKED_BY = (('OBSERVATION_NOT_FRESH', '관측 유효 시간 지남 · 새 관측 필요'),
              ('OBSERVATION_REVISION_CHANGED', '새 관측 있음 · 다시 계산 필요'),
              ('PLAN_QUEUE_CHANGED', '요청이나 대기열이 바뀜 · 다시 계산 필요'))


def placement_decision(plan):
    return Audit.objects.filter(action=DECIDE_ACTION, object_type='Plan', object_id=str(plan.pk)).select_related('actor').first()


@serialized
@transaction.atomic
def decide_placement(actor, *, plan_id, expected_version):
    """Record that a person accepts the latest READY inbound recommendation of its request."""
    _role(actor, 'operator')
    plan = Plan.objects.select_related('order', 'observation', 'condition').get(pk=plan_id)
    if plan.result.get('plan_kind') != 'inbound-placement' or plan.result.get('status') != 'READY':
        raise ValidationError('결정할 수 있는 입고 추천이 아닙니다.')
    if plan.order.version != expected_version or plan.order_version != plan.order.version:
        raise Conflict('The request changed; review its latest plan.')
    if Plan.objects.filter(order=plan.order, pk__gt=plan.pk).exists():
        raise Conflict('A newer plan exists for this request.')
    existing = placement_decision(plan)
    if existing:
        return existing
    if decision_block(plan.order, plan):  # the decision page hides its button for the same reasons
        raise Conflict('The plan basis changed (request, queue or observation); calculate it again first.')
    return Audit.objects.create(actor=actor, action=DECIDE_ACTION, object_type='Plan', object_id=str(plan.pk),
        detail=dict(order_id=plan.order_id, order_version=plan.order_version, observation_id=plan.observation_id,
                    recommendation=plan.result['recommendation'], incoming_shape=plan.result.get('incoming_shape')))


def decision_block(order, plan, *, queue_digest=None):
    """Why a person cannot decide this inbound request now, or None when the decision page shows its button.

    The page (placement_plan.html hides the button while workspace.plan_changes reports a change or a newer plan
    exists), the server (decide_placement) and the scenario progress (latest plan only) share this rule, so none of
    them waits for a decision the others refuse (review of #44).
    """
    if plan is None:
        return '계획 없음 · 계산 필요'
    if plan.result.get('plan_kind') != 'inbound-placement' or plan.result.get('status') != 'READY':
        return '추천 위치 없음(보류)'
    from harbor_web.workspace import plan_changes
    codes = {change['code'] for change in plan_changes(order, plan, current_queue_digest=queue_digest)}
    return next((reason for code, reason in BLOCKED_BY if code in codes), None)


def inbound_progress():
    """Inbound requests without a completed handoff, with their latest plan, decision and decision block.

    `waiting` holds the undecided ones a person can decide now. `undecidable` holds the rest, each with the reason
    (no plan, HOLD, or a plan whose request, queue or observation changed since): they need a new calculation or a
    new observation, not a person's decision, so they do not hold the rearrangement scenario.
    """
    from harbor_web import services
    done = set(Command.objects.filter(state='COMPLETED').values_list('order_id', flat=True))
    digest, rows = services.queue_digest(), []
    for order in Order.objects.filter(kind='INBOUND').exclude(pk__in=done).select_related('cargo').order_by('priority_group', 'sequence', 'pk'):
        plan = Plan.objects.filter(order=order).select_related('observation', 'condition').order_by('-pk').first()
        decision = placement_decision(plan) if plan else None
        current = bool(decision and plan.order_version == order.version)
        rows.append(dict(order=order, plan=plan, decision=decision if current else None,
                         reason=None if current else decision_block(order, plan, queue_digest=digest)))
    undecided = [row for row in rows if not row['decision']]
    return dict(rows=rows, total=len(rows), decided=sum(1 for row in rows if row['decision']),
                waiting=[row for row in undecided if row['reason'] is None],
                undecidable=[row for row in undecided if row['reason'] is not None])


def projected_snapshot(snapshot, progress):
    """The observed yard with every decided, not yet observed inbound placed where it was decided."""
    from harbor.spatial import CargoShape, Placement
    placed = {p.cargo_id for p in snapshot.placements}
    shapes, placements = list(snapshot.shapes), list(snapshot.placements)
    for row in progress['rows']:
        detail = row['decision'].detail if row['decision'] else None
        if not detail or not detail.get('incoming_shape') or detail['recommendation']['cargo_id'] in placed:
            continue
        shapes.append(CargoShape(**detail['incoming_shape']))
        placements.append(Placement(**detail['recommendation']))
    return snapshot.__class__(snapshot.revision, snapshot.observed_at, snapshot.layout, snapshot.areas,
                              tuple(shapes), tuple(placements), snapshot.holdings)
