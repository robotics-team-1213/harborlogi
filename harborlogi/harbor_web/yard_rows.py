"""Yard plan by rows: with 1×3 containers one yard row holds one container per level (KAN-115 2-7).

Read-only. Cells of a row are grouped so a container that covers the whole row reads as one slot; a
row whose level is split between several cargo (older 1×1 or 2×1 footprints) keeps every run visible.
"""
from harbor_web import services

AREA_ROLES = {'yard': '보관'}


def _runs(cells):
    """Consecutive columns with the same occupant (None = empty) at one level of one row."""
    runs = []
    for column, cargo in cells:
        if runs and runs[-1]['cargo'] == cargo:
            runs[-1]['span'] += 1
        else:
            runs.append(dict(cargo=cargo, column=column, span=1))
    return runs


def _role(cargo, targets, rehandles):
    return 'target' if cargo in targets else 'rehandle' if cargo in rehandles else 'cargo'


def _spatial(observation, targets, rehandles):
    policy = observation.condition.payload
    labels = {area_id: value.get('label', area_id) for area_id, value in services.area_presentation(policy).items()}
    return rows_from_snapshot(services.spatial_snapshot(observation), labels=labels, temporary=policy['temporary_bays'],
                              outbound=policy['outbound_stand'], targets=targets, rehandles=rehandles)


def rows_from_snapshot(snapshot, *, labels=None, temporary=(), outbound=None, targets=(), rehandles=()):
    """Row view of one spatial snapshot; yard first, then the auxiliary areas in snapshot order."""
    labels, temporary = labels or {}, set(temporary)
    targets, rehandles = set(targets), set(rehandles) - set(targets)
    occupied = snapshot.occupied()
    areas = []
    for area in snapshot.areas:
        rows = []
        for row in range(1, area.rows + 1):
            cell_ids = ([snapshot.layout.at(row, column).bay_id for column in range(1, area.columns + 1)]
                        if area.area_id == 'yard' else
                        [f'{area.area_id}-R{row}C{column}' for column in range(1, area.columns + 1)])
            levels = []
            for level in range(area.levels, 0, -1):
                runs = _runs([(column, occupied.get((area.area_id, row, column, level)))
                              for column in range(1, area.columns + 1)])
                for run in runs:
                    run['role'] = _role(run['cargo'], targets, rehandles) if run['cargo'] else 'empty'
                    run['whole'] = run['span'] == area.columns
                levels.append(dict(level=level, runs=runs, empty=all(run['cargo'] is None for run in runs)))
            height = max((entry['level'] for entry in levels if not entry['empty']), default=0)
            cargo = sorted({run['cargo'] for entry in levels for run in entry['runs'] if run['cargo']})
            rows.append(dict(number=row, key=f'{area.area_id}-R{row}', cell_ids=cell_ids,
                             cells=f'{cell_ids[0]}–{cell_ids[-1]}' if len(cell_ids) > 1 else cell_ids[0],
                             height=height, levels=levels, cargo=cargo))
        free = sum(1 for row in rows for entry in row['levels'] if entry['empty'])
        role = '임시 장치' if area.area_id in temporary else '반출대' if area.area_id == outbound else AREA_ROLES.get(area.area_id, '보관')
        areas.append(dict(id=area.area_id, label=labels.get(area.area_id, area.area_id), role=role,
                          rows=rows, columns=area.columns, levels=area.levels, free=free,
                          cargo=sorted({c for row in rows for c in row['cargo']})))
    yard = next(area for area in areas if area['id'] == 'yard')
    return dict(spatial=True, yard=yard, auxiliary=[area for area in areas if area['id'] != 'yard'],
                dimensions=f"{yard['levels']}층 · {len(yard['rows'])}행 × {yard['columns']}열",
                holdings=snapshot.holdings)


def _bays(observation, targets, rehandles):
    """Older bay observations have no grid: each bay is shown as its own row, bottom to top."""
    snapshot = services.snapshot(observation)
    policy = observation.condition.payload
    temporary, outbound = set(policy['temporary_bays']), policy['outbound_stand']
    rows, auxiliary = [], []
    for number, bay in enumerate(snapshot.bays, start=1):
        levels = [dict(level=level, empty=level > len(bay.cargo),
                       runs=[dict(cargo=bay.cargo[level - 1], column=1, span=1, whole=True,
                                  role=_role(bay.cargo[level - 1], targets, rehandles))] if level <= len(bay.cargo) else
                            [dict(cargo=None, column=1, span=1, whole=True, role='empty')])
                  for level in range(bay.capacity, 0, -1)]
        row = dict(number=number, key=f'bay-{bay.bay_id}', cell_ids=[bay.bay_id], cells=bay.bay_id,
                   height=len(bay.cargo), levels=levels, cargo=sorted(bay.cargo))
        if bay.bay_id in temporary or bay.bay_id == outbound:
            auxiliary.append(dict(id=bay.bay_id, label=bay.bay_id, role='임시 장치' if bay.bay_id in temporary else '반출대',
                                  rows=[row], columns=1, levels=bay.capacity, free=bay.capacity - len(bay.cargo), cargo=sorted(bay.cargo)))
        else:
            rows.append(row)
    yard = dict(id='yard', label='장치 야드', role='보관', rows=rows, columns=1,
                levels=max((r['levels'][0]['level'] for r in rows), default=0),
                free=sum(1 for r in rows for entry in r['levels'] if entry['empty']), cargo=sorted({c for r in rows for c in r['cargo']}))
    return dict(spatial=False, yard=yard, auxiliary=auxiliary, dimensions=f'적재 자리 {len(rows)}곳', holdings=snapshot.holdings)


def yard_rows(observation, *, targets=(), rehandles=()):
    """None without an observation. ``targets``/``rehandles`` only mark cargo; they come from stored plans."""
    if observation is None:
        return None
    targets, rehandles = set(targets), set(rehandles) - set(targets)
    if observation.payload.get('schema') == 'grid-placement-v1':
        return _spatial(observation, targets, rehandles)
    return _bays(observation, targets, rehandles)


def place_label(placement, labels=None):
    """'1행 2층' in the yard, '임시 장치 1층' elsewhere; placement is a stored move's placement dict."""
    if not placement:
        return '관측 출발 위치 없음'
    if placement.get('area_id') == 'yard':
        return f"{placement['row']}행 {placement['level']}층"
    label = (labels or {}).get(placement.get('area_id'), placement.get('area_id'))
    return f"{label} {placement['level']}층"
