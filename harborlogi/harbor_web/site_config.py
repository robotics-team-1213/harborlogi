"""Declared site inputs: constraints, map areas, spatial policy, and snapshots built from an observation."""
import math
from datetime import timedelta

from django.core.exceptions import ValidationError

from harbor.model import Bay, CargoHolding, YardSnapshot, PortConstraints
from harbor.layout import YardLayout
from harbor_web.models import clean_text, Cargo, CargoFootprint
from harbor_web.service_common import _domain
from harbor_web.yard_occupancy import AREA_LABELS


def constraints(payload, *, _validate_spatial=True):
    def build():
        required = {'outbound_stand', 'temporary_bays', 'allowed_moves', 'max_observation_age_seconds', 'conveyor', 'arm_reach'}
        if not isinstance(payload, dict) or not required <= set(payload) <= required | {'yard_layout', 'spatial_areas', 'spatial_moves', 'storage_areas', 'area_presentation', 'extra_outbound_stands', 'stack_rules', 'slot_cells'}:
            raise ValueError('Condition requires exactly the documented keys.')
        for name in ('temporary_bays', 'allowed_moves', 'arm_reach'):
            if not isinstance(payload[name], list):
                raise ValueError('Expected lists.')
        extra = payload.get('extra_outbound_stands', [])
        if not isinstance(extra, list) or len({*extra, payload['outbound_stand']}) != len(extra) + 1:
            raise ValueError('Extra outbound stands require unique text identifiers.')
        if extra and 'spatial_areas' not in payload:
            raise ValueError('Several outbound stands require spatial areas.')
        for name in ('allowed_moves', 'arm_reach'):
            if any(not isinstance(edge, list) or len(edge) != 2 for edge in payload[name]):
                raise ValueError('Expected pairs.')
            if len({tuple(e) for e in payload[name]}) != len(payload[name]):
                raise ValueError('Duplicate pairs.')
        for text in (payload['outbound_stand'], *extra, payload['conveyor'], *payload['temporary_bays'],
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
            if mapped & {payload['outbound_stand'], *extra, payload['conveyor'], *payload['temporary_bays']}:
                raise ValueError('Auxiliary locations must remain outside the storage grid.')
            known = mapped | {payload['outbound_stand'], *extra, payload['conveyor'], *payload['temporary_bays'], *payload.get('storage_areas', [])}
            referenced = {p for edge in payload['allowed_moves'] for p in edge} | {edge[1] for edge in payload['arm_reach']}
            if not referenced <= known:
                raise ValueError('Moves and reach must reference configured grid or auxiliary locations.')
        if _validate_spatial and any(k in payload for k in ('spatial_areas', 'spatial_moves', 'storage_areas', 'area_presentation', 'extra_outbound_stands', 'stack_rules', 'slot_cells')):
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
    from harbor.spatial import STACK_RULES
    rules = payload.get('stack_rules', {})
    if not isinstance(rules, dict) or any(not isinstance(v, str) or v not in STACK_RULES for v in rules.values()):
        raise ValueError('Stack rules map area identifiers to known rules.')
    areas = (Area('yard', layout.rows, layout.columns, layout.levels, rules.get('yard', 'any')),
             *(Area(**a, stack_rule=rules.get(a['area_id'], 'any')) for a in items))
    if not rules.keys() <= {a.area_id for a in areas}:
        raise ValueError('Stack rules must reference configured areas.')
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
    roles = [*storage, *payload['temporary_bays'], payload['outbound_stand'], *payload.get('extra_outbound_stands', [])]
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
                result[area.area_id] = dict(label=AREA_LABELS.get(area.area_id, area.area_id), x=right, y=y)
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
        slot_cells = payload.get('slot_cells')
        if slot_cells is not None and type(slot_cells) is not int:
            raise ValueError('slot_cells must be an integer when present.')
        if slot_cells and any(a.columns % slot_cells for a in areas):  # a slot never runs past the end of a row
            raise ValueError('Every area must hold whole slots: columns must be a multiple of slot_cells.')
        return SpatialPolicy(legacy.outbound_stand, legacy.temporary_bays,
            frozenset(tuple(p) for p in pairs), legacy.arm_reach, legacy.max_observation_age,
            tuple(payload.get('storage_areas', ['yard'])), tuple(payload.get('extra_outbound_stands', [])),
            slot_cells=slot_cells)
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
