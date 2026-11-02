"""Persist declared inputs and planning results; never issue physical commands."""
import hashlib
import json
import math
import os
from dataclasses import asdict
from datetime import timedelta

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from harbor.model import Bay, CargoHolding, YardSnapshot, PortConstraints, OutboundRequest, aware_time
from harbor.layout import YardLayout
from harbor.outbound import plan_outbound, OutboundProposal
from harbor_web.auth import has_role
from harbor_web.coordination import serialized
from harbor_web.models import (clean_text, Cargo, CargoFootprint, Condition, Observation, Order, Plan, Audit,
                               FieldEpisode, FieldConfirmation, DepartureIntent, Measurement)


class Conflict(Exception):
    pass


def _role(actor, *roles):
    if not has_role(actor, *roles):
        raise PermissionDenied


def _save(actor, obj, action):
    obj.full_clean()
    obj.save()
    Audit.objects.create(actor=actor, action=action, object_type=type(obj).__name__, object_id=str(obj.pk))
    return obj


def _domain(call):
    try:
        return call()
    except (ValueError, TypeError, KeyError, OverflowError) as error:
        raise ValidationError('Invalid structured input: ' + str(error)) from error


def constraints(payload, *, _validate_spatial=True):
    def build():
        required = {'outbound_stand', 'temporary_bays', 'allowed_moves', 'max_observation_age_seconds', 'conveyor', 'arm_reach'}
        if not isinstance(payload, dict) or not required <= set(payload) <= required | {'yard_layout', 'spatial_areas', 'spatial_moves', 'storage_areas', 'area_presentation'}:
            raise ValueError('Condition requires exactly the documented keys.')
        for name in ('temporary_bays', 'allowed_moves', 'arm_reach'):
            if not isinstance(payload[name], list):
                raise ValueError('Expected lists.')
        for name in ('allowed_moves', 'arm_reach'):
            if any(not isinstance(edge, list) or len(edge) != 2 for edge in payload[name]):
                raise ValueError('Expected pairs.')
            if len({tuple(e) for e in payload[name]}) != len(payload[name]):
                raise ValueError('Duplicate pairs.')
        for text in (payload['outbound_stand'], payload['conveyor'], *payload['temporary_bays'],
                     *(v for edge in payload['allowed_moves'] for v in edge), *(v for edge in payload['arm_reach'] for v in edge)):
            if not isinstance(text, str):
                raise ValueError('Expected text identifiers.')
            clean_text(text)
        age = payload['max_observation_age_seconds']
        if type(age) not in (int, float):
            raise ValueError('Expected numeric freshness.')
        if 'yard_layout' in payload:
            layout = YardLayout.from_dict(payload['yard_layout'])
            mapped = {cell.bay_id for cell in layout.cells}
            for cell in layout.cells:
                clean_text(cell.bay_id)
            if mapped & {payload['outbound_stand'], payload['conveyor'], *payload['temporary_bays']}:
                raise ValueError('Auxiliary locations must remain outside the storage grid.')
            known = mapped | {payload['outbound_stand'], payload['conveyor'], *payload['temporary_bays'], *payload.get('storage_areas', [])}
            referenced = {p for edge in payload['allowed_moves'] for p in edge} | {edge[1] for edge in payload['arm_reach']}
            if not referenced <= known:
                raise ValueError('Moves and reach must reference configured grid or auxiliary locations.')
        if _validate_spatial and any(k in payload for k in ('spatial_areas', 'spatial_moves', 'storage_areas', 'area_presentation')):
            spatial_policy(payload)
            area_presentation(payload)
        return PortConstraints(payload['outbound_stand'], tuple(payload['temporary_bays']),
                               frozenset(tuple(e) for e in payload['allowed_moves']), timedelta(seconds=age),
                               payload['conveyor'], frozenset(tuple(e) for e in payload['arm_reach']))
    return _domain(build)


