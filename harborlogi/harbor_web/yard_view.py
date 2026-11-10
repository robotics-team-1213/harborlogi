"""Present configured logical addresses without inferring robot geometry."""
from copy import deepcopy

from harbor.layout import YardLayout


def yard_view(bays, policy, *, target='', blockers=()):
    result = deepcopy(bays)
    for bay in result:
        bay['kind'] = ('반출대' if bay['bay_id'] in (policy['outbound_stand'], *policy.get('extra_outbound_stands', [])) else '컨베이어' if bay['bay_id'] == policy['conveyor']
                       else '임시 장치' if bay['bay_id'] in policy['temporary_bays'] else '장치 칸')
        bay['free'] = bay['capacity'] - len(bay['cargo'])
        bay['cells'] = [dict(code=code, level=i + 1,
            role='target' if code == target else 'blocker' if code in blockers else 'stored',
            label='반출 대상' if code == target else '재취급 대상' if code in blockers else '보관 화물')
            for i, code in enumerate(bay['cargo'])]
    if 'yard_layout' not in policy:
        return dict(bays=result, layout=None, layers=[], auxiliary_bays=[])
    layout = YardLayout.from_dict(policy['yard_layout'])
    by_id = {bay['bay_id']: bay for bay in result}
    layers = []
    for level in range(1, layout.levels + 1):
        rows = []
        for row in range(1, layout.rows + 1):
            cells = []
            for column in range(1, layout.columns + 1):
                address = layout.at(row, column)
                bay = by_id[address.bay_id]
                cargo = bay['cells'][level - 1] if level <= len(bay['cells']) else dict(code=None, role='vacant', label='빈 위치')
                cells.append(dict(cargo, bay_id=address.bay_id, row=row, column=column, level=level,
                                  position_id=f'{address.bay_id}-L{level}', kind=bay['kind'], stack_height=len(bay['cargo'])))
            rows.append(dict(number=row, cells=cells))
        layers.append(dict(level=level, rows=rows))
    mapped = {c.bay_id for c in layout.cells}
    return dict(bays=result, layout=dict(rows=layout.rows, columns=layout.columns, levels=layout.levels,
                positions=len(layout.cells) * layout.levels, base_positions=len(layout.cells)),
                column_labels=list(range(1, layout.columns + 1)), layers=layers,
                auxiliary_bays=[b for b in result if b['bay_id'] not in mapped])


def spatial_yard_view(snapshot, *, target='', blockers=()):
    """One table cell per cargo, spanning its complete footprint at its pinned placement."""
    occupied = snapshot.occupied()
    placements = {p.cargo_id: p for p in snapshot.placements}
    blockers = set(blockers)
    if target in placements:
        dependencies = {target}
        while True:
            expanded = dependencies | {p.cargo_id for p in snapshot.placements
                if any(p.area_id == placements[c].area_id and p.level > placements[c].level and
                    {(r, col) for _, r, col, _ in snapshot.cells(p)} &
                    {(r, col) for _, r, col, _ in snapshot.cells(placements[c])} for c in dependencies)}
            if expanded == dependencies:
                break
            dependencies = expanded
        blockers.update(dependencies - {target})

    areas = []
    for area in snapshot.areas:
        layers = []
        for level in range(1, area.levels + 1):
            rows = []
            for row in range(1, area.rows + 1):
                cells = []
                for column in range(1, area.columns + 1):
                    code = occupied.get((area.area_id, row, column, level))
                    p = placements.get(code)
                    if p and (p.row, p.column) != (row, column):
                        continue  # The anchor cell owns the whole colspan/rowspan.
                    anchor = snapshot.layout.at(row, column).bay_id if area.area_id == 'yard' else f'{area.area_id}-R{row}C{column}'
                    cell = dict(code=code, bay_id=anchor, row=row, column=column, level=level,
                        position_id=f'{anchor}-L{level}', role='vacant', label='빈 위치', colspan=1, rowspan=1,
                        area_id=area.area_id, footprint_revision=None, covered_positions=[])
                    if p:
                        shape = snapshot.shape(code)
                        covered = sorted(snapshot.cells(p))
                        cell.update(role='target' if code == target else 'blocker' if code in blockers else 'stored',
                            label='반출 대상' if code == target else '재취급 대상' if code in blockers else '보관 화물',
                            colspan=shape.length_cells if p.orientation == 0 else 1,
                            rowspan=shape.length_cells if p.orientation == 90 else 1,
                            footprint_label=f'{shape.length_cells}×1', footprint_revision=shape.footprint_revision,
                            orientation=p.orientation,
                            covered_positions=[f'{snapshot.layout.at(r, col).bay_id}-L{z}' if a == 'yard'
                                               else f'{a}-R{r}C{col}-L{z}' for a, r, col, z in covered])
                    cells.append(cell)
                rows.append(dict(number=row, cells=cells))
            layers.append(dict(level=level, rows=rows))
        areas.append(dict(area_id=area.area_id, layers=layers, spatial=True, panel_id=f'area-{area.area_id}',
            cargo_count=sum(p.area_id == area.area_id for p in snapshot.placements),
            occupied_units=sum(a == area.area_id for a, _, _, _ in occupied),
            column_labels=list(range(1, area.columns + 1)),
            layout=dict(rows=area.rows, columns=area.columns, levels=area.levels,
                        base_positions=area.rows * area.columns, positions=area.rows * area.columns * area.levels)))
    yard = next(a for a in areas if a['area_id'] == 'yard')
    return dict(yard, bays=[], auxiliary_bays=[], spatial=True,
                auxiliary_areas=[a for a in areas if a['area_id'] != 'yard'],
                total_cargo_count=len(snapshot.placements), total_occupied_units=len(occupied))
