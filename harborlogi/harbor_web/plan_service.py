"""Compute and persist plans from an order, an observation, and the declared site inputs."""
import json
import os
from dataclasses import asdict

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from harbor.model import OutboundRequest, aware_time
from harbor.outbound import plan_outbound, OutboundProposal
from harbor_web.coordination import serialized
from harbor_web.models import CargoFootprint, CargoPriority, Observation, Order, Plan, Audit
from harbor_web.inbound_schedule import active_start
from harbor_web.service_common import Conflict, _domain, _role, _save
from harbor_web.site_config import constraints, snapshot, spatial_policy, spatial_snapshot
from harbor_web.order_queue import open_outbound_orders, queue_digest, queue_snapshot, request_snapshot


def _open_outbound_ahead(order):
    """Open outbound orders ahead in priority; a ledger-completed order no longer blocks."""
    ahead = Q(priority_group__lt=order.priority_group)
    if order.tie_policy == 'FIXED':
        ahead |= Q(priority_group=order.priority_group, sequence__lt=order.sequence)
    return open_outbound_orders().filter(ahead)


@serialized
@transaction.atomic
def make_plan(actor, *, order_id, expected_version, observation_id, now=None, automatic=None):
    _role(actor, 'operator')
    order = Order.objects.get(pk=order_id)
    if order.version != expected_version:
        raise Conflict('Order changed; reload before planning.')
    observation = Observation.objects.select_related('condition').get(pk=observation_id)
    if automatic is not None:
        if type(automatic) is not bool:
            raise ValidationError('Automatic planning requires a boolean.')
        if automatic and Order.objects.filter(auto_replan=True).exclude(pk=order.pk).count() >= 16:
            raise ValidationError('At most 16 automatic requests can be enabled.')
        order.auto_replan = automatic
        order.planning_observation = observation if automatic else None
        order.save(update_fields=['auto_replan', 'planning_observation'])
        Audit.objects.create(actor=actor, action='order.auto_replan', object_type='Order', object_id=str(order.pk),
            detail={'enabled': automatic, 'observation_id': observation.pk if automatic else None,
                    'source_id': observation.source_id if automatic else None,
                    'condition_id': observation.condition_id if automatic else None})
    plan = _calculate_plan(actor, order, observation, now=now)
    from harbor_web.autoplanning import replan_enabled
    replan_enabled(actor, 'plan.manual')
    return plan


def _outbound_schedule():
    schedule = {}
    # Completed orders still rank their cargo for rearrangement (design 32); only blocking uses open orders.
    for outgoing in Order.objects.filter(kind='OUTBOUND').select_related('cargo').order_by('priority_group', 'sequence', 'pk'):
        schedule.setdefault(outgoing.cargo.code, (outgoing.priority_group, outgoing.sequence if outgoing.tie_policy == 'FIXED' else None))
    # A container of the active inbound schedule without an outbound request leaves by its tier (ADR 0031 decision 3).
    start = active_start()
    if start is not None:
        for priority in CargoPriority.objects.filter(start_id=start).select_related('cargo'):
            schedule.setdefault(priority.cargo.code, (priority.tier, None))
    return schedule


def _installed_arm_candidates():
    """Per-(move, arm) correspondence from the installed bindings (design 29), or None.

    Any per-arm entry activates the correspondence as the reach source; without one,
    the legacy installer arm_reach table in the condition payload decides.
    """
    from harbor.process import arm_candidates, load_bindings
    path = os.environ.get('HARBOR_BINDINGS_PATH', '')
    if not path:
        return None
    try:
        with open(path, encoding='utf-8') as source:
            candidates = arm_candidates(load_bindings(json.load(source)))
    except (OSError, ValueError, TypeError, KeyError):
        return None
    return candidates or None