def snapshot(observation):
    if isinstance(observation.payload, dict) and observation.payload.get('schema') == 'grid-placement-v1':
        return spatial_snapshot(observation)
    if any(k in observation.condition.payload for k in ('storage_areas', 'area_presentation')):
        raise ValidationError('This map requires a grid-placement-v1 observation.')
    def build():
        payload = observation.payload
        if not isinstance(payload, dict) or set(payload) != {'bays', 'holdings'}:
            raise ValueError('Observation requires bays and holdings.')
        if not isinstance(payload['bays'], list) or not isinstance(payload['holdings'], list):
            raise ValueError('Expected observation lists.')
        bays, holdings = [], []
        for item in payload['bays']:
            if not isinstance(item, dict) or set(item) != {'bay_id', 'cargo', 'capacity'} or not isinstance(item['cargo'], list):
                raise ValueError('Invalid bay fields.')
            bays.append(Bay(item['bay_id'], tuple(item['cargo']), item['capacity']))
        for item in payload['holdings']:
            if not isinstance(item, dict) or set(item) != {'cargo_id', 'holder_id'}:
                raise ValueError('Invalid holding fields.')
            holdings.append(CargoHolding(**item))
        result = YardSnapshot(observation.pk or 0, observation.observed_at, tuple(bays), tuple(holdings))
        for text in (*(b.bay_id for b in result.bays), *(c for b in result.bays for c in b.cargo),
                     *(h.cargo_id for h in result.holdings), *(h.holder_id for h in result.holdings if h.holder_id is not None)):
            clean_text(text)
        codes = {c for b in result.bays for c in b.cargo} | {h.cargo_id for h in result.holdings}
        if codes != set(Cargo.objects.filter(code__in=codes).values_list('code', flat=True)):
            raise ValueError('Every observed cargo must be registered.')
        policy = constraints(observation.condition.payload)
        if 'yard_layout' in observation.condition.payload:
            YardLayout.from_dict(observation.condition.payload['yard_layout']).validate_bays(result.bays,
                auxiliary_ids=(policy.outbound_stand, policy.conveyor, *policy.temporary_bays))
        referenced = {policy.outbound_stand, *policy.temporary_bays, *(p for edge in policy.allowed_moves for p in edge)} - {policy.conveyor}
        if not referenced <= {b.bay_id for b in result.bays}:
            raise ValueError('Condition references unobserved bays.')
        return result
    return _domain(build)


def map_areas(payload):
    return _domain(lambda: _map_areas(payload))


def _map_areas(payload):
    from harbor.spatial import Area
    layout = YardLayout.from_dict(payload['yard_layout'])
    items = payload['spatial_areas']
    if not isinstance(items, list) or len(items) > 15 or any(not isinstance(a, dict) or
            set(a) != {'area_id', 'rows', 'columns', 'levels'} for a in items):
        raise ValueError('Spatial areas require explicit dimensions.')
    areas = (Area('yard', layout.rows, layout.columns, layout.levels), *(Area(**a) for a in items))
    identifiers = [a.area_id for a in areas]
    if len(set(identifiers)) != len(identifiers):
        raise ValueError('Spatial areas must be unique; yard is configured by yard_layout.')
    if {a.area_id for a in areas[1:]} & {c.bay_id for c in layout.cells}:
        raise ValueError('Auxiliary areas must not alias primary yard cell identifiers.')
    if len(areas) > 16 or sum(a.rows*a.columns*a.levels for a in areas) > 4096:
        raise ValueError('Map workload exceeds configured bounds.')
    storage = payload.get('storage_areas', ['yard'])
    if not isinstance(storage, list) or not storage or any(not isinstance(a, str) for a in storage) or len(set(storage)) != len(storage):
        raise ValueError('Storage areas require unique text identifiers.')
    roles = [*storage, *payload['temporary_bays'], payload['outbound_stand']]
    if 'yard' not in storage or len(roles) != len(set(roles)) or set(identifiers) != set(roles):
        raise ValueError('Storage, temporary and outbound roles must disjointly cover every area.')
    if payload['conveyor'] in identifiers:
        raise ValueError('The conveyor must remain outside configured areas.')
    for identifier in identifiers:
        clean_text(identifier)
    return areas


def _spatial_areas(payload):
    return YardLayout.from_dict(payload['yard_layout']), _map_areas(payload)


