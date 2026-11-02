from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import Http404, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods

from harbor_web import forms, services
from harbor_web.auth import has_role, require_roles
from harbor_web.models import Audit, Cargo, CargoFootprint, Command, Condition, FieldEpisode, Observation, Order, Plan


def page(request, template, **context):
    return render(request, template, context)


def paginate(request, items):
    query = request.GET.copy()
    query.pop('page', None)
    return dict(page=Paginator(items, 25).get_page(request.GET.get('page')), query=query.urlencode())


def selected_observation(request, *, default_latest=False):
    if 'observation' in request.GET:
        value = request.GET['observation']
        if not value.isascii() or not value.isdigit():
            raise Http404('Invalid observation.')
        return get_object_or_404(Observation.objects.select_related('condition'), pk=value)
    return Observation.objects.select_related('condition').order_by('-pk').first() if default_latest else None


def form_error(form, error):
    for message in error.messages:
        form.add_error(None, message)


def form_page(request, *, form_type, title, section, save, destination, description='', initial=None):
    form = form_type(request.POST or None, initial=initial)
    if request.method == 'POST' and form.is_valid():
        try:
            obj = save(form.cleaned_data)
        except ValidationError as error:
            form_error(form, error)
        except services.Conflict:
            return render(request, 'error.html', dict(heading='기준이 변경되었습니다', reason='입력할 때와 현재 원본이 다릅니다. 최신 자료를 다시 확인하세요.', return_url=request.path), status=409)
        else:
            messages.success(request, '저장했습니다. 원래 자료와 변경 기록을 확인할 수 있습니다.')
            return redirect(destination(obj) if callable(destination) else destination)
    return page(request, 'form.html', form=form, title=title, section=section, description=description)


@require_roles('viewer', 'operator', 'admin', 'field')
@require_http_methods(['GET'])
def overview(request):
    from harbor_web.workspace import plan_changes
    if not has_role(request.user, 'viewer', 'operator', 'admin'):
        return redirect('/field/')
    observation = selected_observation(request, default_latest=True)
    observations = list(Observation.objects.select_related('condition').order_by('-pk')[:100])
    if observation and all(item.pk != observation.pk for item in observations):
        observations.insert(0, observation)
    issues = list(Command.objects.filter(state__in=['UNKNOWN', 'DISPATCHING', 'CANCEL_PENDING', 'BLOCKED']).select_related('plan__order__cargo').order_by('created_at')[:12])
    issue_ids = [command.plan.order_id for command in issues]
    requests = [command.plan.order for command in issues]
    requests += list(Order.objects.exclude(kind='REARRANGE').exclude(pk__in=issue_ids).select_related('cargo').order_by('priority_group', 'sequence', 'id')[:12 - len(requests)])
    executions = {command.plan.order_id: command for command in Command.objects.filter(plan__order_id__in=[order.pk for order in requests]).select_related('plan')}
    plans = {p.order_id: p for p in Plan.objects.filter(order_id__in=[o.pk for o in requests]).order_by('id')}
    rows = []
    current_queue_digest = services.queue_digest()
    for order in requests:
        plan = plans.get(order.pk)
        action = '계획 확인' if plan else ('입고 인계 조건 확인' if order.kind == 'INBOUND' else '관측 선택·계획 검토')
        row = dict(order=order, plan=plan, action=action, reason=reason_text(plan.result['reason']) if plan else ('입고 실행 인계 조건을 확인하세요.' if order.kind == 'INBOUND' else '관측 원본과 반출 순서를 검토하세요.'), status=plan.result['status'] if plan else '', status_label=plan.result['status'] if plan else '계획 전', href=f'/requests/{order.pk}/')
        if order.kind == 'INBOUND':
            from harbor_web.field_pages import episode_status
            episode = FieldEpisode.objects.filter(order=order).select_related('order__cargo', 'assigned_to').order_by('-pk').first()
            if episode:
                state = episode_status(episode)
                row.update(action=state['action'], reason=state['reason'], href=state['href'],
                           status=episode.state, status_label=state['status_label'])
                if not has_role(request.user, 'operator', 'admin'):
                    row.update(action='입고 진행 확인', href=f'/requests/{order.pk}/')
            else:
                row.update(action='현장 확인 배정', reason='차량과 담당자를 지정해 입고 확인 회차를 배정하세요.',
                           status_label='배정 전', href=f'/requests/{order.pk}/')
            if plan and plan.result.get('plan_kind') == 'inbound-placement':
                row['field_status'] = row['status_label']
                row.update(action='입고 추천·변경 근거 확인', reason=reason_text(plan.result['reason']),
                    status=plan.result['status'], status_label='입고 배치 추천' if plan.result['status'] == 'READY' else '입고 배치 보류',
                    href=f'/plans/{plan.pk}/')
        changes = plan_changes(order, plan, current_queue_digest=current_queue_digest)
        if changes:
            row.update(status='HOLD', status_label='계획 재검토', reason=changes[0]['message'], action='최신 기준으로 검토', href=f'/requests/{order.pk}/')
        command = executions.get(order.pk)
        if command:
            row.update(status=command.state, status_label={'UNKNOWN': '결과 불명', 'DISPATCHING': '제출 대조 필요', 'CANCEL_PENDING': '취소 대조 필요', 'BLOCKED': '인계 보류', 'PREPARED': '제안 검토 대기', 'RECONCILED': '원본 대조됨'}.get(command.state, command.state), action='원래 작업 대조', href=f'/work/{command.pk}/')
            row['reason'] = {
                'UNKNOWN': '제출 결과가 불명입니다. 새 계획으로 재제출하지 말고 원래 요청을 대조하세요.',
                'DISPATCHING': '제출 중이거나 응답을 확인하지 못했습니다. 원래 요청을 대조하세요.',
                'CANCEL_PENDING': '취소 결과와 실제 정지를 대조해야 합니다. 후속 업무는 보류합니다.',
                'BLOCKED': '실행 인계가 보류되었습니다. 원래 작업의 설치·관측·복구 조건을 확인하세요.',
                'PREPARED': '실행 인계 요청이 준비되었습니다. 원래 계획으로 현재 단계 제안을 검토하세요.',
                'RECONCILED': '실행 원본을 대조했습니다. 실제 수행과 전체 업무 완료 근거를 확인하세요.',
            }.get(command.state, '원래 작업의 상태와 근거를 확인하세요.')
        rows.append(row)
    if observation:
        for row in rows:
            if row['href'].startswith(('/requests/', '/plans/')):
                row['href'] += f'?observation={observation.pk}'
    from harbor_web.operations import operations_context
    return page(request, 'overview.html', section='overview', rows=rows, operations=operations_context(observation), observations=observations,
                observation=observation, condition=observation.condition if observation else None,
                field_pending=FieldEpisode.objects.filter(state='OPEN').count())


