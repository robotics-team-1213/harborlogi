"""Read-only task presentation. Preview placements never change observed or executed state."""
from copy import deepcopy

from django.db.models import Q
from django.utils import timezone

from harbor_web import commands, forms, services
from harbor_web.models import Audit, Command, Observation, Plan
from harbor_web.yard_view import yard_view, spatial_yard_view
from harbor_web.yard_occupancy import occupancy_map
from harbor_web.yard_rows import yard_rows
from harbor_web.yard_scene import present_stage, scene_for


COMMAND_LABELS = {
    'PREPARED': '제안 검토 대기', 'DISPATCHING': '제출 대조 필요', 'UNKNOWN': '결과 불명',
    'CANCEL_PENDING': '취소 대조 필요', 'BLOCKED': '인계 보류', 'RECONCILED': '원본 대조됨',
}


def plan_changes(order, plan, *, current_queue_digest=None):
    if not plan:
        return []
    changes = []
    if current_queue_digest is None:
        current_queue_digest = services.queue_digest()
    if order.version != plan.order_version or plan.queue_digest != current_queue_digest:
        changes.append(dict(code='PLAN_QUEUE_CHANGED', message='검토 후 업무 지시가 변경되었습니다. 최신 지시로 계획을 다시 검토하세요.'))
    if Observation.objects.filter(source_id=plan.observation.source_id, pk__gt=plan.observation_id).exists():
        changes.append(dict(code='OBSERVATION_REVISION_CHANGED', message='같은 출처에 새 관측이 있습니다. 아래 그림은 계획에 고정된 이전 원본입니다.'))
    age = timezone.now() - plan.observation.observed_at
    if not timezone.timedelta(0) <= age <= services.constraints(plan.condition.payload).max_observation_age:
        changes.append(dict(code='OBSERVATION_NOT_FRESH', message='계획 기준 관측의 유효 시간이 지났거나 시각을 확인할 수 없습니다. 최신 원본이 필요합니다.'))
    return changes


def _yard_stages(order, plan, source):
    if not source:
        return [], []
    if source.payload.get('schema') == 'grid-placement-v1':
        return spatial_stages(order, source, plan.result['moves'] if plan and plan.result['status'] == 'READY' else [])
    target = order.cargo.code
    blockers = set()
    for bay in source.payload['bays']:
        if target in bay['cargo']:
            blockers.update(bay['cargo'][bay['cargo'].index(target) + 1:])
    policy = source.condition.payload
    bays = deepcopy(source.payload['bays'])

    def stage(index, phase, label, move=None):
        return present_stage(dict(yard_view(bays, policy, target=target, blockers=blockers), index=index, phase=phase, label=label, move=move,
                    panel_id=f'yard-{order.pk}-{index}'), policy)

    stages = [stage(0, '관측', '관측한 배치')]
    moves = plan.result['moves'] if plan and plan.result['status'] == 'READY' else []
    outbound_index = next((i for i, m in enumerate(moves) if m['cargo_id'] == target), -1)
    display_moves = []
    for i, move in enumerate(moves):
        phase = move.get('phase_label') or ('재취급' if i < outbound_index else '반출' if i == outbound_index else '복귀')
        by_id = {b['bay_id']: b for b in bays}
        display = dict(move, number=i + 1, phase_label=phase,
                       source_level=len(by_id[move['source']]['cargo']), destination_level=len(by_id[move['destination']]['cargo']) + 1)
        display_moves.append(display)
        by_id[move['source']]['cargo'].remove(move['cargo_id'])
        by_id[move['destination']]['cargo'].append(move['cargo_id'])
        stages.append(stage(i + 1, phase, f'{i + 1}단계 후 예상 · {phase}', display))
    return stages, display_moves


def spatial_stages(order, source, moves):
    from harbor.spatial import Placement
    current = services.spatial_snapshot(source)
    blockers = {m['cargo_id'] for m in moves if m['phase_label'] == '재취급'}

    def stage(index, phase, label, move=None):
        view = spatial_yard_view(current, target=order.cargo.code, blockers=blockers)
        return present_stage(dict(view, index=index, phase=phase, label=label, move=move, panel_id=f'yard-{order.pk}-{index}'), source.condition.payload)

    stages, display_moves = [stage(0, '관측', '관측한 배치')], []
    for i, move in enumerate(moves):
        source_placement = Placement(**move['source_placement'])
        destination = Placement(**move['destination_placement'])
        actual = next((placed for placed in current.placements if placed.cargo_id == move['cargo_id']), None)
        if actual != source_placement or source_placement.cargo_id != move['cargo_id'] or destination.cargo_id != move['cargo_id']:
            raise ValueError('Stored move does not match its source cargo placement')
        if destination.orientation != source_placement.orientation or move.get('footprint_revision') != current.shape(move['cargo_id']).footprint_revision:
            raise ValueError('Stored move changes the fixed cargo footprint or orientation')
        current = current.with_placements(tuple(destination if p.cargo_id == move['cargo_id'] else p for p in current.placements))
        display = dict(move, number=i + 1, source_level=move['source_placement']['level'],
                       destination_level=destination.level, spatial=True)
        display_moves.append(display)
        stages.append(stage(i + 1, move['phase_label'], f"{i + 1}단계 후 예상 · {move['phase_label']}", display))
    return stages, display_moves