def area_presentation(payload):
    """Resolve display rectangles independently of physical robot coordinates."""
    def build():
        areas = _map_areas(payload)
        if 'area_presentation' not in payload:
            result = {'yard': dict(label='장치 야드', x=0, y=0)}
            right, y = areas[0].columns + 1.2, 0
            for area in areas[1:]:
                result[area.area_id] = dict(label={'temp': '임시 장치', 'stand': '반출대'}.get(area.area_id, area.area_id), x=right, y=y)
                y += area.rows + 1.2
            return result
        source = payload['area_presentation']
        if not isinstance(source, dict) or set(source) != {a.area_id for a in areas}:
            raise ValueError('Presentation must describe every configured area exactly once.')
        result = {}
        for area in areas:
            item = source[area.area_id]
            if not isinstance(item, dict) or set(item) != {'label', 'x', 'y'}:
                raise ValueError('Presentation requires exactly label, x and y.')
            if not isinstance(item['label'], str) or not item['label'].strip() or len(item['label']) > 80:
                raise ValueError('Area labels require 1 to 80 characters.')
            clean_text(item['label'])
            if any(type(item[k]) not in (int, float) or not math.isfinite(item[k]) or abs(item[k]) > 1000 for k in ('x', 'y')):
                raise ValueError('Presentation coordinates must be finite and within 1000.')
            result[area.area_id] = dict(item)
        for index, left in enumerate(areas):
            a = result[left.area_id]
            for right in areas[index+1:]:
                b = result[right.area_id]
                if a['x'] < b['x']+right.columns and b['x'] < a['x']+left.columns and a['y'] < b['y']+right.rows and b['y'] < a['y']+left.rows:
                    raise ValueError('Presentation area rectangles must not overlap.')
        return result
    return _domain(build)


def spatial_policy(payload):
    from harbor.spatial import SpatialPolicy
    def build():
        if not isinstance(payload, dict):
            raise ValueError('Expected a condition object.')
        legacy = constraints(payload, _validate_spatial=False)
        _, areas = _spatial_areas(payload)
        pairs = payload['spatial_moves']
        if not isinstance(pairs, list) or any(not isinstance(p, list) or len(p) != 2 or
                any(not isinstance(v, str) for v in p) for p in pairs):
            raise ValueError('Spatial moves require area pairs.')
        if len({tuple(p) for p in pairs}) != len(pairs):
            raise ValueError('Duplicate spatial moves.')
        known = {a.area_id for a in areas}
        if any(v not in known for pair in pairs for v in pair):
            raise ValueError('Spatial moves must reference configured areas.')
        return SpatialPolicy(legacy.outbound_stand, legacy.temporary_bays,
            frozenset(tuple(p) for p in pairs), legacy.arm_reach, legacy.max_observation_age,
            tuple(payload.get('storage_areas', ['yard'])))
    return _domain(build)


def spatial_snapshot(observation):
    from harbor.spatial import CargoShape, Placement, SpatialSnapshot
    def build():
        payload = observation.payload
        if not isinstance(payload, dict) or set(payload) != {'schema', 'observed_areas', 'placements', 'holdings'} or payload['schema'] != 'grid-placement-v1':
            raise ValueError('Spatial observation requires exactly the documented fields.')
        constraints(observation.condition.payload)
        layout, areas = _spatial_areas(observation.condition.payload)
        observed = payload['observed_areas']
        if not isinstance(observed, list) or any(not isinstance(a, str) for a in observed) or len(set(observed)) != len(observed) or set(observed) != {a.area_id for a in areas}:
            raise ValueError('Spatial observation must cover every configured area exactly once.')
        if not isinstance(payload['placements'], list) or not isinstance(payload['holdings'], list):
            raise ValueError('Expected observation lists.')
        if len(payload['placements']) + len(payload['holdings']) > 256:
            raise ValueError('Observation exceeds the cargo limit.')
        placements, shapes, holdings = [], [], []
        fields = {'cargo_id', 'area_id', 'row', 'column', 'level', 'orientation', 'footprint_revision'}
        for item in payload['placements']:
            if not isinstance(item, dict) or set(item) != fields or type(item['footprint_revision']) is not int:
                raise ValueError('Each placement requires a pinned footprint revision and complete address.')
            for key in ('cargo_id', 'area_id'):
                if not isinstance(item[key], str):
                    raise ValueError('Expected text identifiers.')
                clean_text(item[key])
            footprint = CargoFootprint.objects.filter(pk=item['footprint_revision'], cargo__code=item['cargo_id']).first()
            if footprint is None:
                raise ValueError('Footprint revision must belong to the observed cargo.')
            shapes.append(CargoShape(item['cargo_id'], footprint.length_cells, footprint.pk))
            placements.append(Placement(**{k: v for k, v in item.items() if k != 'footprint_revision'}))
        for item in payload['holdings']:
            if not isinstance(item, dict) or set(item) != {'cargo_id', 'holder_id'}:
                raise ValueError('Invalid holding fields.')
            holdings.append(CargoHolding(**item))
            clean_text(item['cargo_id'])
            if item['holder_id'] is not None:
                clean_text(item['holder_id'])
        codes = {h.cargo_id for h in holdings}
        if codes != set(Cargo.objects.filter(code__in=codes).values_list('code', flat=True)):
            raise ValueError('Every held cargo must be registered.')
        return SpatialSnapshot(observation.pk or 0, observation.observed_at, layout, areas,
            tuple(shapes), tuple(placements), tuple(holdings))
    return _domain(build)