def _calculate_plan(actor, order, observation, *, now=None, trigger='manual', input_digest=None, hold_reason=None, rearrange=None):
    """Trusted calculator; callers hold the serialized transaction and authorize the event."""
    current = timezone.now() if now is None else now
    _domain(lambda: aware_time(current))
    yard = snapshot(observation)
    spatial = observation.payload.get('schema') == 'grid-placement-v1'
    incoming_shape = None
    reserved_cells, reservation_refs = frozenset(), []
    if order.kind == 'INBOUND' and spatial:
        from harbor_web.autoplanning import placement_reservations
        reserved_cells, reservation_refs = placement_reservations(order, observation)
    if hold_reason:
        proposal = OutboundProposal('HOLD', hold_reason, yard.revision)
    elif order.kind == 'INBOUND' and spatial:
        from harbor.spatial import CargoShape
        from harbor.placement import plan_placement
        footprint = CargoFootprint.objects.filter(cargo=order.cargo).order_by('-pk').first()
        if footprint is None:
            proposal = OutboundProposal('HOLD', 'INCOMING_FOOTPRINT_REQUIRED', yard.revision)
        else:
            incoming_shape = CargoShape(order.cargo.code, footprint.length_cells, footprint.pk)
            proposal = _domain(lambda: plan_placement(yard, incoming_shape,
                spatial_policy(observation.condition.payload), _outbound_schedule(), now=current, reserved_cells=reserved_cells))
    elif order.kind == 'REARRANGE':
        from harbor_web.rearrangement import rearrange_proposal
        proposal = rearrange_proposal(observation, rearrange, now=current) if spatial else OutboundProposal(
            'HOLD', 'SPATIAL_OBSERVATION_REQUIRED', yard.revision)
    elif order.kind == 'INBOUND':
        proposal = OutboundProposal('HOLD', 'INBOUND_PLANNER_NOT_AVAILABLE', yard.revision)
    elif _open_outbound_ahead(order).exists():
        proposal = OutboundProposal('HOLD', 'PRIORITY_ORDER_BLOCKED', yard.revision)
    elif spatial:
        from harbor.spatial import plan_spatial
        proposal = _domain(lambda: plan_spatial(yard, OutboundRequest(order.cargo.code, yard.revision),
            spatial_policy(observation.condition.payload), now=current, schedule=_outbound_schedule()))
    elif CargoFootprint.objects.filter(length_cells__gt=1).filter(
            cargo__code__in={order.cargo.code, *(c for b in yard.bays for c in b.cargo), *(h.cargo_id for h in yard.holdings)}).exists():
        proposal = OutboundProposal('HOLD', 'SPATIAL_OBSERVATION_REQUIRED', yard.revision)
    else:
        proposal = _domain(lambda: plan_outbound(yard, OutboundRequest(order.cargo.code, yard.revision),
            constraints(observation.condition.payload), now=current, arm_candidates=_installed_arm_candidates()))
    result = asdict(proposal)
    # Serialize tuples as JSON lists before returning the same representation as persisted reads.
    result = json.loads(json.dumps(result))
    result['request_snapshot'] = request_snapshot(order)
    result['queue_snapshot'] = queue_snapshot()
    previous = Plan.objects.filter(order=order).order_by('-pk').first()
    result['trigger'] = trigger
    result['previous_plan_id'] = previous.pk if previous else None
    result['input_digest'] = input_digest
    binding = order.planning_observation if order.planning_observation_id else observation
    result['binding'] = {'source_id': binding.source_id, 'condition_id': binding.condition_id}
    result['source_refs'] = {'observation_id': observation.pk, 'source_id': observation.source_id,
        'source_revision': observation.source_revision, 'condition_id': observation.condition_id}
    result['incoming_shape'] = asdict(incoming_shape) if incoming_shape else None
    result['reservation_plan_ids'] = [reference['plan_id'] for reference in reservation_refs]
    result['change_summary'] = {
        'recommendation_changed': bool(previous and previous.result.get('recommendation') != result.get('recommendation')),
        'move_count_before': len(previous.result.get('moves', [])) if previous else 0,
        'move_count_after': len(result.get('moves', []))}
    if input_digest is None:
        from harbor_web.autoplanning import planning_digest
        result['input_digest'] = planning_digest(order, observation, now=current)
    if spatial:
        result['model'] = 'grid-placement-v1'
        if order.kind == 'REARRANGE':
            from harbor_web.rearrangement import plan_record
            result['plan_kind'] = 'rearrangement'
            result['rearrangement'] = plan_record(proposal, rearrange)
        if order.kind == 'INBOUND':
            result['plan_kind'] = 'inbound-placement'
            result['required_completion'] = ['inbound_placed_at_target', 'arms_retreated']
        result['source_shapes'] = [asdict(shape) for shape in yard.shapes]
        projected = {p.cargo_id: p for p in yard.placements}
        for move in proposal.moves:
            projected[move.cargo_id] = move.destination_placement
        if result.get('recommendation') is not None:
            projected[order.cargo.code] = proposal.recommendation
        result['projected_placements'] = [asdict(p) for p in projected.values()] if proposal.status == 'READY' else None
        result['projected_bays'] = None
    else:
        projected = {bay.bay_id: list(bay.cargo) for bay in yard.bays}
        for move in proposal.moves:
            projected[move.source].remove(move.cargo_id)
            projected[move.destination].append(move.cargo_id)
        result['projected_bays'] = ([{'bay_id': b.bay_id, 'cargo': projected[b.bay_id], 'capacity': b.capacity} for b in yard.bays]
                                    if proposal.status == 'READY' else None)
    return _save(actor, Plan(created_by=actor, order=order, order_version=order.version,
                            observation=observation, condition=observation.condition,
                            queue_digest=queue_digest(), result=result), 'plan.create')


def placement_projection(plan):
    """Reconstruct the pinned predicted geometry, without creating an observation."""
    from dataclasses import replace
    from harbor.spatial import CargoShape, Placement
    result = plan.result
    if result.get('model') != 'grid-placement-v1' or result.get('status') != 'READY':
        return None
    yard = spatial_snapshot(plan.observation)
    shapes = tuple(CargoShape(**shape) for shape in result['source_shapes'])
    incoming = result.get('incoming_shape')
    if incoming is not None:
        shapes += (CargoShape(**incoming),)
    return replace(yard, shapes=shapes, placements=tuple(Placement(**p) for p in result['projected_placements']))