@require_roles('viewer', 'operator', 'admin')
@require_http_methods(['GET'])
def cargo_list(request):
    q = request.GET.get('q', '')[:200]
    items = Cargo.objects.filter(Q(code__icontains=q) | Q(name__icontains=q) | Q(destination__icontains=q)).order_by('-id')
    pagination = paginate(request, items)
    footprints = {f.cargo_id: f for f in CargoFootprint.objects.filter(cargo_id__in=[c.pk for c in pagination['page']]).order_by('pk')}
    for cargo in pagination['page']:
        cargo.current_footprint = footprints.get(cargo.pk)
    return page(request, 'cargo.html', section='cargo', q=q, **pagination)


@require_roles('viewer', 'operator', 'admin')
@require_http_methods(['GET', 'POST'])
def cargo_footprint(request, pk):
    cargo = get_object_or_404(Cargo, pk=pk)
    if request.method == 'POST' and not has_role(request.user, 'operator'):
        from django.core.exceptions import PermissionDenied
        raise PermissionDenied
    current_footprint = CargoFootprint.objects.filter(cargo=cargo).order_by('-pk').first()
    form = forms.FootprintForm(request.POST or None, initial={'length_cells': current_footprint.length_cells if current_footprint else 1})
    if request.method == 'POST' and form.is_valid():
        try:
            services.register_footprint(request.user, cargo_id=pk, **form.cleaned_data)
        except ValidationError as error:
            form_error(form, error)
        else:
            messages.success(request, '새 점유 크기를 기록했습니다. 과거 관측과 계획의 크기 원본은 유지됩니다.')
            return redirect(f'/cargo/{pk}/footprint/')
    return page(request, 'cargo_footprint.html', section='cargo', cargo=cargo, form=form, current_footprint=current_footprint,
                footprints=CargoFootprint.objects.filter(cargo=cargo).select_related('created_by').order_by('-pk'))


@require_roles('operator')
@require_http_methods(['GET', 'POST'])
def cargo_new(request):
    return form_page(request, form_type=forms.CargoForm, title='화물 등록', section='cargo', description='화물의 기본 정보를 등록합니다. 실제 위치와 내려놓기는 별도 관측으로 확인합니다.', save=lambda data: services.register_cargo(request.user, **data), destination='/cargo/')