def _provenance(provenance):
    if provenance == 'SYNTHETIC' and settings.MODE == 'live':
        raise ValidationError('Synthetic records are forbidden in live mode.')


@serialized
@transaction.atomic
def register_cargo(actor, length_cells=1, **validated_fields):
    _role(actor, 'operator')
    cargo = _save(actor, Cargo(created_by=actor, **validated_fields), 'cargo.register')
    register_footprint(actor, cargo_id=cargo.pk, length_cells=length_cells)
    return cargo


@serialized
@transaction.atomic
def register_footprint(actor, *, cargo_id, length_cells):
    _role(actor, 'operator')
    if type(length_cells) is not int or length_cells not in (1, 2, 3):
        raise ValidationError('Footprint length must be 1, 2 or 3 cells.')
    obj = _save(actor, CargoFootprint(created_by=actor, cargo=Cargo.objects.get(pk=cargo_id),
        length_cells=length_cells), 'footprint.register')
    from harbor_web.autoplanning import replan_enabled
    replan_enabled(actor, 'footprint.register')
    return obj


@transaction.atomic
def register_condition(actor, *, name, calibration_ref, payload):
    _role(actor, 'admin')
    constraints(payload)
    return _save(actor, Condition(created_by=actor, name=name, calibration_ref=calibration_ref, payload=payload), 'condition.register')


@serialized
@transaction.atomic
def record_observation(actor, *, source_id, source_revision, observed_at, provenance, payload, condition):
    _role(actor, 'admin')
    _provenance(provenance)
    obj = Observation(created_by=actor, source_id=source_id, source_revision=source_revision,
                      observed_at=observed_at, provenance=provenance, payload=payload, condition=condition)
    snapshot(obj)
    existing = Observation.objects.filter(source_id=source_id, source_revision=source_revision).first()
    if existing:
        if any(getattr(existing, key) != getattr(obj, key) for key in ('observed_at', 'provenance', 'payload', 'condition_id')):
            raise Conflict('Source revision already has different content.')
        return existing
    obj = _save(actor, obj, 'observation.record')
    from harbor_web.autoplanning import replan_enabled
    replan_enabled(actor, 'observation.record', source_id=source_id)
    return obj


def _queue_policy(order):
    if order.kind != 'OUTBOUND':
        return
    peers = Order.objects.filter(kind='OUTBOUND', priority_group=order.priority_group).exclude(pk=order.pk)
    if peers.exclude(tie_policy=order.tie_policy).exists():
        raise ValidationError('All members of a priority group must use the same policy.')
    if order.tie_policy == 'FIXED' and peers.filter(sequence=order.sequence).exists():
        raise ValidationError('Fixed group sequence must be unique.')


def request_snapshot(order):
    return dict(id=order.pk, cargo_id=order.cargo_id, cargo_code=order.cargo.code if order.cargo_id else None, kind=order.kind,
        priority_group=order.priority_group, sequence=order.sequence, tie_policy=order.tie_policy,
        note=order.note, version=order.version)


def queue_snapshot():
    return [request_snapshot(order) for order in Order.objects.exclude(kind='REARRANGE').select_related('cargo').order_by('pk')]