def workspace_context(order, plan=None, *, priority_form=None, plan_form=None, review_observation=None):
    from harbor_web.views import reason_text
    from harbor_web.plan_explanation import explain_plan
    from harbor_web.planning_pages import gate_text

    command = Command.objects.filter(order=order).select_related('plan__observation__condition', 'plan__condition').first()
    plans = Plan.objects.filter(order=order).select_related('observation__condition', 'condition').order_by('-id')
    if plan is None:
        plan = command.plan if command else plans.first()
    latest = Observation.objects.select_related('condition').order_by('-id').first()
    review_observation = review_observation or latest
    source = plan.observation if plan else review_observation
    stages, moves = _yard_stages(order, plan, source)
    changes = plan_changes(order, plan)
    gate = commands.capability(plan) if plan else dict(enabled=False, reason='', fleet_url=None)
    detail = commands.command_detail(command) if command else None
    different = bool(command and plan and command.plan_id != plan.pk)
    if command:
        decision = dict(title=COMMAND_LABELS.get(command.state, '원래 실행 대조'),
            body={
                'UNKNOWN': '제출 결과가 불명입니다. 원래 요청을 대조한 뒤 다음 업무를 판단하세요.',
                'DISPATCHING': '제출 결과를 확인해야 합니다. 새 요청으로 다시 제출하지 마세요.',
                'CANCEL_PENDING': '취소 요청 뒤 실제 정지와 점유 해제를 대조해야 합니다.',
                'PREPARED': '원래 계획의 현재 단계 제안이 준비돼 있습니다. 실행 승인은 ROSY Fleet에서 진행합니다.',
                'RECONCILED': '실행 원본을 대조했습니다. 배치·복귀·관련 팔 퇴피의 업무 근거는 별도로 확인하세요.',
                'BLOCKED': '인계가 보류되었습니다. 원래 작업의 연결 조건과 보류 사유를 확인하세요.',
            }.get(command.state, '원래 실행의 상태와 근거를 확인하세요.'),
            href=f'/work/{command.pk}/', label='원래 작업 대조', tone='attention')
    elif changes:
        decision = dict(title='계획 재검토 필요', body=changes[0]['message'], href='#planning', label='최신 원본으로 검토', tone='attention')
    elif plan and plan.result['status'] == 'HOLD':
        decision = dict(title='반출 계획 보류', body=reason_text(plan.result['reason']), href='#planning', label='계획 조건 확인', tone='attention')
    elif plan:
        decision = dict(title='계획 검토 완료 · 실행 인계 대기', body=gate_text(gate['reason']) or '관측과 예상 이동을 검토한 뒤 현재 단계의 인계를 준비하세요.', href='#execution', label='실행 인계 조건 확인', tone='info')
    else:
        decision = dict(title='관측 기준으로 계획 검토', body='대상 화물의 현재 위치와 재취급 공간을 확인한 뒤 반출 계획을 만드세요.', href='#planning', label='관측 선택·계획 검토', tone='info')

    plan_ids = [str(pk) for pk in plans.values_list('pk', flat=True)]
    audit_scope = (Q(object_type='Order', object_id=str(order.pk)) |
                   Q(object_type='Plan', object_id__in=plan_ids))
    if command:
        audit_scope |= Q(object_type='Command', object_id=str(command.pk))
    history = Audit.objects.filter(audit_scope).select_related('actor').order_by('-id')[:20]
    if plan_form is None:
        plan_form = forms.PlanForm(review_observation=review_observation,
            initial=dict(expected_version=order.version, observation=review_observation.pk if review_observation else None))
    if priority_form is None:
        priority_form = forms.PriorityForm(initial=dict(expected_version=order.version, priority_group=order.priority_group,
            sequence=order.sequence, tie_policy=order.tie_policy, note=order.note))
    occupancy = occupancy_map(stages[0] if stages else None, moves)
    scene = scene_for(stages)
    if scene:
        scene['rehandle'] = occupancy['rehandle_cargo'] if occupancy else []  # Stored-plan cargo; the viewer never recomputes it.
    completion_plan = command.plan if command else plan
    completion_moves = completion_plan.result['moves'] if completion_plan else []
    related_arms = sorted({arm for move in completion_moves for arm in [move['primary_arm'], *move['alternate_arms']]})
    from harbor_web.pipeline import pipeline_for
    return dict(order=order, plan=plan, plans=plans, command=command, execution=detail,
        pipeline=pipeline_for(order, plan, command, detail), review_observation=review_observation,
        review_differs=bool(plan and review_observation and plan.observation_id != review_observation.pk),
        source=source, latest_source=latest, yard_stages=stages, yard_scene=scene, yard_occupancy=occupancy, display_moves=moves,
        yard_rows=yard_rows(source, targets={order.cargo.code} if order.cargo_id and order.kind == 'OUTBOUND' else (),
                            rehandles=occupancy['rehandle_cargo'] if occupancy else ()),
        plan_explanation=explain_plan(plan) if plan else None,
        plan_changes=changes, different_execution_plan=different, decision=decision,
        gate=gate, gate_reason=gate_text(gate['reason']), plan_reason=reason_text(plan.result['reason']) if plan else '',
        command_label=COMMAND_LABELS.get(command.state, command.state) if command else '',
        completion_state='미확인', completion_plan=completion_plan, related_arms=related_arms,
        rehandled_cargo=sorted({m['cargo_id'] for m in completion_moves if m['cargo_id'] != order.cargo.code}),
        audit_rows=history, priority_form=priority_form, plan_form=plan_form)