@require_roles('viewer', 'operator', 'admin')
@require_http_methods(['GET'])
def order_list(request):
    observation = selected_observation(request)
    q, kind = request.GET.get('q', '')[:200], request.GET.get('kind', '')
    items = Order.objects.select_related('cargo').filter(Q(cargo__code__icontains=q) | Q(cargo__name__icontains=q)).order_by('priority_group', 'sequence', 'id')
    if kind in {'INBOUND', 'OUTBOUND'}:
        items = items.filter(kind=kind)
    return page(request, 'requests.html', section='cargo', q=q, kind=kind, review_observation=observation,
                observation_query=f'observation={observation.pk}' if observation else '', **paginate(request, items))


@require_roles('viewer', 'operator', 'admin')
@require_http_methods(['GET'])
def outbound_queue(request):
    from harbor_web.workspace import COMMAND_LABELS, plan_changes
    observation = selected_observation(request)
    q = request.GET.get('q', '')[:200]
    items = Order.objects.filter(kind='OUTBOUND').select_related('cargo').filter(
        Q(cargo__code__icontains=q) | Q(cargo__name__icontains=q) | Q(note__icontains=q)
    ).order_by('priority_group', 'sequence', 'id')
    pagination = paginate(request, items)
    orders = list(pagination['page'])
    ids = [o.pk for o in orders]
    plans = {p.order_id: p for p in Plan.objects.filter(order_id__in=ids).order_by('id')}
    executions = {c.order_id: c for c in Command.objects.filter(order_id__in=ids)}
    rows = []
    current_queue_digest = services.queue_digest()
    for order in orders:
        command, plan = executions.get(order.pk), plans.get(order.pk)
        changes = plan_changes(order, plan, current_queue_digest=current_queue_digest)
        label = COMMAND_LABELS.get(command.state, command.state) if command else '계획 재검토' if changes else '계획 보류' if plan and plan.result['status'] == 'HOLD' else '계획 검토됨' if plan else '계획 전'
        rows.append(dict(order=order, plan=plan, command=command, status_label=label,
            status=command.state if command else 'HOLD' if changes else plan.result['status'] if plan else '',
            reason='원래 실행의 상태와 업무 근거를 대조하세요.' if command else changes[0]['message'] if changes else reason_text(plan.result['reason']) if plan else '관측 재고를 선택해 재취급·반출·복귀를 검토하세요.'))
    return page(request, 'outbound_queue.html', section='outbound', rows=rows, q=q,
                observation=observation, **pagination)


@require_roles('operator')
@require_http_methods(['GET', 'POST'])
def order_new(request):
    observation = selected_observation(request)
    initial = {'kind': request.GET['kind']} if request.GET.get('kind') in {'INBOUND', 'OUTBOUND'} else {}
    if 'cargo' in request.GET:
        if not request.GET['cargo'].isascii() or not request.GET['cargo'].isdigit():
            raise Http404('Invalid cargo.')
        initial['cargo'] = get_object_or_404(Cargo, pk=request.GET['cargo'])
    suffix = f'?observation={observation.pk}' if observation else ''
    return form_page(request, form_type=forms.OrderForm, title='업무 요청 등록', section='cargo', description='업무 지시와 우선순위를 저장합니다. 요청 등록으로 실제 화물 위치가 바뀌지 않습니다.', save=lambda data: services.create_order(request.user, **data), destination=lambda obj: f'/requests/{obj.pk}/{suffix}', initial=initial)


