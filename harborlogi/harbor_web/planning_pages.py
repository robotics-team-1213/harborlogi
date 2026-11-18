import os
import sqlite3

from django import forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.http import Http404
from django.views.decorators.http import require_http_methods

from harbor.ledger import ReadOnlyLedger
from harbor_web import commands, human_evidence, services
from harbor_web.auth import has_role, require_roles
from harbor_web.models import Command, Plan
from harbor_web.pipeline import pipeline_for
from harbor_web.presentation import COMMAND_LABELS, gate_text, reason_text
from harbor_web.page_helpers import page, paginate, selected_observation


class CommandForm(forms.Form):
    expected_version = forms.IntegerField(min_value=1, widget=forms.HiddenInput)
    reason = forms.CharField(label='취소 요청 사유', max_length=192, required=False)


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
        if request.POST.get('action') == 'decide':  # a person accepts the algorithm's inbound placement (ADR 0012)
            from django.contrib import messages
            from harbor_web.inbound_decision import decide_placement
            version = request.POST.get('expected_version', '')
            if not (version.isascii() and version.isdigit()):
                return render(request, 'error.html', dict(reason='검토한 revision이 올바르지 않습니다.', return_url=f'/plans/{pk}/'), status=400)
            try:
                decide_placement(request.user, plan_id=pk, expected_version=int(version))
            except (services.Conflict, ValidationError):
                return render(request, 'error.html', dict(heading='결정할 수 없습니다', reason='요청이나 계획이 바뀌었습니다. 최신 입고 추천을 다시 확인하세요.', return_url=f'/requests/{plan.order_id}/'), status=409)
            messages.success(request, '입고 위치를 결정했습니다. 실제 이동은 입고 실행 연결과 관측으로 확인합니다.')
            return redirect(f'/plans/{pk}/')
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
        from harbor_web.inbound_decision import placement_decision
        return page(request, 'placement_plan.html', section='cargo', decision=placement_decision(plan), **placement_context(plan))
    from harbor_web.yard_scene import present_stage, scene_for
    observation_stage = None
    if plan.observation.payload.get('schema') == 'grid-placement-v1':
        from harbor_web.yard_view import spatial_yard_view
        observation_stage = present_stage(dict(spatial_yard_view(services.spatial_snapshot(plan.observation), target=plan.order.cargo.code if plan.order.cargo_id else ''),
            panel_id=f'held-plan-{plan.pk}', label='관측한 배치', index=0), plan.condition.payload)
    from harbor_web.yard_rows import place_context, placement_text
    labels, slot = place_context(plan.condition.payload)
    display_moves = [dict(move, source=placement_text(move['source_placement'], labels, slot),
                          destination=placement_text(move['destination_placement'], labels, slot)) if move.get('source_placement') else move
                     for move in (plan.result.get('moves', []) if plan.order.kind == 'REARRANGE' else [])]
    return page(request, 'plan.html', section='inventory', plan=plan, display_moves=display_moves, gate=gate,
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


@require_roles('viewer', 'operator', 'admin', 'field')  # same roles as work_detail, which narrows field staff
@require_http_methods(['GET'])
def notification_work(request, notification_id):
    """Open a HOLD notification link on the protected business screen (ADR 0029).

    The opaque outbox id resolves through the same read-only ledger the workbench
    uses; authenticated operators land on the command's existing work screen.
    """
    path = os.environ.get('HARBOR_LEDGER_PATH', '')
    if not path:  # A deployment gap must not look like a bad link.
        return render(request, 'error.html', dict(heading='업무 장부가 설정되지 않았습니다', reason='배포 설정(HARBOR_LEDGER_PATH)이 없어 통지가 가리키는 업무를 열 수 없습니다. 설정·연동 담당자가 확인해야 합니다.', return_url='/work/'), status=503)
    try:
        row = ReadOnlyLedger(path).read(notification_id)
    except (KeyError, ValueError, OSError, sqlite3.Error):
        raise Http404('통지가 가리키는 업무를 찾지 못했습니다.')
    command = (Command.objects.filter(key=row['request_key'][len('harbor:'):]).first()
               if row['request_key'].startswith('harbor:') else None)
    if command is None:
        raise Http404('통지가 가리키는 업무를 찾지 못했습니다.')
    return redirect(f'/work/{command.pk}/')


@require_roles('viewer', 'operator', 'admin', 'field')
@require_http_methods(['GET', 'POST'])
def work_detail(request, pk):
    command = get_object_or_404(Command.objects.select_related('plan__order__cargo', 'plan__observation'), pk=pk)
    if request.method == 'GET' and not has_role(request.user, 'viewer', 'operator', 'admin') and (
            not human_evidence.human_evidence_enabled() or human_evidence.awaiting(command) is None):
        from django.core.exceptions import PermissionDenied
        raise PermissionDenied  # field staff see a command only while it waits for their confirmation
    attest_form = None
    if request.method == 'POST' and request.POST.get('action') == 'attest':
        if not has_role(request.user, *human_evidence.ROLES):
            from django.core.exceptions import PermissionDenied
            raise PermissionDenied
        stage = human_evidence.awaiting(command)
        if stage is None:
            return render(request, 'error.html', dict(heading='현장 확인을 받을 단계가 아닙니다', reason='지금은 사람 확인을 기다리는 단계가 아니거나 이미 기록되었습니다. 화면을 새로 열어 확인하세요.', return_url=f'/work/{pk}/'), status=409)
        attest_form = human_evidence.AttestForm(request.POST, required_arms=stage['arms'])
        if attest_form.is_valid():
            try:
                human_evidence.attest(request.user, command_id=pk, **{k: v for k, v in attest_form.cleaned_data.items()
                                                                      if k != 'cargo_seen'})
            except (services.Conflict, ValidationError):
                return render(request, 'error.html', dict(heading='현장 확인 대상이 바뀌었습니다', reason='이 확인은 현재 단계와 시도에 맞지 않거나 이미 기록되었습니다. 화면을 새로 열어 현재 단계를 다시 확인하세요.', return_url=f'/work/{pk}/'), status=409)
            messages.info(request, '현장 확인을 기록했습니다. 업무 장부가 대조하기 전에는 업무 완료가 아닙니다.')
            return redirect(f'/work/{pk}/')
    elif request.method == 'POST':
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
    stage = human_evidence.awaiting(command) if has_role(request.user, *human_evidence.ROLES) else None
    if stage and attest_form is None:
        attest_form = human_evidence.form_for(command, stage)
    return page(request, 'work_detail.html', section='work', command=command, detail=detail,
                attest_form=attest_form if stage else None, attest_stage=stage,
                pipeline=pipeline_for(command.plan.order, command.plan, command, detail),
                gate_reason=gate_text(detail['reason']))
