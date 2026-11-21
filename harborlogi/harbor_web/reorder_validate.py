"""Read-only POST /outbound/validate/ endpoint (WP11): simulate a candidate outbound order without saving."""
import json

from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from harbor.sequencing import evaluate_order
from harbor_web.auth import require_roles
from harbor_web.models import Command, Order
from harbor_web.order_queue import TIERS, open_outbound_orders, tier_of
from harbor_web.plan_service import _outbound_schedule
from harbor_web.presentation import reason_text
from harbor_web.site_config import spatial_policy, spatial_snapshot
from harbor_web.views import parse_entries, parse_lanes, selected_observation

RUNNING_STATES = {'DISPATCHING', 'RECONCILED'}


def _parse_request_lanes(request):
    content_type = request.content_type or ''
    if 'application/json' in content_type:
        try:
            body = json.loads(request.body.decode('utf-8') or '{}')
        except (UnicodeDecodeError, ValueError):
            return None
        if isinstance(body, dict) and isinstance(body.get('lanes'), list):
            if all(isinstance(item, str) for item in body['lanes']):
                return parse_lanes(body['lanes'])
            lanes = []
            for item in body['lanes']:
                if not isinstance(item, dict) or not isinstance(item.get('orders'), list):
                    return None
                parsed = []
                for entry in item['orders']:
                    if not isinstance(entry, dict):
                        return None
                    oid, ver = entry.get('id'), entry.get('version')
                    if type(oid) is not int or type(ver) is not int:
                        return None
                    parsed.append((oid, ver))
                lanes.append(dict(fixed=bool(item.get('fixed', True)), orders=parsed))
            return lanes or None
        if isinstance(body, dict) and isinstance(body.get('order'), list):
            entries = parse_entries(','.join(str(x) for x in body['order']))
            if entries is None:
                return None
            return [dict(fixed=True, orders=entries), dict(fixed=True, orders=[]), dict(fixed=True, orders=[])]
        return None
    if 'lane' in request.POST:
        return parse_lanes(request.POST.getlist('lane'))
    if 'order' in request.POST:
        entries = parse_entries(request.POST.get('order', ''))
        if entries is not None:
            return [dict(fixed=True, orders=entries), dict(fixed=True, orders=[]), dict(fixed=True, orders=[])]
    return None


def _response(*, ok, reasons=(), moves=None, rehandles=None, items=(), missing=(), current=None, status=200):
    return JsonResponse(
        dict(
            ok=ok,
            reasons=list(reasons),
            moves=moves,
            rehandles=rehandles,
            items=list(items),
            missing=list(missing),
            current=current,
        ),
        status=status,
    )


def _evaluate_codes(snapshot, policy, codes, *, now, schedule):
    scenario = evaluate_order(snapshot, policy, codes, now=now, schedule=schedule, group=1)
    items = []
    reasons = []
    for step in scenario.steps:
        step_ok = step.status == 'READY'
        message = '' if step_ok else f'{step.cargo_id}: {reason_text(step.reason)}'
        if message:
            reasons.append(message)
        items.append(dict(code=step.cargo_id, ok=step_ok, moves=step.move_count, rehandles=step.rehandle_count, reason=message))
    return scenario, items, reasons


@require_roles('operator')
@require_http_methods(['POST'])
def outbound_validate(request):
    lanes = _parse_request_lanes(request)
    if lanes is None or len(lanes) != len(TIERS):
        return _response(ok=False, reasons=['반출 순서 형식이 올바르지 않습니다.'], status=400)

    open_orders = {
        order.pk: order
        for order in open_outbound_orders().select_related('cargo').order_by('priority_group', 'sequence', 'pk')
    }
    submitted = [entry for lane in lanes for entry in lane['orders']]
    submitted_ids = [oid for oid, _ in submitted]
    if len(submitted_ids) != len(set(submitted_ids)) or set(submitted_ids) != set(open_orders):
        return _response(ok=False, reasons=['반출 대기열이 바뀌었습니다. 최신 순서를 다시 불러와 검증하세요.'], status=409)
    for oid, version in submitted:
        if open_orders[oid].version != version:
            return _response(ok=False, reasons=['반출 대기열이 바뀌었습니다. 최신 순서를 다시 불러와 검증하세요.'], status=409)

    running_ids = set(
        Command.objects.filter(order_id__in=open_orders, state__in=RUNNING_STATES).values_list('order_id', flat=True)
    )
    seen_non_running = False
    for oid in submitted_ids:
        if oid in running_ids:
            if seen_non_running:
                return _response(ok=False, reasons=['실행 중인 반출 작업보다 앞에 둘 수 없습니다.'])
        else:
            seen_non_running = True

    observation = selected_observation(request, default_latest=True)
    if observation is None:
        return _response(ok=False, reasons=['야드 현황이 없어 순서를 검증할 수 없습니다.'])
    if observation.payload.get('schema') != 'grid-placement-v1':
        return _response(ok=False, reasons=['공간 배치 야드 현황이 없어 순서를 검증할 수 없습니다.'])

    snapshot = spatial_snapshot(observation)
    policy = spatial_policy(observation.condition.payload)
    placed = {p.cargo_id for p in snapshot.placements}
    codes = tuple(open_orders[oid].cargo.code for oid in submitted_ids if open_orders[oid].cargo.code in placed)
    missing = [open_orders[oid].cargo.code for oid in submitted_ids if open_orders[oid].cargo.code not in placed]
    if not codes:
        return _response(ok=False, reasons=['현재 야드 현황에 있는 반출 화물이 없습니다.'], missing=missing)

    now = timezone.now()
    base_schedule = {
        code: rank
        for code, rank in _outbound_schedule().items()
        if code not in codes
    }
    # Assign explicit positions across the three submitted lanes so tie/priority checks match the edited order.
    proposed_schedule = dict(base_schedule)
    for group, lane in enumerate(lanes, start=1):
        for pos, (oid, _) in enumerate(lane['orders']):
            code = open_orders[oid].cargo.code
            if code in placed:
                proposed_schedule[code] = (group, pos)

    try:
        scenario, items, reasons = _evaluate_codes(snapshot, policy, codes, now=now, schedule=proposed_schedule)
    except ValueError:
        return _response(ok=False, reasons=['설치 조건을 확인할 수 없어 순서를 검증할 수 없습니다.'])

    current_codes = tuple(o.cargo.code for o in open_orders.values() if o.cargo.code in placed)
    current_summary = None
    if current_codes:
        cur_schedule = dict(base_schedule)
        for pos, o in enumerate(o for o in open_orders.values() if o.cargo.code in placed):
            cur_schedule[o.cargo.code] = (tier_of(o.priority_group), o.sequence)
        try:
            cur_scenario, _, _ = _evaluate_codes(snapshot, policy, current_codes, now=now, schedule=cur_schedule)
            current_summary = dict(ok=cur_scenario.feasible, moves=cur_scenario.total_moves, rehandles=cur_scenario.rehandles)
        except ValueError:
            current_summary = None

    return _response(
        ok=scenario.feasible,
        reasons=reasons,
        moves=scenario.total_moves,
        rehandles=scenario.rehandles,
        items=items,
        missing=missing,
        current=current_summary,
    )
