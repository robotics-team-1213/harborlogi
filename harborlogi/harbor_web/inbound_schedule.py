"""Inbound schedule (ADR 0031): deliveries from the scenario sheet 「입고 일정」, the rearrangement slots between them,
the automatic rearrangement gate and the operations page work plan rows.

A delivery is one Pinky arriving at one time with one or more containers; the container on top is stored first.
A rearrangement slot opens where a delivery's estimated end leaves at least MIN_GAP before the next arrival, and
once more after the last delivery. The durations are planning defaults to tune on site, not measurements.
"""
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
import re
from types import SimpleNamespace

from django.utils import timezone

INBOUND_PER_CONTAINER = timedelta(minutes=2)
REARRANGE_PER_MOVE = timedelta(minutes=2)
MIN_GAP = timedelta(minutes=10)
BUSY = 'INBOUND_SCHEDULE_BUSY'
HOLD_LABELS = {
    'AUTHORIZATION_OFF_OR_EXPIRED': '자동 허용 꺼짐', 'DAILY_LIMIT_REACHED': '하루 한도 도달',
    'OUTBOUND_PENDING': '반출 대기 중', 'OTHER_COMMAND_ACTIVE': '다른 작업 실행 중',
    'SPATIAL_OBSERVATION_REQUIRED': '격자 야드 현황 없음', 'SPATIAL_OBSERVATION_INVALID': '야드 현황을 읽을 수 없음',
    'OBSERVATION_NOT_CURRENT': '야드 현황이 오래됨', 'OBSERVATION_REVISION_CHANGED': '새 야드 현황 있음',
    'REARRANGE_NOT_WORTH': '옮길 만한 이득 없음', BUSY: '입고 시간대',
}

_EXCEL_EPOCH = date(1899, 12, 30)
_CLOCK = re.compile(r'(\d{1,2}):(\d{2})(?::(\d{2}))?')
_STAMP = re.compile(r'(\d{4})-(\d{2})-(\d{2})[ T](\d{1,2}):(\d{2})(?::(\d{2}))?')
_NUMBER = re.compile(r'\d{1,6}(?:\.\d{1,17})?')


