"""Bounded event-driven recalculation of explicitly source-bound requests."""
import hashlib
import json

from django.utils import timezone
from harbor_web.command_models import ACTIVE_COMMAND_STATES
from harbor_web.models import Command, CargoFootprint, Observation, Order, Plan


def placement_reservations(order, observation):
    """Earlier automatic requests reserve empty cells, never observed support."""
    from harbor.spatial import CargoShape, Placement
    from harbor_web.inbound_decision import placement_decision
    binding = order.planning_observation if order.planning_observation_id else observation
    seen = {item['cargo_id'] for item in observation.payload.get('placements', [])}
    cells, references = set(), []
    earlier = Order.objects.filter(auto_replan=True, kind='INBOUND', pk__lt=order.pk,
        planning_observation__source_id=binding.source_id,
        planning_observation__condition_id=binding.condition_id).select_related('cargo')
    for request in earlier.order_by('pk'):
        if request.cargo.code in seen or Command.objects.filter(order=request, state__in=ACTIVE_COMMAND_STATES).exists():
            continue
        plan = Plan.objects.filter(order=request).select_related('observation').order_by('-pk').first()
        if (plan is None or plan.result.get('status') != 'READY'
                or plan.result.get('plan_kind') != 'inbound-placement'
                or plan.observation.source_id != observation.source_id
                or plan.condition_id != observation.condition_id):
            continue
        decision = placement_decision(plan)
        recommendation = decision.detail['recommendation'] if decision else plan.result.get('recommendation')
        incoming = plan.result.get('incoming_shape')
        if recommendation is None or incoming is None:
            continue
        shape, placement = CargoShape(**incoming), Placement(**recommendation)
        cells.update((placement.area_id,
            placement.row + (n if placement.orientation == 90 else 0),
            placement.column + (n if placement.orientation == 0 else 0), placement.level)
            for n in range(shape.length_cells))
        references.append({'plan_id': plan.pk, 'recommendation': recommendation, 'incoming_shape': incoming})
    return frozenset(cells), references


def planning_digest(order, observation, *, now=None):
    from harbor_web import services
    current = timezone.now() if now is None else now
    binding = order.planning_observation if order.planning_observation_id else observation
    age = services.constraints(observation.condition.payload).max_observation_age
    elapsed = (current - observation.observed_at).total_seconds()
    payload = {'queue': services.queue_digest(), 'observation': observation.pk,
        'binding': [binding.source_id, binding.condition_id], 'condition': observation.condition_id,
        'policy': observation.condition.payload,
        'reservations': placement_reservations(order, observation)[1] if order.kind == 'INBOUND' else [],
        'footprints': list(CargoFootprint.objects.order_by('pk').values_list('pk', 'cargo_id', 'length_cells')),
        'fresh': 0 <= elapsed <= age.total_seconds(),
        'commands': list(Command.objects.order_by('pk').values_list('pk', 'state'))}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def replan_enabled(actor, trigger, *, source_id=None):
    # Called inside each event's serialized atomic transaction; never calls a public mutator.
    from harbor_web import services
    for order in Order.objects.filter(auto_replan=True).select_related('cargo', 'planning_observation').order_by('pk'):
        binding = order.planning_observation
        if binding is None or (source_id is not None and binding.source_id != source_id):
            continue
        if Command.objects.filter(order=order, state__in=ACTIVE_COMMAND_STATES).exists():
            continue
        observation = Observation.objects.filter(source_id=binding.source_id).select_related('condition').latest('pk')
        current = timezone.now()
        digest = planning_digest(order, observation, now=current)
        previous = Plan.objects.filter(order=order).order_by('-pk').first()
        if previous and previous.result.get('input_digest') == digest:
            continue
        reason = None
        if observation.condition_id != binding.condition_id:
            reason = 'SOURCE_CONDITION_CHANGED'
        elif Command.objects.exclude(order=order).filter(state__in=ACTIVE_COMMAND_STATES).exists():
            # Command states do not prove physical completion or release of reserved space.
            reason = 'OTHER_COMMAND_REQUIRES_RECONCILIATION'
        services._calculate_plan(actor, order, observation, now=current, trigger=trigger,
            input_digest=digest, hold_reason=reason)
