"""Site map by container slots (KAN-115): what sits in each 1×3 slot, every level at once.

A slot is `slot_cells` columns of one row (3 by default, since every site container is 1×3). Each slot lists its
containers by level ("1층 K1 · 2층 K2"); sizes, turns and covered cells are fixed and not shown. A level split
between several cargo (older 1×1 or 2×1 footprints) still shows every one of them. Read-only.

Areas are placed like the site map: the configured presentation puts trays before the first row (left), after the
last row (right) or past the last column (below the yard, e.g. the conveyor). The yard's rows are drawn as lanes.
"""
from dataclasses import dataclass

from harbor_web import services

DEFAULT_SLOT = 3
ROLES = {'target': 'target', 'blocker': 'rehandle', 'rehandle': 'rehandle', 'stored': 'cargo', 'cargo': 'cargo'}


def _role(cargo, targets, rehandles):
    return 'target' if cargo in targets else 'rehandle' if cargo in rehandles else 'cargo'


def _slot_ranges(columns, slot):
    return [(first, min(first + slot - 1, columns)) for first in range(1, columns + 1, slot)]


def build_map(areas, occupant, *, labels, temporary, outbound, presentation, slot):
    """areas: [(area_id, rows, columns, levels)], yard first; occupant(area, row, column, level) -> (code, role)."""
    built = []
    for area_id, rows, columns, levels in areas:
        ranges = _slot_ranges(columns, slot)
        lanes = []
        for row in range(1, rows + 1):
            slots = []
            for number, (first, last) in enumerate(ranges, start=1):
                stack = []
                for level in range(1, levels + 1):
                    found = [occupant(area_id, row, column, level) for column in range(first, last + 1)]
                    codes = []
                    for code, role in found:
                        if code and code not in [c for c, _ in codes]:
                            codes.append((code, role))
                    if codes:
                        partial = len(codes) > 1 or any(code is None for code, _ in found)
                        stack.append(dict(level=level, cargo=', '.join(code for code, _ in codes),
                                          role=codes[0][1] if len(codes) == 1 else 'cargo', partial=partial))
                label = (f'{number}칸' if len(ranges) > 1 else '') if area_id == 'yard' else (f'{row}번' if rows > 1 else '')
                slots.append(dict(number=number, key=f'{area_id}-R{row}S{number}', label=label, stack=stack,
                                  height=max((entry['level'] for entry in stack), default=0), levels=levels))
            lanes.append(dict(number=row, key=f'{area_id}-R{row}', label=f'{row}행', slots=slots))
        role = '보관' if area_id == 'yard' else '임시 적재' if area_id in temporary else '반출' if area_id == outbound else area_id
        cargo = sorted({code.strip() for lane in lanes for s in lane['slots'] for e in s['stack'] for code in e['cargo'].split(',')})
        built.append(dict(id=area_id, label=labels.get(area_id, area_id), role=role, lanes=lanes, levels=levels,
                          slots=sum(len(lane['slots']) for lane in lanes) * levels,
                          used=sum(len(s['stack']) for lane in lanes for s in lane['slots']), cargo=cargo))
    yard, rows, columns = built[0], areas[0][1], areas[0][2]
    sides = dict(left=[], right=[], below=[])
    for (area_id, area_rows, _, _), area in zip(areas[1:], built[1:]):
        item = presentation.get(area_id, {'x': columns + 1, 'y': 0})
        side = 'left' if item['y'] + area_rows <= 0 else 'right' if item['y'] >= rows else 'below'
        sides[side].append(area)
    per_row = len(_slot_ranges(columns, slot))
    return dict(yard=yard, **sides, dimensions=f'{rows}행{f" × 한 행 {per_row}자리" if per_row > 1 else ""} · {yard["levels"]}층')


def _labels(payload):
    if 'yard_layout' not in payload or 'spatial_areas' not in payload:
        return {}  # a bay site has no area map; its bays are named by id
    return {area_id: item.get('label', area_id) for area_id, item in services.area_presentation(payload).items()}