@require_roles('viewer', 'operator', 'admin')
@require_http_methods(['GET', 'POST'])
def order_detail(request, pk):
    order = get_object_or_404(Order.objects.select_related('cargo'), pk=pk)
    observation = selected_observation(request, default_latest=True)
    suffix = f'?observation={observation.pk}' if observation else ''
    priority = forms.PriorityForm(initial=dict(expected_version=order.version, priority_group=order.priority_group, sequence=order.sequence, tie_policy=order.tie_policy, note=order.note))
    plan_form = forms.PlanForm(initial={'expected_version': order.version, 'observation': observation}, review_observation=observation)
    if request.method == 'POST':
        if not has_role(request.user, 'operator'):
            from django.core.exceptions import PermissionDenied
            raise PermissionDenied
        action = request.POST.get('action')
        if action not in {'priority', 'plan'}:
            return HttpResponseBadRequest('지원하지 않는 요청입니다.')
        if action == 'priority':
            active = priority = forms.PriorityForm(request.POST)
        else:
            active = plan_form = forms.PlanForm(request.POST, review_observation=observation)
        if active.is_valid():
            try:
                if action == 'priority':
                    services.update_order(request.user, order_id=pk, **active.cleaned_data)
                else:
                    data = active.cleaned_data
                    plan = services.make_plan(request.user, order_id=pk, expected_version=data['expected_version'], observation_id=data['observation'].pk, automatic=data['automatic'])
                    return redirect(f'/plans/{plan.pk}/')
            except services.Conflict:
                return render(request, 'error.html', dict(heading='요청이 변경되었습니다', reason='다른 사용자의 변경이 있습니다. 현재 revision을 다시 확인하세요.', return_url=f'/requests/{pk}/'), status=409)
            except ValidationError as error:
                form_error(active, error)
            else:
                messages.success(request, '요청 revision을 변경했습니다. 기존 계획은 원래 기준을 유지합니다.')
                return redirect(f'/requests/{pk}/{suffix}')
    if order.kind == 'OUTBOUND':
        from harbor_web.workspace import workspace_context
        return page(request, 'workspace.html', section='outbound', **workspace_context(order, priority_form=priority, plan_form=plan_form, review_observation=observation))
    from harbor_web.field_pages import episode_status
    episode_rows = [dict(episode=episode, **episode_status(episode)) for episode in
                    FieldEpisode.objects.filter(order=order).select_related('order__cargo', 'assigned_to').order_by('-pk')]
    return page(request, 'request_detail.html', section='cargo', order=order, priority_form=priority, plan_form=plan_form,
                plans=Plan.objects.filter(order=order).order_by('-id'), episode_rows=episode_rows,
                review_observation=observation, observation_query=f'observation={observation.pk}' if observation else '')


@require_roles('viewer', 'operator', 'admin')
@require_http_methods(['GET'])
def inventory(request):
    observations = Observation.objects.select_related('condition').order_by('-id')
    condition = None
    if 'condition' in request.GET:
        value = request.GET['condition']
        if not value.isascii() or not value.isdigit():
            raise Http404('Invalid condition.')
        condition = get_object_or_404(Condition, pk=value)
        observations = observations.filter(condition=condition)
    selected = selected_observation(request)
    if selected and condition and selected.condition_id != condition.pk:
        return HttpResponseBadRequest('Observation does not belong to the selected condition.')
    if selected is None:
        selected = observations.first()
    condition = selected.condition if selected else condition or (
        Condition.objects.filter(payload__has_key='yard_layout').order_by('-pk').first()
        or Condition.objects.order_by('-pk').first())
    choices = list(observations[:100])
    if selected and all(item.pk != selected.pk for item in choices):
        choices.insert(0, selected)
    from harbor_web.yard_view import yard_view, spatial_yard_view
    from harbor_web.yard_scene import present_stage, scene_for
    from harbor_web.map_config import map_scene
    view = (spatial_yard_view(services.spatial_snapshot(selected)) if selected.payload.get('schema') == 'grid-placement-v1'
            else yard_view(selected.payload['bays'], selected.condition.payload)) if selected else None
    stage = present_stage(dict(view, panel_id='inventory-yard', label='관측한 배치', index=0), condition.payload) if view else None
    scene = scene_for([stage]) if stage else map_scene(condition.payload) if condition else None
    definition_rows = []
    if scene and not selected:
        for area in scene['areas']:
            for level in range(1, area['levels'] + 1):
                cells = (condition.payload['yard_layout']['cells'] if area['id'] == 'yard' else
                    [dict(bay_id=f"{area['id']}-R{r}C{c}", row=r, column=c)
                     for r in range(1, area['rows'] + 1) for c in range(1, area['columns'] + 1)])
                definition_rows.extend(dict(area=area['label'], position_id=f"{cell['bay_id']}-L{level}",
                    row=cell['row'], column=cell['column'], level=level) for cell in cells)
    return page(request, 'inventory.html', section='inventory', observation=selected, observations=choices,
                condition=condition, condition_filter='condition' in request.GET,
                conditions=Condition.objects.order_by('-pk'), definition_rows=definition_rows,
                yard_stage=stage, yard_scene=scene)


@require_roles('admin')
@require_http_methods(['GET', 'POST'])
def observation_new(request):
    return form_page(request, form_type=forms.ObservationForm, title='관측 원본 등록', section='inventory', description='원본 출처와 시각을 함께 보존합니다. 수동 등록은 인증된 운영 producer를 대신하지 않습니다.', save=lambda data: services.record_observation(request.user, **data), destination=lambda obj: f'/inventory/?observation={obj.pk}')


@require_roles('admin')
@require_http_methods(['GET'])
def settings_page(request):
    return page(request, 'settings.html', section='settings', conditions=Condition.objects.order_by('-id'))


