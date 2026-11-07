"""Read-only logical cuboids from the existing configured stage presentation."""
from copy import deepcopy


def _box(cell, area):
    return dict(id=cell['code'], area=area['id'], row=cell['row'], column=cell['column'], level=cell['level'],
        width=cell.get('colspan', 1), depth=cell.get('rowspan', 1),
        x=area['x'] + cell['column'] - 1, y=area['y'] + cell['row'] - 1, z=cell['level'] - 1,
        role=cell['role'], label=cell['label'], positions=list(cell.get('covered_positions') or [cell['position_id']]),
        footprint_revision=cell.get('footprint_revision'), orientation=cell.get('orientation', 0),
        size=cell.get('footprint_label', '1×1'))


def present_stage(stage, payload):
    """Carry explicit display metadata with the pinned condition, without changing old spacing."""
    if 'area_presentation' in payload:
        from harbor_web import services
        stage['area_presentation'] = services.area_presentation(payload)
        for source in [stage, *stage.get('auxiliary_areas', []), *stage.get('auxiliary_bays', [])]:
            area_id = source.get('area_id', source.get('bay_id', 'yard'))
            item = stage['area_presentation'][area_id]
            source.update(area_label=item['label'], x=item['x'], y=item['y'])
    stage['slot_cells'] = payload.get('slot_cells')
    if stage.get('spatial') and stage.get('layout') and payload.get('slot_cells'):
        # A site with fixed container slots reads slots, not cells (KAN-115); other sites keep the cell grid
        # because their cargo sizes and turns vary.
        from harbor_web.yard_rows import slot_map_from_stage
        stage['slot_map'] = slot_map_from_stage(stage, payload)
    return stage


def scene_for(stages):
    if not stages or any(not stage.get('layout') for stage in stages):
        return None
    first = stages[0]
    areas = [dict(id=first.get('area_id', 'yard'), label='장치 야드', x=0, y=0, **first['layout'])]
    # Auxiliary offsets are presentation spacing, never measured world coordinates.
    right, y = first['layout']['columns'] + 1.2, 0
    for source in first.get('auxiliary_areas', []):
        area_id = source['area_id']
        areas.append(dict(id=area_id, label={'temp': '임시 장치', 'stand': '반출대'}.get(area_id, area_id),
            x=right, y=y, **source['layout']))
        y += source['layout']['rows'] + 1.2
    for bay in first.get('auxiliary_bays', []):
        areas.append(dict(id=bay['bay_id'], label=bay.get('kind', bay['bay_id']), rows=1, columns=1,
            levels=bay['capacity'], x=right, y=y))
        y += 2.2
    for area in areas:
        area.update(first.get('area_presentation', {}).get(area['id'], {}))
    by_id = {area['id']: area for area in areas}
    # Preserve configured position IDs, including cells hidden by a merged footprint.
    for area in areas:
        area['addresses'] = {}
    for stage in stages:
        for source in [stage, *stage.get('auxiliary_areas', [])]:
            area = by_id[source.get('area_id', 'yard')]
            for layer in source['layers']:
                for row in layer['rows']:
                    for cell in row['cells']:
                        positions = cell.get('covered_positions') or [cell['position_id']]
                        coordinates = [(r, c) for r in range(cell['row'], cell['row'] + cell.get('rowspan', 1))
                                       for c in range(cell['column'], cell['column'] + cell.get('colspan', 1))]
                        for (r, c), position in zip(coordinates, positions):
                            area['addresses'][f"{r},{c},{cell['level']}"] = position
        for bay in stage.get('auxiliary_bays', []):
            area = by_id[bay['bay_id']]
            for level in range(1, area['levels'] + 1):
                area['addresses'][f'1,1,{level}'] = f"{bay['bay_id']}-L{level}"

    frames = []
    for stage in stages:
        boxes = []
        for source in [stage, *stage.get('auxiliary_areas', [])]:
            area = by_id[source.get('area_id', 'yard')]
            boxes.extend(_box(cell, area) for layer in source['layers'] for row in layer['rows']
                for cell in row['cells'] if cell['code'])
        for bay in stage.get('auxiliary_bays', []):
            area = by_id[bay['bay_id']]
            boxes.extend(_box(dict(cell, row=1, column=1, position_id=f"{bay['bay_id']}-L{cell['level']}"), area)
                for cell in bay['cells'] if cell['code'])
        frames.append(dict(index=stage['index'], label=stage['label'], boxes=boxes, move=deepcopy(stage.get('move'))))
    return dict(areas=[{k: a[k] for k in ('id', 'label', 'rows', 'columns', 'levels', 'x', 'y', 'addresses')} for a in areas],
        frames=frames, slot_cells=first.get('slot_cells'))