def rows_from_snapshot(snapshot, *, labels=None, temporary=(), outbound=None, targets=(), rehandles=(),
                       presentation=None, slot=DEFAULT_SLOT):
    """Site map of one spatial snapshot; yard first, then the auxiliary areas in snapshot order."""
    targets, rehandles = set(targets), set(rehandles) - set(targets)
    occupied = snapshot.occupied()

    def occupant(area_id, row, column, level):
        code = occupied.get((area_id, row, column, level))
        return code, (_role(code, targets, rehandles) if code else None)
    ordered = sorted(snapshot.areas, key=lambda area: area.area_id != 'yard')
    return build_map([(a.area_id, a.rows, a.columns, a.levels) for a in ordered], occupant, labels=labels or {},
                     temporary=set(temporary), outbound=outbound, presentation=presentation or {}, slot=slot)


def slot_size(payload):
    return payload.get('slot_cells') or DEFAULT_SLOT


@dataclass(frozen=True)
class SlotSite:
    """A site with fixed container slots: the slot width, and the areas whose slot number or level says nothing."""
    size: int
    one_slot: frozenset = frozenset()  # e.g. the conveyor: its name alone
    one_level: frozenset = frozenset()  # e.g. the temp trays: no '1층'


def place_context(payload):
    """(area labels, SlotSite or None) of a condition payload, for place_label."""
    labels, size = _labels(payload), payload.get('slot_cells')
    if not size:
        return labels, None
    areas = payload.get('spatial_areas', [])
    return labels, SlotSite(size, frozenset(a['area_id'] for a in areas if a['rows'] * (a['columns'] // size) == 1),
                            frozenset(a['area_id'] for a in areas if a['levels'] == 1))


def _spatial(observation, targets, rehandles):
    payload = observation.condition.payload
    return rows_from_snapshot(services.spatial_snapshot(observation), labels=_labels(payload),
                              temporary=payload['temporary_bays'], outbound=payload['outbound_stand'], targets=targets,
                              rehandles=rehandles, presentation=services.area_presentation(payload), slot=slot_size(payload))


def slot_map_from_stage(stage, payload):
    """The same site map from a presented stage (observed or planned), keeping its target and rehandle marks."""
    sources = [stage, *stage.get('auxiliary_areas', [])]
    grid = {}
    for source in sources:
        area_id = source.get('area_id', 'yard')
        for layer in source['layers']:
            for row in layer['rows']:
                for cell in row['cells']:
                    if not cell.get('code'):
                        continue
                    for r in range(cell['row'], cell['row'] + cell.get('rowspan', 1)):
                        for c in range(cell['column'], cell['column'] + cell.get('colspan', 1)):
                            grid[area_id, r, c, cell['level']] = (cell['code'], ROLES.get(cell.get('role'), 'cargo'))
    areas = [(s.get('area_id', 'yard'), s['layout']['rows'], s['layout']['columns'], len(s['layers'])) for s in sources]
    return build_map(areas, lambda a, r, c, z: grid.get((a, r, c, z), (None, None)), labels=_labels(payload),
                     temporary=set(payload.get('temporary_bays', [])), outbound=payload.get('outbound_stand'),
                     presentation=services.area_presentation(payload), slot=slot_size(payload))


def _bays(observation, targets, rehandles):
    """Older bay observations have no grid: each bay is one lane with one slot, bottom to top."""
    snapshot = services.snapshot(observation)
    policy = observation.condition.payload
    temporary, outbound = set(policy['temporary_bays']), policy['outbound_stand']

    def area(bay, role):
        stack = [dict(level=level, cargo=code, role=_role(code, targets, rehandles), partial=False)
                 for level, code in enumerate(bay.cargo, start=1)]
        slot = dict(number=1, key=f'bay-{bay.bay_id}', label='', stack=stack, height=len(stack), levels=bay.capacity)
        return dict(id=bay.bay_id, label=bay.bay_id, role=role, levels=bay.capacity, slots=bay.capacity, used=len(stack),
                    cargo=sorted(bay.cargo), lanes=[dict(number=1, key=f'bay-{bay.bay_id}', label=bay.bay_id, slots=[slot])])
    lanes, below = [], []
    for bay in snapshot.bays:
        if bay.bay_id in temporary or bay.bay_id == outbound:
            below.append(area(bay, '임시 적재' if bay.bay_id in temporary else '반출'))
        else:
            lanes.append(dict(number=len(lanes) + 1, key=f'bay-{bay.bay_id}', label=bay.bay_id, slots=area(bay, '보관')['lanes'][0]['slots']))
    yard = dict(id='yard', label='장치 야드', role='보관', lanes=lanes, levels=max((s['levels'] for l in lanes for s in l['slots']), default=0),
                slots=sum(s['levels'] for l in lanes for s in l['slots']), used=sum(len(s['stack']) for l in lanes for s in l['slots']),
                cargo=sorted({e['cargo'] for l in lanes for s in l['slots'] for e in s['stack']}))
    return dict(yard=yard, left=[], right=[], below=below, dimensions=f'적재 자리 {len(lanes)}곳')


def yard_rows(observation, *, targets=(), rehandles=()):
    """None without an observation. ``targets``/``rehandles`` only mark cargo; they come from stored plans."""
    if observation is None:
        return None
    if observation.payload.get('schema') == 'grid-placement-v1':
        return _spatial(observation, set(targets), set(rehandles) - set(targets))
    return _bays(observation, set(targets), set(rehandles) - set(targets))


def slot_key(placement, slot=DEFAULT_SLOT):
    """The site-map key of the slot a stored move's placement dict sits in; ``slot`` is a width or a SlotSite."""
    size = getattr(slot, 'size', slot)
    return f"{placement['area_id']}-R{placement['row']}S{(placement['column'] - 1) // size + 1}"


def spot_label(placement, labels=None, slot=None):
    """Where in its area, without the level: '1행' or '1행 2칸' in the yard, '임시 적재(왼쪽) 1번' elsewhere.

    ``slot`` is the slot width or a SlotSite; an area with a single slot is named alone.
    """
    size = getattr(slot, 'size', slot)
    if placement.get('area_id') == 'yard':
        return f"{placement['row']}행" + (f" {(placement['column'] - 1) // size + 1}칸" if size else '')
    label = (labels or {}).get(placement.get('area_id'), placement.get('area_id'))
    return f"{label} {placement['row']}번" if size and placement.get('area_id') not in getattr(slot, 'one_slot', ()) else label


def place_label(placement, labels=None, slot=None):
    """'1행 2층' (or '1행 2칸 2층' when the site has two slots per row), '임시 적재(왼쪽) 1번 1층' elsewhere.

    With a SlotSite a single-level area drops its level: '임시 적재(왼쪽) 1번', '컨베이어(반출)'.
    """
    if not placement:
        return '반입(컨베이어)'
    spot = spot_label(placement, labels, slot)
    return spot if placement.get('area_id') in getattr(slot, 'one_level', ()) else f"{spot} {placement['level']}층"


def route_text(move, labels=None, slot=None):
    """'1행 1칸 2층 → 임시 적재(왼쪽) 1번 1층' for a stored spatial move."""
    return f"{placement_text(move['source_placement'], labels, slot)} → {placement_text(move['destination_placement'], labels, slot)}"


def placement_text(placement, labels=None, slot=None):
    """place_label plus, on a site without fixed slots, the column and turn of a narrower cargo."""
    label = place_label(placement, labels, slot)
    if placement and not slot and (placement['column'] != 1 or placement.get('orientation', 0) != 0):
        label += f" · {placement['column']}열" + (f" · {placement['orientation']}°" if placement.get('orientation', 0) else '')
    return label
