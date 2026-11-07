"""Explain immutable proposals without calculating plans or execution facts."""
from copy import deepcopy

from harbor_web.models import Plan
from harbor_web.recommendation_view import explain_optimization
from harbor_web.yard_rows import place_label


def _location(value):
    if value is None:
        return None
    location = dict(value)
    if 'area_id' in location:
        # Read by rows like the yard plan: a 1×3 container fills its row, so only a narrower cargo needs its column.
        label = place_label(location, {'temp': '임시 장치', 'stand': '반출대'})
        if location['column'] != 1 or location.get('orientation', 0) != 0:
            label += f" · {location['column']}열" + (f" · {location['orientation']}°" if location.get('orientation', 0) else '')
        location['label'] = label
    else:
        location['label'] = f"{location['bay_id']} · {location['level']}단"
    return location


def _current(plan):
    result, payload = plan.result, plan.observation.payload
    request = result.get('request_snapshot') or {}
    target = request.get('cargo_code', plan.order.cargo.code)
    shapes = {s['cargo_id']: dict(s) for s in result.get('source_shapes', [])}
    observed = {}
    for item in payload.get('placements', []):
        observed[item['cargo_id']] = _location(item)
        shapes.setdefault(item['cargo_id'], dict(cargo_id=item['cargo_id'],
            footprint_revision=item.get('footprint_revision')))
    stacks = {b['bay_id']: list(b['cargo']) for b in payload.get('bays', [])}
    for bay, cargoes in stacks.items():
        for level, cargo in enumerate(cargoes, 1):
            observed[cargo] = _location(dict(bay_id=bay, level=level))
    incoming = result.get('incoming_shape')
    if incoming:
        shapes[incoming['cargo_id']] = dict(incoming)
    pins = dict(plan_id=plan.pk, order_version=plan.order_version,
        request=deepcopy(request), observation_id=plan.observation_id,
        source_id=plan.observation.source_id, source_revision=plan.observation.source_revision,
        condition_id=plan.condition_id, footprints=list(shapes.values()))
    routes = {}
    moves = result.get('moves', []) if result.get('status') == 'READY' else []
    outbound_index = next((i for i, m in enumerate(moves) if m['cargo_id'] == target), None)
    for index, move in enumerate(moves):
        cargo = move['cargo_id']
        phase = move.get('phase_label')
        if not phase:
            phase = ('반출' if cargo == target else '재취급' if outbound_index is not None
                     and index < outbound_index else '복귀' if outbound_index is not None else '이동')
        if move.get('source_placement'):
            source = _location(move['source_placement'])
            destination = _location(move['destination_placement'])
        else:
            source_stack = stacks.get(move['source'], [])
            level = source_stack.index(cargo) + 1 if cargo in source_stack else None
            source = _location(dict(bay_id=move['source'], level=level))
            destination_stack = stacks.setdefault(move['destination'], [])
            destination = _location(dict(bay_id=move['destination'], level=len(destination_stack) + 1))
            if cargo in source_stack:
                source_stack.remove(cargo)
            destination_stack.append(cargo)
        route = routes.setdefault(cargo, dict(cargo_id=cargo, kind='outbound-route',
            role='대상 반출' if cargo == target else '계획상 재취급', observed_source=observed.get(cargo),
            footprint_revision=move.get('footprint_revision', shapes.get(cargo, {}).get('footprint_revision')),
            shape=shapes.get(cargo), locations=[observed.get(cargo) or source], steps=[]))
        route['locations'].append(destination)
        route['steps'].append(dict(number=index + 1, phase=phase, source=source, destination=destination,
            primary_arm=move.get('primary_arm'), alternate_arms=list(move.get('alternate_arms', []))))
    if (request.get('kind', plan.order.kind) == 'INBOUND' and result.get('status') == 'READY'
            and result.get('recommendation')):
        destination = _location(result['recommendation'])
        routes[target] = dict(cargo_id=target, kind='intake-recommendation', role='입고 배치 추천',
            observed_source=observed.get(target), footprint_revision=(incoming or {}).get('footprint_revision'),
            shape=incoming, locations=[p for p in [observed.get(target), destination] if p], steps=[])
    for route in routes.values():
        route['route_label'] = ' → '.join(p['label'] for p in route['locations'])
    return dict(routes=list(routes.values()), pins=pins, move_count=len(moves),
        outbound_count=sum(s['phase'] == '반출' for r in routes.values() for s in r['steps']),
        rehandle_count=sum(any(s['phase'] == '재취급' for s in r['steps']) for r in routes.values()),
        restore_count=sum(s['phase'] == '복귀' for r in routes.values() for s in r['steps']),
          status=result.get('status', ''), comparison=None,
          optimization=explain_optimization(result.get('optimization')))


def explain_plan(plan):
    """Return current cargo routes and a same-order, pinned previous-plan comparison."""
    if plan is None:
        return None
    explanation = _current(plan)
    previous_id = plan.result.get('previous_plan_id')
    if type(previous_id) is not int or previous_id <= 0 or previous_id >= plan.pk:
        return explanation
    previous = Plan.objects.filter(pk=previous_id, order_id=plan.order_id).select_related(
        'observation', 'condition', 'order__cargo').first()
    if previous is None:
        return explanation
    before = _current(previous)
    old_routes = {r['cargo_id']: r for r in before['routes']}
    new_routes = {r['cargo_id']: r for r in explanation['routes']}
    changes, counts = [], dict(added=0, removed=0, route_changed=0, unchanged=0)
    labels = dict(added='추가', removed='제외', route_changed='경로 변경', unchanged='유지')
    for cargo in sorted(old_routes.keys() | new_routes.keys()):
        old, new = old_routes.get(cargo), new_routes.get(cargo)
        status = ('added' if old is None else 'removed' if new is None
                  else 'unchanged' if old == new else 'route_changed')
        counts[status] += 1
        changes.append(dict(cargo_id=cargo, status=status, label=labels[status], before=old, after=new))
    explanation['comparison'] = dict(previous_plan=previous, before_pins=before['pins'],
        after_pins=explanation['pins'], changes=changes, counts=counts)
    return explanation