@serialized
@transaction.atomic
def create_order(actor, *, cargo, kind, priority_group, sequence, tie_policy, note=''):
    _role(actor, 'operator')
    obj = Order(created_by=actor, cargo=cargo, kind=kind, priority_group=priority_group,
                sequence=sequence, tie_policy=tie_policy, note=note)
    _queue_policy(obj)
    obj.full_clean()
    obj.save()
    Audit.objects.create(actor=actor, action='order.create', object_type='Order', object_id=str(obj.pk),
                         detail={'before': None, 'after': request_snapshot(obj)})
    from harbor_web.autoplanning import replan_enabled
    replan_enabled(actor, 'order.create')
    return obj


@serialized
@transaction.atomic
def update_order(actor, *, order_id, expected_version, priority_group, sequence, tie_policy, note=''):
    _role(actor, 'operator')
    obj = Order.objects.get(pk=order_id)
    if obj.version != expected_version:
        raise Conflict('Order changed; reload before editing.')
    before = request_snapshot(obj)
    for key, value in dict(priority_group=priority_group, sequence=sequence, tie_policy=tie_policy, note=note).items():
        setattr(obj, key, value)
    obj.version += 1
    obj.full_clean()
    _queue_policy(obj)
    updated = Order.objects.filter(pk=order_id, version=expected_version).update(
        priority_group=obj.priority_group, sequence=obj.sequence, tie_policy=obj.tie_policy,
        note=obj.note, version=obj.version, updated_at=timezone.now())
    if not updated:
        raise Conflict('Order changed; reload before editing.')
    Audit.objects.create(actor=actor, action='order.update', object_type='Order', object_id=str(obj.pk),
                         detail={'before': before, 'after': request_snapshot(obj)})
    obj.refresh_from_db()
    from harbor_web.autoplanning import replan_enabled
    replan_enabled(actor, 'order.update')
    return obj


def queue_digest():
    # REARRANGE orders are not queue entries: they must not invalidate other plans.
    rows = list(Order.objects.exclude(kind='REARRANGE').order_by('pk').values('id', 'cargo_id', 'kind', 'priority_group', 'sequence', 'tie_policy', 'note', 'version'))
    return hashlib.sha256(json.dumps(rows, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


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
    for outgoing in Order.objects.filter(kind='OUTBOUND').select_related('cargo').order_by('priority_group', 'sequence', 'pk'):
        schedule.setdefault(outgoing.cargo.code, (outgoing.priority_group, outgoing.sequence if outgoing.tie_policy == 'FIXED' else None))
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
    elif Order.objects.filter(kind='OUTBOUND', priority_group__lt=order.priority_group).exists() or (
            order.tie_policy == 'FIXED' and Order.objects.filter(kind='OUTBOUND', priority_group=order.priority_group, sequence__lt=order.sequence).exists()):
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
    _fresh(confirmation.observed_at, timezone.now() if now is None else now)
    return _save(actor, DepartureIntent(created_by=actor, actor=actor, episode=episode, confirmation=confirmation,
                                        vehicle_id=episode.vehicle_id, cargo=episode.order.cargo), 'departure.prepare')


@transaction.atomic
def record_measurement(actor, **fields):
    _role(actor, 'operator')
    _provenance(fields.get('provenance'))
    return _save(actor, Measurement(created_by=actor, **fields), 'measurement.record')


def compare_measurements(left, right):
    keys = ('scenario', 'conditions_ref', 'initial_layout_ref', 'information_ref', 'provenance')
    reason = 'MATCHED_CONDITIONS'
    if any(getattr(left, key) != getattr(right, key) for key in keys):
        reason = 'CONDITIONS_DIFFER'
    elif not left.baseline_reach_verified or not right.baseline_reach_verified:
        reason = 'BASELINE_REACH_UNVERIFIED'
    elif left.method == right.method:
        reason = 'METHODS_MUST_DIFFER'
    comparable = reason == 'MATCHED_CONDITIONS'
    def row(name):
        a, b = getattr(left, name), getattr(right, name)
        return {'name': name, 'left': a, 'right': b, 'delta': b - a if comparable and a is not None and b is not None else None}
    return {'comparable': comparable, 'reason': reason, 'primary': [row('vehicle_transfers'), row('human_interventions')],
            'secondary': [row('elapsed_seconds')]}