@require_roles('admin')
@require_http_methods(['GET', 'POST'])
def condition_new(request):
    return form_page(request, form_type=forms.ConditionForm, title='설치 기준 등록', section='settings', description='검증 기록의 참조와 계획 조건을 새 버전으로 보존합니다. 진행 중 작업의 기준은 바꾸지 않습니다.', save=lambda data: services.register_condition(request.user, **data), destination='/settings/')


def reason_text(code):
    return {
        'PLACEMENT_RECOMMENDATION_REQUIRES_EXECUTION_BINDINGS': '반출 순서를 고려한 입고 위치를 계산했습니다. 실제 입고에는 설치 대응과 승인·관측 근거가 필요합니다.',
        'INBOUND_ALREADY_OBSERVED': '이 화물은 이미 관측에 배치되어 있습니다. 새 입고 위치를 중복 생성하지 않습니다.',
        'NO_LEGAL_PLACEMENT': '화물의 전체 크기·지지·도달 조건을 만족하는 보관 위치가 없습니다.',
        'PLACEMENT_SEARCH_LIMIT_REACHED': '배치 후보 검토 한도에 도달했습니다. 위치를 임의로 확정하지 않고 보류합니다.',
        'OBSERVATION_CARGO_LIMIT_REACHED': '관측 화물 수 한도에 도달해 입고 배치를 보류합니다.',
        'SOURCE_CONDITION_CHANGED': '결속한 출처의 설치 기준이 바뀌었습니다. 새 기준의 관측을 직접 선택해 다시 계산하세요.',
        'INCOMING_FOOTPRINT_REQUIRED': '입고 화물의 점유 크기 원본이 필요합니다.',
        'OTHER_COMMAND_REQUIRES_RECONCILIATION': '다른 인계 작업의 실제 실행 상태를 먼저 대조해야 합니다. 자동 계산으로 실행 중인 배치를 추측하지 않습니다.',
        'SPATIAL_OBSERVATION_REQUIRED': '이 화물의 다칸 점유 크기를 확인할 수 있는 공간 관측 원본이 필요합니다.',
        'NO_LEGAL_SPATIAL_PLAN': '전체 점유 면적과 지지 조건을 만족하는 임시 배치·반출·복귀 계획을 찾지 못했습니다.',
        'SPATIAL_SEARCH_LIMIT_REACHED': '공간 계획의 검토 한도에 도달했습니다. 공간이 없다고 단정하지 않고 보류합니다.',
        'PROPOSAL_REQUIRES_ADMISSION_AND_EVIDENCE': '계획을 만들었습니다. 실행 전 설치 대응과 승인·근거 조건을 확인하세요.',
        'PRIORITY_ORDER_BLOCKED': '앞선 반출 지시가 있습니다. 우선순위와 그룹 안 허용 순서를 확인하세요.',
        'INBOUND_PLANNER_NOT_AVAILABLE': '이 원본 형식으로는 입고 배치를 계산할 수 없습니다. 전체 구역·점유 크기가 포함된 공간 관측을 선택하세요. 입력한 요청은 보존됩니다.',
        'REVISION_CONFLICT': '검토한 관측 revision과 현재 기준이 다릅니다.',
        'OBSERVATION_NOT_CURRENT': '관측이 오래되었거나 시각을 확인할 수 없습니다. 최신 원본이 필요합니다.',
        'PHYSICAL_HOLDING_REQUIRES_RECONCILIATION': '들고 있는 화물이 있어 실제 인계 상태 확인이 필요합니다.',
        'OUTBOUND_NOT_OBSERVED': '목표 화물을 관측 원본에서 찾을 수 없습니다.',
        'OUTBOUND_NOT_IN_A_BAY': '목표 화물이 보관 칸에서 확인되지 않았습니다.',
        'OUTBOUND_ALREADY_ON_STAND_REQUIRES_EVIDENCE': '반출대에 있는 화물의 완료 근거를 확인해야 합니다.',
        'OUTBOUND_STAND_FULL': '반출대에 빈자리가 없습니다.',
        'OUTBOUND_MOVE_UNAVAILABLE': '보관 칸에서 반출대로 허용된 이동이 없습니다.',
        'NO_LEGAL_TEMPORARY_BAY': '방해 화물을 둘 유효한 임시 공간이 없습니다.',
        'ARM_REACH_UNVERIFIED': '팔의 도달 가능성 검증 참조가 없습니다.',
        'NO_ARM_REACHES_MOVE': '이 이동을 수행할 수 있는 팔 대응이 없습니다.',
    }.get(code, '원본과 연결 조건을 확인해야 합니다.')
