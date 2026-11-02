from django import forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.http import Http404
from django.views.decorators.http import require_http_methods

from harbor_web import commands, services
from harbor_web.auth import has_role, require_roles
from harbor_web.models import Command, Plan
from harbor_web.pipeline import COMMAND_LABELS, pipeline_for
from harbor_web.views import page, paginate, reason_text, selected_observation


class CommandForm(forms.Form):
    expected_version = forms.IntegerField(min_value=1, widget=forms.HiddenInput)
    reason = forms.CharField(label='취소 요청 사유', max_length=192, required=False)


def gate_text(code):
    return {
        'SPATIAL_EXECUTION_BINDINGS_REQUIRED': '공간 계획의 전체 점유 크기·방향·위치에 맞는 설치 대응 검증이 필요합니다.',
        'SPATIAL_OBSERVATION_REQUIRED': '다칸 화물이 등록되었습니다. 전체 점유 위치와 크기 원본을 포함한 공간 관측으로 다시 검토하세요.',
        'EXISTING_ORDER_EXECUTION_REQUIRES_RECONCILIATION': '이 요청에는 이미 실행 인계가 있습니다. 새로 제출하지 말고 원래 작업을 대조하세요.',
        'RESTORE_RECONCILIATION_REQUIRED': '복원한 자료입니다. 원래 실행과 복구 검토가 끝나기 전 새 제출을 보류합니다.',
        'DEPLOYMENT_PROFILE_REQUIRED': '배포 환경과 데이터베이스 기준을 먼저 확인해야 합니다.',
        'EXECUTION_CONFIGURATION_REQUIRED': 'ROSY 연동 설정과 설치된 이동 대응이 필요합니다.',
        'ORIGIN_NOT_ALLOWED': 'Fleet 주소가 배포 서버의 허용 목록에 없습니다. 설정·연동 담당자가 확인해야 합니다.',
        'PLAN_NOT_READY': '계획의 보류 사유를 먼저 해결해야 합니다.',
        'PLAN_QUEUE_CHANGED': '요청 또는 우선순위가 변경되었습니다. 최신 지시로 다시 검토하세요.',
        'OBSERVATION_NOT_FRESH': '검토한 관측이 오래되었습니다. 최신 원본으로 계획을 다시 만드세요.',
        'OBSERVATION_REVISION_CHANGED': '같은 출처의 새 관측이 있습니다. 최신 원본으로 계획을 다시 검토하세요.',
        'AUTHENTICATED_OBSERVATION_AND_EVIDENCE_PRODUCER_REQUIRED': '인증된 관측·업무 근거 연결이 필요합니다. 수동 자료는 운영 실행 근거가 아닙니다.',
        'EXECUTION_INPUTS_UNAVAILABLE': '연결 기준을 읽을 수 없습니다. 설치 담당자와 원본을 확인하세요.',
        'BUSINESS_HOLD': '원래 실행을 찾았지만 업무 보류는 유지됩니다. 완료 근거를 확인하세요.',
        'CANCELLATION_REQUESTED': '취소를 요청했습니다. 실제 정지·점유 해제는 아직 확인되지 않았습니다.',
        'CANCELLATION_OUTCOME_UNKNOWN': '취소 요청의 결과가 불명입니다. 원래 실행으로 대조해야 합니다.',
        'RECONCILIATION_REQUIRED': '제출 또는 원본 조회 결과가 불명입니다. 새로 제출하지 말고 원래 요청을 대조하세요.',
        'LEDGER_UNAVAILABLE': '업무 장부를 읽을 수 없습니다. 실행 상태를 정상으로 판단하지 마세요.',
    }.get(code, '현재 연결과 원래 요청을 대조해야 합니다.' if code else '')


@require_roles('viewer', 'operator', 'admin')
@require_http_methods(['GET', 'POST'])
def plan_detail(request, pk):
    plan = get_object_or_404(Plan.objects.select_related('order__cargo', 'observation', 'condition'), pk=pk)
    review_observation = selected_observation(request, default_latest=True)
    gate = commands.capability(plan)
    if request.method == 'POST':
        if not has_role(request.user, 'operator'):
            from django.core.exceptions import PermissionDenied
            raise PermissionDenied
        form = CommandForm(request.POST)
        if form.is_valid():
            try:
                command = commands.prepare_command(request.user, plan_id=pk, expected_version=form.cleaned_data['expected_version'])
                if command.state == 'PREPARED':
                    command = commands.submit_command(request.user, command_id=command.pk, expected_version=command.version)
            except (services.Conflict, ValidationError):
                return render(request, 'error.html', dict(heading='실행 인계 조건이 달라졌습니다', reason='요청 revision·원본 관측·설치 대응과 연결 상태를 다시 확인하세요.', return_url=f'/plans/{pk}/'), status=409)
            return redirect(f'/work/{command.pk}/')
        return render(request, 'error.html', dict(reason='검토한 revision이 올바르지 않습니다.', return_url=f'/plans/{pk}/'), status=400)
    if plan.order.kind == 'OUTBOUND':
        from harbor_web.workspace import workspace_context
        return page(request, 'workspace.html', section='outbound', **workspace_context(plan.order, plan, review_observation=review_observation))
    if plan.result.get('plan_kind') == 'inbound-placement':
        from harbor_web.placement_view import placement_context
        return page(request, 'placement_plan.html', section='cargo', **placement_context(plan))
    from harbor_web.yard_scene import present_stage, scene_for
    observation_stage = None
    if plan.observation.payload.get('schema') == 'grid-placement-v1':
        from harbor_web.yard_view import spatial_yard_view
        observation_stage = present_stage(dict(spatial_yard_view(services.spatial_snapshot(plan.observation), target=plan.order.cargo.code if plan.order.cargo_id else ''),
            panel_id=f'held-plan-{plan.pk}', label='관측한 배치', index=0), plan.condition.payload)
    return page(request, 'plan.html', section='inventory', plan=plan, display_moves=plan.result.get('moves', []) if plan.order.kind == 'REARRANGE' else [], gate=gate,
                gate_reason=gate_text(gate['reason']), plan_reason=reason_text(plan.result['reason']), command=None,
                observation_stage=observation_stage, yard_scene=scene_for([observation_stage]) if observation_stage else None)


