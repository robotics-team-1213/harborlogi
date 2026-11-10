"""Read-only operations facts, keeping observations, proposals and handoffs separate."""
from django.db.models import Count, Max, Min
from django.utils import timezone

from harbor_web import services
from harbor_web.models import Audit, Command, Observation, Order, Plan
from harbor_web.workspace import COMMAND_LABELS
from harbor_web.yard_rows import DEFAULT_SLOT, place_context, place_label, slot_key, yard_rows

ATTENTION_STATES = ('UNKNOWN', 'DISPATCHING', 'CANCEL_PENDING', 'BLOCKED')
HANDOFF_REASONS = {
    'UNKNOWN': 'ROSY 응답을 확인하지 못했습니다. 자동으로 다시 보내지 않습니다.',
    'DISPATCHING': '제출 중이거나 응답을 확인하지 못했습니다.',
    'CANCEL_PENDING': '취소 결과와 실제 정지를 대조해야 합니다.',
    'BLOCKED': '설치 · 관측 · 복구 조건 때문에 인계가 보류됐습니다.',
}


def _inventory(observation):
    if observation is None:
        return dict(known=False, cargo_count=None, occupied=None, capacity=None,
                    storage_count=None, temporary_count=None, outbound_count=None,
                    holding_count=None, areas=[])
    policy = observation.condition.payload
    temporary = set(policy['temporary_bays'])
    outbound = {policy['outbound_stand'], *policy.get('extra_outbound_stands', [])}
    spatial = observation.payload.get('schema') == 'grid-placement-v1'
    snapshot = services.snapshot(observation)
    areas = []
    presentation = services.area_presentation(policy) if spatial else {}
    slot = policy.get('slot_cells') if spatial else None  # fixed 1×3 slots: count containers, not cells (KAN-115)
    if spatial:
        for area in snapshot.areas:
            placements = [p for p in snapshot.placements if p.area_id == area.area_id]
            areas.append(dict(id=area.area_id, cargo_count=len(placements),
                occupied=len(placements) if slot else sum(len(snapshot.cells(p)) for p in placements),
                capacity=area.rows * (area.columns // slot) * area.levels if slot else area.rows * area.columns * area.levels))
    else:
        areas = [dict(id=b.bay_id, cargo_count=len(b.cargo), occupied=len(b.cargo),
                      capacity=b.capacity) for b in snapshot.bays]
    for area in areas:
        area['label'] = presentation.get(area['id'], {}).get('label', area['id'])
        area['role'] = '임시 장치' if area['id'] in temporary else '반출대' if area['id'] in outbound else '보관'
    counted = [a for a in areas if a['role'] == '보관'] if slot else areas  # with slots the rate is the yard's own
    occupied, capacity = sum(a['occupied'] for a in counted), sum(a['capacity'] for a in counted)
    return dict(known=True, spatial=spatial, cargo_count=sum(a['cargo_count'] for a in areas),
        occupied=occupied, capacity=capacity, slots=bool(slot),
        occupancy_percent=round(occupied * 100 / capacity) if capacity else None,
        unit='자리' if slot else '격자 칸' if spatial else '적재 자리', areas=areas,
        auxiliary=[a for a in areas if a['role'] != '보관'] if slot else [],
        storage_count=sum(a['cargo_count'] for a in areas if a['role'] == '보관'),
        temporary_count=sum(a['cargo_count'] for a in areas if a['role'] == '임시 장치'),
        outbound_count=sum(a['cargo_count'] for a in areas if a['role'] == '반출대'),
        holding_count=len(snapshot.holdings), holdings=snapshot.holdings)


def _cargo_label(order):
    return order.cargo.code if order.cargo else f'재배치 요청 #{order.pk}'  # REARRANGE has no cargo (design 32)


def move_phases(plan, order):
    """Phase of each stored move: its own label, else its order around the target cargo."""
    moves = plan.result.get('moves', [])
    code = order.cargo and order.cargo.code  # REARRANGE has no cargo (design 32)
    target = next((i for i, m in enumerate(moves) if m['cargo_id'] == code), -1)
    return [m.get('phase_label') or ('입고' if order.kind == 'INBOUND' else
            '재취급' if i < target else '반출' if i == target else '복귀') for i, m in enumerate(moves)]


THROUGHPUT_WINDOW = timezone.timedelta(hours=24)
THROUGHPUT_MIN_SPAN = timezone.timedelta(minutes=10)  # shorter spans turn two completions into absurd hourly rates


def _throughput(done, now):
    """Interval rate between the first and last completion Harbor recorded in the window.
    The time is the audit row written with the COMPLETED save (commands._save), not the robot's finish time."""
    stamps = dict(Audit.objects.filter(object_type='Command', action='command.completed',
                                       created_at__gte=now - THROUGHPUT_WINDOW, created_at__lte=now,
                                       object_id__in=[str(c.pk) for c in done])
                  .values_list('object_id').annotate(at=Min('created_at')))  # first time Harbor recorded it
    times = sorted(stamps.values())
    span = times[-1] - times[0] if times else timezone.timedelta(0)
    # count - 1 intervals lie between the first and last completion
    return dict(window_hours=int(THROUGHPUT_WINDOW.total_seconds() // 3600), count=len(times),
                span_minutes=int(span.total_seconds() // 60),
                per_hour=round((len(times) - 1) / (span.total_seconds() / 3600), 1) if span >= THROUGHPUT_MIN_SPAN else None)


def _actual(commands, now):
    """COMPLETED is set only after the ARM_TRANSFER ledger finish() (commands.advance_command); legacy
    cell stages never create a Command. The binding is the digest-checked ledger payload."""
    latest = {}
    for c in commands:  # newest first: a newer completion of the same order replaces, not adds
        if c.state == 'COMPLETED':
            latest.setdefault(c.order_id, c)
    done = list(latest.values())
    kinds = [c.order.kind for c in done]
    rehandles, unmatched = 0, []
    for c in done:
        if c.order.kind != 'OUTBOUND':
            continue
        index = next((i for i, m in enumerate(c.binding.get('moves', []))
                      if m.get('cargo_id') == c.binding.get('outbound_id')), None)
        if index is None:
            unmatched.append(c)  # No outbound move to count against: never report it as zero rehandles.
        else:
            rehandles += index
    from harbor_web.human_evidence import attested_final_commands
    human = len(attested_final_commands([c.pk for c in done]))  # 완료 근거가 기명 사람 확인(ADR 0023)인 건
    return dict(completed=len(done), human_attested=human, inbound=kinds.count('INBOUND'), outbound=kinds.count('OUTBOUND'),
                rearrange=kinds.count('REARRANGE'), outbound_rehandle_moves=None if unmatched else rehandles,
                unmatched=unmatched, throughput=_throughput(done, now))


def operations_context(observation, *, now=None):
    """Summarize one exact observation pin; registered orders and handoffs are global."""
    now = timezone.now() if now is None else now
    inventory = _inventory(observation)
    freshness = dict(stale=None, superseded=None, age_seconds=None)
    if observation is not None:
        age = now - observation.observed_at
        freshness = dict(age_seconds=int(age.total_seconds()),
            stale=not timezone.timedelta(0) <= age <= services.constraints(observation.condition.payload).max_observation_age,
            superseded=Observation.objects.filter(source_id=observation.source_id, pk__gt=observation.pk).exists())
    counts = dict(Order.objects.values_list('kind').annotate(count=Count('pk')))
    commands = list(Command.objects.select_related('plan__observation__condition', 'order__cargo').order_by('-pk'))
    commanded = {c.order_id for c in commands}
    handoffs = [dict(command=c, plan=c.plan, label=COMMAND_LABELS.get(c.state, c.state), cargo=_cargo_label(c.order))
                for c in commands]
    latest_ids = Plan.objects.values('order_id').annotate(latest_id=Max('pk')).values('latest_id')
    latest = Plan.objects.filter(pk__in=latest_ids).select_related(
        'order__cargo', 'observation__condition', 'condition').order_by('-pk')
    rows, rehandled = [], set()
    phases = {'재취급': 0, '반출': 0, '복귀': 0, '입고': 0}
    ready = hold = excluded = move_count = outbound_plans = outbound_rehandles = rehandle_plans = 0
    digest = services.queue_digest()
    labels, slot = place_context(observation.condition.payload) \
        if observation is not None and observation.payload.get('schema') == 'grid-placement-v1' else ({}, None)
    for plan in latest:
        if plan.order_id in commanded or observation is None or plan.observation_id != observation.pk or plan.condition_id != observation.condition_id:
            continue
        stale = (freshness['stale'] or freshness['superseded'] or
                 plan.order_version != plan.order.version or plan.queue_digest != digest)
        status = plan.result['status']
        included = status == 'READY' and not stale
        moves = plan.result.get('moves', [])
        primary = sorted({m['primary_arm'] for m in moves if m.get('primary_arm')})
        alternate = sorted({a for m in moves for a in m.get('alternate_arms', [])} - set(primary))
        rows.append(dict(plan=plan, stale=stale, included=included, status=status, cargo=_cargo_label(plan.order),
                         primary_arms=primary, alternate_arms=alternate, **_job(plan, labels, slot)))
        if status == 'HOLD':
            hold += 1
        elif not included:
            excluded += 1
        if not included:
            continue
        ready += 1
        move_count += len(moves)
        outbound = plan.order.kind == 'OUTBOUND'
        outbound_plans += outbound
        code = plan.order.cargo and plan.order.cargo.code
        rehandle_plans += outbound and any(phase == '재취급' for phase in move_phases(plan, plan.order))
        for move, phase in zip(moves, move_phases(plan, plan.order)):
            phases[phase] = phases.get(phase, 0) + 1
            outbound_rehandles += outbound and phase == '재취급'
            if move['cargo_id'] != code:
                rehandled.add(move['cargo_id'])
    # Existing fields only (ADR 0020): no new state interpretation, no resend action.
    attention = []
    if observation is None:
        attention.append(dict(label='관측 없음', count=1, action='관측 연결 상태 확인', href='/inventory/'))
    if freshness['stale']:
        attention.append(dict(label='관측 유효 시간 밖', count=1, action='관측 선택', href='#observation-select'))
    if freshness['superseded']:
        attention.append(dict(label='같은 출처에 새 관측 있음', count=1, action='관측 선택', href='#observation-select'))
    if hold:
        attention.append(dict(label='HOLD 계획', count=hold, action='현재 검토 보기', href='#proposal-title'))
    if excluded and not (freshness['stale'] or freshness['superseded']):  # stale pin already explains them
        attention.append(dict(label='재검토로 이동 집계 제외', count=excluded, action='현재 검토 보기', href='#proposal-title'))
    for c in commands:
        if c.state in ATTENTION_STATES:
            attention.append(dict(label=f"전체 인계 · {COMMAND_LABELS[c.state]} · {_cargo_label(c.order)}", count=1,
                                  action='원래 작업 대조', href=f'/work/{c.pk}/'))
    actual = _actual(commands, now)
    for c in actual['unmatched']:
        attention.append(dict(label=f"완료 인계 · 반출 이동 대조 불가 · {_cargo_label(c.order)}", count=1,
                              action='원래 작업 대조', href=f'/work/{c.pk}/'))
    targets = {row['target'] for row in rows if row['target']}
    yard = yard_rows(observation, targets=targets, rehandles={c for row in rows for c in row['rehandles']})
    return dict(observation=observation, inventory=inventory, freshness=freshness, attention=attention,
        attention_view=[_attention_view(item, observation, commands) for item in attention], yard=yard,
        attention_total=sum(item['count'] for item in attention),
        orders=dict(inbound=counts.get('INBOUND', 0), outbound=counts.get('OUTBOUND', 0), scope='전체 등록 지시'),
        workload=dict(plans=rows, ready_count=ready, hold_count=hold, excluded_count=excluded,
            rehandled_cargo_count=len(rehandled) if observation else None,
            move_count=move_count if observation else None, phase_counts=phases,
            rehandled_cargo=sorted(rehandled), outbound_plan_count=outbound_plans if observation else None,
            outbound_rehandle_moves=outbound_rehandles if observation else None,
            outbound_rehandle_plans=rehandle_plans if observation else None,
            rehandle_per_outbound=round(outbound_rehandles / outbound_plans, 1) if observation and outbound_plans else None),
        handoffs=handoffs, actual=actual)


def _job(plan, labels, slot=None):
    """Steps, marks and touched slots of one stored plan; read from its moves, never recomputed."""
    moves = plan.result.get('moves', []) if plan.result.get('status') == 'READY' else []
    phases = move_phases(plan, plan.order)
    target = plan.order.cargo.code if plan.order.cargo_id and plan.order.kind == 'OUTBOUND' else None
    steps, touched, counts = [], [], {}
    for number, (move, phase) in enumerate(zip(moves, phases), start=1):
        source, destination = move.get('source_placement'), move.get('destination_placement')
        steps.append(dict(number=number, phase=phase, cargo=move['cargo_id'], arm=move.get('primary_arm'),
            source=place_label(source, labels, slot) if 'source_placement' in move else move.get('source', '—'),
            destination=place_label(destination, labels, slot) if 'destination_placement' in move else move.get('destination', '—')))
        counts[phase] = counts.get(phase, 0) + 1
        for placement in (source, destination):
            if placement and phase in ('재취급', '반출', '입고'):
                key = slot_key(placement, slot or DEFAULT_SLOT)
                if key not in touched:
                    touched.append(key)
    summary = ' · '.join(f'{phase} {counts[phase]}' for phase in ('재취급', '반출', '복귀', '입고') if counts.get(phase))
    return dict(steps=steps, target=target, touched=touched,
                rehandles=sorted({s['cargo'] for s in steps if s['phase'] == '재취급'}),
                summary=summary or ('이동 없음' if plan.result.get('status') == 'READY' else '보류 · 부분 이동 없음'))


def _attention_view(item, observation, commands):
    """One line per item: a short status, what it is about, why it matters (ADR 0020 facts only)."""
    label = item['label']
    if label == '관측 없음':
        return dict(item, chip='현황 없음', tone='unknown', what='야드 현황이 없습니다', action='현황 연결 확인',
                    why='계획에는 출처 · 시각 · 설치 기준이 있는 야드 현황이 필요합니다.')
    if label == '관측 유효 시간 밖':
        return dict(item, chip='현황 만료', tone='hold', what=f'야드 현황 #{observation.pk}', action='현황 선택',
                    why='유효 시간이 지나 이 현황으로 낸 계획은 실행 근거가 되지 않습니다.')
    if label == '같은 출처에 새 관측 있음':
        return dict(item, chip='새 현황 있음', tone='new', what=f'#{observation.pk}보다 최근 야드 현황', action='현황 선택',
                    why='이 현황에 고정된 계획은 그대로 둡니다. 새 현황으로 바꿔 검토하세요.')
    if label == 'HOLD 계획':
        return dict(item, chip='HOLD 계획', tone='hold', what=f'보류된 계획 {item["count"]}건',
                    why='부분 이동은 만들지 않습니다. 보류 이유는 현재 검토 표에서 확인합니다.')
    if label == '재검토로 이동 집계 제외':
        return dict(item, chip='재검토 필요', tone='hold', what=f'계획 {item["count"]}건',
                    why='검토 뒤 지시나 대기열이 바뀌어 예상 이동 합계에서 뺐습니다.')
    if label.startswith('전체 인계 · '):
        _, state, cargo = label.split(' · ', 2)
        command = next((c for c in commands if item['href'] == f'/work/{c.pk}/'), None)
        return dict(item, chip=state, tone='unknown', what=f'{cargo} 인계 #{command.pk}' if command else cargo,
                    why=HANDOFF_REASONS.get(command.state if command else '', '원래 작업의 상태와 근거를 확인하세요.'))
    if label.startswith('완료 인계 · '):
        return dict(item, chip='대조 불가', tone='danger', what=label.rsplit(' · ', 1)[-1],
                    why='완료 기록에서 반출 이동을 찾지 못해 재취급 이동을 세지 않았습니다.')
    return dict(item, chip=label, tone='unknown', what='', why='')
