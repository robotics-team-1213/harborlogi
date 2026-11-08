"""Per-container event history assembled from existing records (ADR 0019).

No new table and no inferred completion: every row names the record it came from, and dwell time is
only the span between observations that show the container, never an estimate.
"""
from django.core.exceptions import ValidationError

from harbor_web import services
from harbor_web.command_models import EvidenceSubmission
from harbor_web.models import (
    Command, CommandFact, DepartureIntent, FieldConfirmation, FieldEpisode, Observation, Order, Plan,
)
from harbor_web.workspace import COMMAND_LABELS

# shortcut: scans the newest 200 observations and plans; index by cargo when a yard keeps more history.
RECENT = 200
KINDS = {'INBOUND': '입고', 'OUTBOUND': '반출', 'REARRANGE': '재배치'}


def _where(observation, code):
    """Area, bay or holder that shows this container in one observation; None when absent."""
    snapshot = services.snapshot(observation)
    for placement in getattr(snapshot, 'placements', ()):
        if placement.cargo_id == code:
            return placement.area_id
    for bay in getattr(snapshot, 'bays', ()):
        if code in bay.cargo:
            return bay.bay_id
    for holding in snapshot.holdings:
        if holding.cargo_id == code:
            return '보유 중 · ' + (holding.holder_id or '보유자 미상')
    return None


def _sightings(cargo):
    rows, unreadable = [], 0
    for observation in Observation.objects.select_related('condition').order_by('-observed_at', '-pk')[:RECENT]:
        try:
            place = _where(observation, cargo.code)
        except (ValueError, ValidationError):
            unreadable += 1
            continue
        if place is not None:
            rows.append((observation, place))
    return rows[::-1], unreadable


def cargo_history(cargo):
    events = [dict(at=cargo.created_at, kind='등록', text='화물 등록', source=f'화물 #{cargo.pk}')]
    orders = list(Order.objects.filter(cargo=cargo))
    for order in orders:
        events.append(dict(at=order.created_at, kind='요청', source=f'요청 #{order.pk}',
                           text=f'{KINDS.get(order.kind, order.kind)} 요청 · 우선순위 {order.priority_group}',
                           href=f'/requests/{order.pk}/'))
    for plan in Plan.objects.select_related('order').order_by('-pk')[:RECENT]:
        status = plan.result.get('status')
        moves = plan.result.get('moves') or []
        if plan.order.cargo_id == cargo.pk:
            text = f'계획 검토 {status} · 이동 {len(moves)}회'
        elif any(move.get('cargo_id') == cargo.code for move in moves):
            text = f'다른 화물(요청 #{plan.order_id}) 계획의 재취급 대상 · {status}'
        else:
            continue
        events.append(dict(at=plan.created_at, kind='계획', text=text, source=f'Plan #{plan.pk}',
                           href=f'/plans/{plan.pk}/'))
    commands = list(Command.objects.filter(plan__order__cargo=cargo).select_related('plan'))
    for command in commands:
        label = COMMAND_LABELS.get(command.state, command.state)
        events.append(dict(at=command.created_at, kind='실행 지시', source=f'Plan #{command.plan_id} 지시',
                           text=f'실행 지시 · 현재 {label}' + (f' · ROSY {command.mission_id}' if command.mission_id else '')))
    keys = {command.key: command for command in commands}
    for fact in CommandFact.objects.filter(command_key__in=keys).order_by('read_at'):
        status = (fact.job or {}).get('status', '상태 미상')
        events.append(dict(at=fact.read_at, kind='ROSY 조회', source='ROSY 공개 조회',
                           text=f'작업 {status}' + (' · 물리 receipt 있음' if fact.receipt else '')))
    for evidence in EvidenceSubmission.objects.filter(command__in=commands):
        events.append(dict(at=evidence.received_at, kind='업무 근거', source=f'근거 제출 #{evidence.pk}',
                           text='내려놓기·퇴피 근거 접수(대조 전)'))
    for confirmation in FieldConfirmation.objects.filter(episode__order__in=orders).select_related('episode'):
        events.append(dict(at=confirmation.observed_at, kind='현장 확인', source=f'현장 확인 #{confirmation.pk}',
                           text=f'{confirmation.episode.vehicle_id} 현장 확인 {confirmation.outcome}'))
    for departure in DepartureIntent.objects.filter(cargo=cargo):
        events.append(dict(at=departure.created_at, kind='출발', source=f'출발 의도 #{departure.pk}',
                           text=f'{departure.vehicle_id} 출발 의도 기록'))
    sightings, unreadable = _sightings(cargo)
    for observation, place in sightings:
        events.append(dict(at=observation.observed_at, kind='관측', source=f'관측 #{observation.pk} · {observation.source_id}',
                           text=f'{place}에서 관측 ({observation.provenance})'))
    events.sort(key=lambda event: event['at'])
    dwell = None
    if sightings:
        first, last = sightings[0][0], sightings[-1][0]
        dwell = dict(first=first.observed_at, last=last.observed_at, span=last.observed_at - first.observed_at,
                     count=len(sightings))
    return dict(events=events, dwell=dwell, unreadable=unreadable,
                open_episodes=FieldEpisode.objects.filter(order__in=orders, state='OPEN').count())
