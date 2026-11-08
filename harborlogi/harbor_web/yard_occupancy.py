"""Per-column occupancy and planned-rehandle marks, read from an already built observed stage."""
STEP_CAP = 3  # Steps 0..3; step 3 means three or more levels.
AREA_LABELS = {'yard': '장치 야드', 'temp': '임시 장치', 'temp_a': '임시 장치 A', 'temp_b': '임시 장치 B', 'stand': '반출대'}


def _area(source, rehandle):
    layout = source['layout']
    height, marked, cargo = {}, {}, set()
    for layer in source['layers']:
        for row in layer['rows']:
            for cell in row['cells']:
                if not cell['code']:
                    continue
                cargo.add(cell['code'])  # A multi-cell cargo is one entity however many columns it covers.
                for r in range(cell['row'], cell['row'] + cell.get('rowspan', 1)):
                    for c in range(cell['column'], cell['column'] + cell.get('colspan', 1)):
                        height[r, c] = max(height.get((r, c), 0), cell['level'])
                        if cell['code'] in rehandle:
                            marked.setdefault((r, c), set()).add(cell['code'])
    area_id = source.get('area_id', 'yard')
    rows = [dict(number=r, cells=[dict(row=r, column=c, height=height.get((r, c), 0),
            step=min(height.get((r, c), 0), STEP_CAP), rehandle=sorted(marked.get((r, c), ())))
            for c in range(1, layout['columns'] + 1)]) for r in range(1, layout['rows'] + 1)]
    return dict(area_id=area_id, label=source.get('area_label') or AREA_LABELS.get(area_id, area_id),
        column_labels=list(range(1, layout['columns'] + 1)), rows=rows, cargo_count=len(cargo),
        rehandle_cargo=sorted(cargo & rehandle))


def occupancy_map(stage, display_moves):
    """None without a configured layout. Rehandle cargo = stored moves whose phase is 재취급; never recomputed."""
    if not stage or not stage.get('layout'):
        return None
    rehandle = {m['cargo_id'] for m in display_moves if m['phase_label'] == '재취급'}
    areas = [_area(source, rehandle) for source in [stage, *stage.get('auxiliary_areas', [])]]
    return dict(areas=areas, rehandle_cargo=sorted({c for a in areas for c in a['rehandle_cargo']}),
                has_moves=bool(display_moves))
