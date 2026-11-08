"""Pure map drafts: presentation may change; execution authority is never added."""
from copy import deepcopy
import hashlib
import json
import math

from django.core.exceptions import ValidationError

from harbor.layout import YardLayout
from harbor_web import services
from harbor_web.models import clean_text


def _fresh_payload():
    return dict(outbound_stand='stand', temporary_bays=['temp'], conveyor='conveyor',
        allowed_moves=[], arm_reach=[], spatial_moves=[], max_observation_age_seconds=60,
        yard_layout=dict(rows=3, columns=3, levels=3, cells=[
            dict(bay_id=f'Y{r}{c}', row=r, column=c) for r in range(1, 4) for c in range(1, 4)]),
        spatial_areas=[dict(area_id='temp', rows=1, columns=1, levels=3),
                       dict(area_id='stand', rows=1, columns=1, levels=1)], storage_areas=['yard'])


def editor_draft(source_payload=None):
    payload = deepcopy(source_payload) if source_payload is not None else _fresh_payload()
    if 'yard_layout' not in payload:
        # Legacy bay names carry no measured row/column mapping.
        if 'yard' in {payload['outbound_stand'], payload['conveyor'], *payload['temporary_bays']}:
            raise ValidationError('Legacy auxiliary ID yard conflicts with the reserved primary area.')
        payload['yard_layout'] = _fresh_payload()['yard_layout']
    if 'spatial_areas' not in payload:
        payload['spatial_areas'] = [dict(area_id=key, rows=1, columns=1, levels=3)
            for key in payload['temporary_bays']]
        payload['spatial_areas'].append(dict(area_id=payload['outbound_stand'], rows=1, columns=1, levels=1))
    areas = services.map_areas(payload)
    presentation = services.area_presentation(payload)
    storage = set(payload.get('storage_areas', ['yard']))
    outbound = {payload['outbound_stand'], *payload.get('extra_outbound_stands', [])}
    return dict(areas=[dict(id=a.area_id, role=('storage' if a.area_id in storage else
        'outbound' if a.area_id in outbound else 'temporary'),
        rows=a.rows, columns=a.columns, levels=a.levels, **presentation[a.area_id],
        cells=deepcopy(payload['yard_layout']['cells']) if a.area_id == 'yard' else []) for a in areas])


def map_scene(payload):
    if 'yard_layout' not in payload:
        return None
    draft = editor_draft(payload)
    return dict(kind='map-definition', observed=False,
        areas=[{k: a[k] for k in ('id', 'label', 'rows', 'columns', 'levels', 'x', 'y')} for a in draft['areas']],
        frames=[dict(index=0, label='배치 미관측', boxes=[], move=None)])


def _normalize(dto):
    if not isinstance(dto, dict) or set(dto) != {'areas'} or not isinstance(dto['areas'], list):
        raise ValidationError('Map requires exactly an areas list.')
    if not 1 <= len(dto['areas']) <= 16:
        raise ValidationError('A map requires 1–16 areas.')
    areas = []
    for item in dto['areas']:
        if not isinstance(item, dict) or set(item) != {'id', 'label', 'role', 'rows', 'columns', 'levels', 'x', 'y', 'cells'}:
            raise ValidationError('Unexpected area fields; execution policies cannot be edited here.')
        a = dict(item)
        for key in ('id', 'label'):
            if not isinstance(a[key], str) or len(a[key]) > (96 if key == 'id' else 80):
                raise ValidationError('Invalid area identifier or label.')
            clean_text(a[key])
        if a['role'] not in ('storage', 'temporary', 'outbound'):
            raise ValidationError('Unknown area role.')
        for key, limit in (('rows', 12), ('columns', 12), ('levels', 6)):
            if type(a[key]) is not int or not 1 <= a[key] <= limit:
                raise ValidationError('Area dimensions exceed the map limits.')
        for key in ('x', 'y'):
            if type(a[key]) not in (int, float) or not math.isfinite(a[key]) or abs(a[key]) > 1000:
                raise ValidationError('Invalid display position.')
            a[key] = float(a[key])
        if a['id'] == 'yard':
            if a['role'] != 'storage':
                raise ValidationError('The reserved yard must remain storage.')
            if not isinstance(a['cells'], list) or len(a['cells']) != a['rows'] * a['columns']:
                raise ValidationError('Every configured yard column is required.')
            layout = YardLayout.from_dict({k: a[k] for k in ('rows', 'columns', 'levels', 'cells')})
            a = deepcopy(a)
            a['cells'] = sorted(a['cells'], key=lambda c: (c['row'], c['column']))
            for cell in layout.cells:
                clean_text(cell.bay_id)
        elif a['cells'] != []:
            raise ValidationError('Auxiliary areas use generated row/column addresses.')
        areas.append(a)
    ids = [a['id'] for a in areas]
    if len(set(ids)) != len(ids) or 'yard' not in ids:
        raise ValidationError('Unique areas including yard are required.')
    if not any(a['role'] == 'outbound' for a in areas):
        raise ValidationError('At least one outbound area is required.')
    if sum(a['rows'] * a['columns'] * a['levels'] for a in areas) > 4096:
        raise ValidationError('Map exceeds 4096 positions.')
    for i, a in enumerate(areas):
        for b in areas[i + 1:]:
            if (a['x'] < b['x'] + b['columns'] and b['x'] < a['x'] + a['columns'] and
                    a['y'] < b['y'] + b['rows'] and b['y'] < a['y'] + a['rows']):
                raise ValidationError('Display area rectangles overlap.')
    return dict(areas=sorted(areas, key=lambda a: (a['id'] != 'yard', a['id'])))