def parse_time(text):
    """(date or None, time) from a 시각 cell, or None when it is not a time.

    Text cells are 9:05, 09:05, 09:05:30 or 2026-11-10 09:05. A spreadsheet that formats the cell as a time stores
    a fraction of a day (0.378...); one formatted as a date and time stores days since 1899-12-30 plus that fraction.
    """
    text = text.strip()
    if not text.isascii():  # \d also matches other scripts' digits
        return None
    match = _CLOCK.fullmatch(text)
    if match:
        hour, minute, second = int(match[1]), int(match[2]), int(match[3] or 0)
        return (None, time(hour, minute, second)) if hour < 24 and minute < 60 and second < 60 else None
    match = _STAMP.fullmatch(text)
    if match:
        try:
            return (date(int(match[1]), int(match[2]), int(match[3])),
                    time(int(match[4]), int(match[5]), int(match[6] or 0)))
        except ValueError:
            return None
    if not _NUMBER.fullmatch(text):
        return None
    days, seconds = divmod(round(float(text) * 86400), 86400)
    if 0 < days < 20000:  # 1954 or earlier: a typed hour such as 9, not a spreadsheet date
        return None
    clock = time(seconds // 3600, seconds // 60 % 60, seconds % 60)
    return (None, clock) if days == 0 else (_EXCEL_EPOCH + timedelta(days=days), clock)


@dataclass(frozen=True)
class Delivery:
    start: datetime
    end: datetime  # estimated end of storing it, one container at a time after any earlier delivery
    vehicle: str
    entries: tuple  # top first


@dataclass(frozen=True)
class Slot:
    start: datetime
    end: object  # the next arrival, or None after the last delivery
    gap: object  # timedelta from the estimated end to the next arrival, None after the last delivery


def deliveries(entries):
    """Entries (pk, expected_at, vehicle, stack_position) grouped by arrival and vehicle, in arrival order.

    Arrivals at the same time keep the order their rows were registered in (pk).
    """
    groups = {}
    for entry in sorted(entries, key=lambda e: (e.expected_at, e.pk)):
        groups.setdefault((entry.expected_at, entry.vehicle), []).append(entry)
    found, busy = [], None
    for (start, vehicle), items in groups.items():
        busy = max(busy or start, start) + INBOUND_PER_CONTAINER * len(items)
        found.append(Delivery(start, busy, vehicle, tuple(sorted(items, key=lambda e: e.stack_position))))
    return found


def slots(delivery_list):
    found = []
    for this, following in zip(delivery_list, delivery_list[1:] + [None]):
        if following is None:
            found.append(Slot(this.end, None, None))
        elif following.start - this.end >= MIN_GAP:
            found.append(Slot(this.end, following.start, following.start - this.end))
    return found


def active_entries():
    """The rows of the latest imported schedule, or [] when none was imported."""
    from harbor_web.models import ScheduledInbound
    latest = ScheduledInbound.objects.order_by('-start_id').values_list('start_id', flat=True).first()
    if latest is None:
        return []
    return list(ScheduledInbound.objects.filter(start_id=latest).select_related('order__cargo').order_by('pk'))


def _finished(entries):
    from harbor_web.models import Command
    return set(Command.objects.filter(order_id__in=[e.order_id for e in entries], state='COMPLETED')
               .values_list('order_id', flat=True))


def rearrange_gate(now, entries=None, finished=None):
    """(HOLD reason or None, move cap or None) for an automatic rearrangement starting at `now`.

    Without a schedule nothing changes. With one, a plan may start inside a slot, and inside a slot between two
    deliveries it may only take the moves that fit before the next arrival. Once every scheduled container is
    stored the schedule no longer holds anything.
    """
    entries = active_entries() if entries is None else entries
    if not entries:
        return None, None
    finished = _finished(entries) if finished is None else finished
    if all(entry.order_id in finished for entry in entries):
        return None, None
    for slot in slots(deliveries(entries)):
        if slot.start <= now and (slot.end is None or now < slot.end):
            if slot.end is None:
                return None, None
            cap = int((slot.end - now) / REARRANGE_PER_MOVE)
            return (None, cap) if cap >= 1 else (BUSY, None)
    return BUSY, None


def _clock(value, now):
    local, today = timezone.localtime(value), timezone.localtime(now).date()
    return local.strftime('%H:%M') if local.date() == today else local.strftime('%m-%d %H:%M')


def _vehicle(name):
    return f'핑키 {name}' if name.isdigit() else name


def _inbound_state(entry, plan, command, digest):
    """(state, tone, note) of one scheduled container: its command first, then its placement decision."""
    from harbor_web.command_models import ACTIVE_COMMAND_STATES
    from harbor_web.inbound_decision import decision_block, placement_decision
    from harbor_web.workspace import COMMAND_LABELS
    if command is not None:
        if command.state == 'COMPLETED':
            return '완료', 'ok', ''
        if command.state in ACTIVE_COMMAND_STATES:
            return '입고 중', 'ok', COMMAND_LABELS.get(command.state, '')
        return '보류', 'hold', COMMAND_LABELS.get(command.state, command.state)
    order = entry.order
    decision = placement_decision(plan) if plan else None
    if decision and plan.order_version == order.version:
        return '결정됨', 'ok', ''
    reason = decision_block(order, plan, queue_digest=digest)
    if reason is None:
        return '결정 대기', 'wait', ''
    if plan is None:
        return '계획 전', 'wait', reason
    if plan.result.get('status') != 'READY':
        return '보류', 'hold', reason
    return '재검토', 'hold', reason


def _slot_state(slot, now, plans, holds, authorized):
    """(state, tone, note, plan) of one rearrangement slot from the automatic plans and HOLD records inside it."""
    from harbor_web.command_models import ACTIVE_COMMAND_STATES

    def inside(at):
        return slot.start <= at and (slot.end is None or at < slot.end)
    if now < slot.start:
        return '예정', 'wait', '' if authorized else '자동 허용 꺼짐', None
    plan = next((p for p in plans if inside(p.created_at)), None)
    if plan is not None:
        command = getattr(plan, 'command', None)
        if command is not None and command.state == 'COMPLETED':
            return '완료', 'ok', '', plan
        if command is not None and command.state in ACTIVE_COMMAND_STATES:
            return '실행 중', 'ok', '', plan
        return '보류', 'hold', '실행 인계 안 됨', plan
    hold = next((h for h in holds if inside(h.created_at)), None)
    if hold is not None:
        reason = hold.detail.get('reason', '')
        if reason == 'REARRANGE_NOT_WORTH':
            return '할 일 없음', 'muted', HOLD_LABELS[reason], None
        return '보류', 'hold', HOLD_LABELS.get(reason, reason), None
    if slot.end is not None and now >= slot.end:
        return '실행 안 함', 'muted', '', None
    return '대기', 'wait', '실행기가 유휴일 때 검사' if authorized else '자동 허용 꺼짐', None


def work_plan(rows, *, observation, now, digest):
    """The operations page work plan: the latest schedule in time order, then the other listed plans.

    `rows` are the operations page's plan rows of the selected observation (one per order). A scheduled container
    keeps its row when it has one; otherwise its row comes from its latest plan without yard marks. Rows outside the
    schedule keep their place after it with no expected time.
    """
    from harbor_web.models import Audit, Command, Plan
    from harbor_web.rearrangement import HOLD_ACTION, current_authorization
    entries = active_entries()
    by_order = {row['plan'].order_id: row for row in rows}
    result = []
    for delivery in deliveries(entries):
        for entry in delivery.entries:
            row = by_order.pop(entry.order_id, None)
            plan = row['plan'] if row else Plan.objects.filter(order_id=entry.order_id).order_by('-pk').first()
            command = Command.objects.filter(order_id=entry.order_id).order_by('-pk').first()
            state, tone, note = _inbound_state(entry, plan, command, digest)
            if row is None:
                moves = plan.result.get('moves', []) if plan and plan.result.get('status') == 'READY' else []
                row = dict(plan=plan, steps=[], touched=[], rehandles=[], target=None,
                           primary_arms=sorted({m['primary_arm'] for m in moves if m.get('primary_arm')}), alternate_arms=[],
                           summary='다른 야드 현황의 계획' if plan else '아직 계획 없음')
            result.append(dict(row, key=f'inbound-{entry.pk}', kind='inbound', order=entry.order, title=entry.order.cargo.code,
                               when=entry.expected_at, when_text=_clock(entry.expected_at, now),
                               when_note=f'{_vehicle(entry.vehicle)} · {entry.stack_position} / {entry.delivery_size}',
                               state=state, tone=tone, note=note))
        result[-1]['delivery_end'] = delivery.end
    if entries:
        authorized = current_authorization(now) is not None
        plans = list(Plan.objects.filter(order__kind='REARRANGE', created_at__gte=min(e.expected_at for e in entries))
                     .select_related('order', 'command').order_by('-pk'))
        plans = [p for p in plans if p.result.get('rearrangement', {}).get('automatic')]
        holds = list(Audit.objects.filter(action=HOLD_ACTION, created_at__gte=min(e.expected_at for e in entries)).order_by('-pk'))
        for number, slot in enumerate(slots(deliveries(entries)), start=1):
            state, tone, note, plan = _slot_state(slot, now, plans, holds, authorized)
            gap = f'입고 사이 빈 시간 {int(slot.gap.total_seconds() // 60)}분' if slot.gap is not None else '모든 입고 뒤'
            row = next((r for r in rows if plan and r['plan'].pk == plan.pk), None) or dict(
                plan=plan, steps=[], touched=[], rehandles=[], target=None, primary_arms=[], alternate_arms=[],
                summary='예정 시각의 야드 현황으로 계획합니다' if plan is None else f'이동 {len(plan.result.get("moves", []))}')
            if plan is not None:
                by_order.pop(plan.order_id, None)
            item = dict(row, key=f'slot-{number}', kind='rearrange', order=plan.order if plan else None, title='자동 재배치',
                        when=slot.start, when_text=_clock(slot.start, now), when_note=gap, state=state, tone=tone, note=note)
            index = next((i for i, r in enumerate(result) if r.get('delivery_end') == slot.start), len(result) - 1)
            result.insert(index + 1, item)
    for row in rows:
        if row['plan'].order_id in by_order:
            order, note = row['plan'].order, ''
            if order.kind == 'INBOUND':  # listed rows have no command yet (operations_context)
                state, tone, note = _inbound_state(SimpleNamespace(order=order), row['plan'], None, digest)
            else:
                state, tone = ('보류', 'hold') if row['status'] == 'HOLD' else ('재검토', 'hold') if row['stale'] else ('계획됨', 'ok')
            result.append(dict(row, key=f'plan-{row["plan"].pk}', kind=order.kind.lower(), order=order,
                               title=order.cargo.code if order.cargo_id else '재배치', when=None, when_text='—', when_note='',
                               state=state, tone=tone, note=note))
    return result