@require_roles('viewer', 'operator', 'admin')
@require_http_methods(['GET'])
def candidate_preview(request, pk, rank):
    from harbor_web.recommendation_view import candidate_details
    from harbor_web.workspace import spatial_stages, plan_changes
    from harbor_web.yard_scene import scene_for
    plan = get_object_or_404(Plan.objects.select_related('order__cargo', 'observation__condition', 'condition'), pk=pk)
    details = candidate_details(plan.result, rank)
    if details is None or not isinstance(plan.observation.payload, dict) or plan.observation.payload.get('schema') != 'grid-placement-v1':
        raise Http404('저장된 공간 비교 후보가 없습니다.')
    try:
        stages, moves = spatial_stages(plan.order, plan.observation, details['candidate']['moves'])
    except (KeyError, TypeError, ValueError, ValidationError) as error:
        raise Http404('저장된 후보의 배치 자료를 읽을 수 없습니다.') from error
    return page(request, 'candidate_preview.html', section='outbound', plan=plan, rank=rank,
        is_recommended=rank == 1, score_rows=details['score_rows'],
        optimization_quality=details['quality_label'], search_complete=details['search_complete'],
        candidate_ranks=range(1, details['candidate_count'] + 1), plan_changes=plan_changes(plan.order, plan),
        yard_stages=stages, display_moves=moves, yard_scene=scene_for(stages))


@require_roles('viewer', 'operator', 'admin')
@require_http_methods(['GET'])
def work_list(request):
    q, state = request.GET.get('q', '')[:200], request.GET.get('state', '')
    items = Command.objects.select_related('plan__order__cargo').order_by('-id')
    if q:  # A rearrangement order has no cargo code; an empty search must not drop it.
        items = items.filter(Q(plan__order__cargo__code__icontains=q) | Q(mission_id__icontains=q))
    if state in dict(Command._meta.get_field('state').choices):
        items = items.filter(state=state)
    context = paginate(request, items)
    for command in context['page']:
        command.display_state = COMMAND_LABELS.get(command.state, '원래 실행 대조 필요')
    return page(request, 'work.html', section='work', q=q, state=state, **context)


@require_roles('viewer', 'operator', 'admin')
@require_http_methods(['GET', 'POST'])
def work_detail(request, pk):
    command = get_object_or_404(Command.objects.select_related('plan__order__cargo', 'plan__observation'), pk=pk)
    if request.method == 'POST':
        if not has_role(request.user, 'operator'):
            from django.core.exceptions import PermissionDenied
            raise PermissionDenied
        form = CommandForm(request.POST)
        action = request.POST.get('action')
        if form.is_valid() and action in {'submit', 'reconcile', 'cancel', 'close'}:
            try:
                args = dict(command_id=pk, expected_version=form.cleaned_data['expected_version'])
                if action == 'cancel':
                    commands.cancel_command(request.user, reason=form.cleaned_data['reason'], **args)
                elif action == 'close':
                    commands.close_cancelled(request.user, **args)
                elif action == 'submit':
                    commands.submit_command(request.user, **args)
                else:
                    commands.reconcile_command(request.user, **args)
            except (services.Conflict, ValidationError):
                return render(request, 'error.html', dict(heading='원래 요청을 확인해야 합니다', reason='작업 기준이 바뀌었거나 현재 조작을 허용할 수 없습니다. 새 요청을 만들지 말고 현재 원본을 확인하세요.', return_url=f'/work/{pk}/'), status=409)
            messages.info(request, '처리 결과를 저장했습니다. 실제 수행과 업무 완료 상태를 각각 확인하세요.')
            return redirect(f'/work/{pk}/')
        return render(request, 'error.html', dict(reason='요청 형식이나 취소 사유가 올바르지 않습니다.', return_url=f'/work/{pk}/'), status=400)
    detail = commands.command_detail(command)
    return page(request, 'work_detail.html', section='work', command=command, detail=detail,
                pipeline=pipeline_for(command.plan.order, command.plan, command, detail),
                gate_reason=gate_text(detail['reason']))