def build_map(source_payload, dto, *, source_id=None):
    """Rebuild canonical data from a server-owned source and a strict map DTO."""
    try:
        normalized = _normalize(dto)
        source = deepcopy(source_payload) if source_payload is not None else _fresh_payload()
        old = editor_draft(source_payload)
        previous = {a['id']: a for a in old['areas']}
        current = {a['id']: a for a in normalized['areas']}
        yard = current['yard']
        if {cell['bay_id'] for cell in yard['cells']} & (current.keys() | {source['conveyor']}):
            raise ValidationError('Yard cell IDs must be separate from area and conveyor IDs.')
        payload = deepcopy(source)
        outbound = [a['id'] for a in normalized['areas'] if a['role'] == 'outbound']
        payload.update(yard_layout={k: deepcopy(yard[k]) for k in ('rows', 'columns', 'levels', 'cells')},
            spatial_areas=[dict(area_id=a['id'], **{k: a[k] for k in ('rows', 'columns', 'levels')})
                for a in normalized['areas'] if a['id'] != 'yard'],
            storage_areas=[a['id'] for a in normalized['areas'] if a['role'] == 'storage'],
            temporary_bays=[a['id'] for a in normalized['areas'] if a['role'] == 'temporary'],
            outbound_stand=outbound[0], extra_outbound_stands=outbound[1:],
            area_presentation={a['id']: {k: a[k] for k in ('label', 'x', 'y')} for a in normalized['areas']})
        rules = {k: v for k, v in payload.get('stack_rules', {}).items() if k in current}
        payload.pop('stack_rules', None)
        if rules:
            payload['stack_rules'] = rules
        if not outbound[1:]:
            payload.pop('extra_outbound_stands')  # keep single-stand payloads byte-compatible
        unchanged_areas = {key for key, a in current.items() if key in previous and
            all(a[k] == previous[key][k] for k in ('role', 'rows', 'columns', 'levels'))}
        # Inferred legacy auxiliary dimensions are a draft, not installation evidence.
        if 'spatial_areas' not in source:
            unchanged_areas &= {'yard'}
        old_cells = {c['bay_id']: (c['row'], c['column']) for c in previous['yard']['cells']}
        stable_cells = {c['bay_id'] for c in yard['cells'] if old_cells.get(c['bay_id']) == (c['row'], c['column'])
            and yard['levels'] == previous['yard']['levels']}
        if 'yard_layout' not in source:
            stable_cells.clear()
            unchanged_areas.clear()
        stable = (unchanged_areas - {'yard'}) | stable_cells | {source['conveyor']}
        if 'yard_layout' not in source:
            stable.discard('yard')
        removed = {}
        for key in ('arm_reach', 'allowed_moves', 'spatial_moves'):
            edges = source.get(key, [])
            safe = unchanged_areas if key == 'spatial_moves' else stable
            payload[key] = [deepcopy(edge) for edge in edges if
                (edge[1] in safe if key == 'arm_reach' else all(v in safe for v in edge))]
            removed[key] = [edge for edge in edges if edge not in payload[key]]
        services.constraints(payload)
        fingerprint = hashlib.sha256(json.dumps(dict(source_id=source_id, map=normalized),
            sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
        summary = dict(added_areas=sorted(current.keys() - previous.keys()),
            removed_areas=sorted(previous.keys() - current.keys()),
            changed_areas=sorted(k for k in current.keys() & previous.keys() if current[k] != previous[k]),
            removed_references=removed, unobserved=True)
        return dict(map=normalized, payload=payload, summary=summary, fingerprint=fingerprint, scene=map_scene(payload))
    except (ValueError, TypeError, KeyError, OverflowError) as error:
        raise ValidationError('Invalid map: ' + str(error)) from error
