import json
import os
import sqlite3

from django import forms
from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.http import Http404
from django.views.decorators.http import require_http_methods

from harbor.ledger import ReadOnlyLedger
from harbor_web import cell_plan_dispatch, commands, human_evidence, outbound_loop, services
from harbor_web.auth import has_role, require_roles
from harbor_web.models import CellPlanDispatch, Command, OutboundDispatch, Plan
from harbor_web.pipeline import pipeline_for
from harbor_web.presentation import gate_text, reason_text, refusal_text
from harbor_web.page_helpers import page, selected_observation


class CommandForm(forms.Form):
    expected_version = forms.IntegerField(min_value=1, widget=forms.HiddenInput)
    reason = forms.CharField(label='취소 요청 사유', max_length=192, required=False)


def _parse_goal_payload(raw_goal):
    text = (raw_goal or '').strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except ValueError as exc:
        raise ValidationError('목표 배치 JSON 형식이 올바르지 않습니다.') from exc
    if not isinstance(parsed, dict) or not parsed or any(
        not isinstance(k, str) or not isinstance(v, list) or not all(isinstance(item, str) for item in v)
        for k, v in parsed.items()
    ):
        raise ValidationError('목표 배치는 {"slot_id": ["CARGO", ...]} 형태여야 합니다.')
    return parsed


@require_roles('viewer', 'operator', 'admin', 'field')
@require_http_methods(['GET', 'POST'])
def plan_detail(request, pk):
    plan = get_object_or_404(Plan.objects.select_related('order__cargo', 'observation', 'condition'), pk=pk)
    review_observation = selected_observation(request, default_latest=True)
    gate = commands.capability(plan)
    awaiting_field_load = OutboundDispatch.objects.filter(
        cell_plan_dispatch__order=plan.order, state='PICKUP_ARRIVED',
    ).exists()
    if (request.method == 'GET' and not has_role(request.user, 'viewer', 'operator', 'admin')
            and not awaiting_field_load):
        raise PermissionDenied
    preview_result = None
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'cell_plan_outbound_loaded':
            if not has_role(request.user, 'field', 'operator'):
                raise PermissionDenied
        elif not has_role(request.user, 'operator'):
            raise PermissionDenied
        if action == 'cell_plan_preview_harbor':
            try:
                dry = cell_plan_dispatch.preview_harbor_plan(request.user, plan_id=pk)
            except (services.Conflict, ValidationError) as exc:
                return render(request, 'error.html', dict(heading='OMX 셀 계획 미리보기를 실행하지 못했습니다', reason=refusal_text(exc, '계획·상태·Fleet 연결을 확인하세요.'), return_url=f'/plans/{pk}/'), status=409)
            preview_result = dict(mode=dry.mode, order_source=dry.order_source, moves=list(dry.moves), final_state=dry.final_state)
            messages.info(request, f'Harbor 계획 미리보기: {len(dry.moves)}단계 이동 확인')
        elif action == 'cell_plan_preview_rosy':
            directive_mode = request.POST.get('directive_mode') or ('outbound' if plan.order.kind == 'OUTBOUND' else 'rehandle')
            destination = request.POST.get('destination') or 'stand'
            try:
                goal = _parse_goal_payload(request.POST.get('goal_json')) if directive_mode == 'goal' else None
                dry = cell_plan_dispatch.preview_rosy_directive(
                    request.user,
                    order_id=plan.order_id,
                    observation_id=plan.observation_id,
                    directive_mode=directive_mode,
                    destination=destination,
                    goal=goal,
                )
            except (services.Conflict, ValidationError) as exc:
                return render(request, 'error.html', dict(heading='ROSY 자율 계획 미리보기를 실행하지 못했습니다', reason=refusal_text(exc, '배치·목표·Fleet 연결을 확인하세요.'), return_url=f'/plans/{pk}/'), status=409)
            preview_result = dict(mode=dry.mode, order_source=dry.order_source, moves=list(dry.moves), final_state=dry.final_state)
            messages.info(request, f'ROSY 계획 미리보기: {len(dry.moves)}단계 이동 확인')
        elif action == 'cell_plan_harbor':
            try:
                dispatch = cell_plan_dispatch.dispatch_harbor_plan(request.user, plan_id=pk)
            except (services.Conflict, ValidationError) as exc:
                return render(request, 'error.html', dict(heading='OMX 셀 계획 요청을 보내지 못했습니다', reason=refusal_text(exc, '계획·관측·Fleet 연결 상태를 확인하세요.'), return_url=f'/plans/{pk}/'), status=409)
            if dispatch.state == 'UNCERTAIN':
                messages.warning(request, '요청 응답을 받지 못해 기록된 요청 키로 진행 상태를 확인 중입니다.')
            else:
                messages.success(request, f'Harbor 계획 순서·좌표 {len(dispatch.rosy_task_ids)}단계를 ROSY에 제안했습니다.')
            return redirect(f'/plans/{pk}/')
        elif action == 'cell_plan_rosy':
            directive_mode = request.POST.get('directive_mode') or ('outbound' if plan.order.kind == 'OUTBOUND' else 'rehandle')
            destination = request.POST.get('destination') or 'stand'
            try:
                goal = _parse_goal_payload(request.POST.get('goal_json')) if directive_mode == 'goal' else None
                dispatch = cell_plan_dispatch.dispatch_rosy_directive(
                    request.user,
                    order_id=plan.order_id,
                    observation_id=plan.observation_id,
                    directive_mode=directive_mode,
                    destination=destination,
                    goal=goal,
                    plan_id=pk,
                )
            except (services.Conflict, ValidationError) as exc:
                return render(request, 'error.html', dict(heading='ROSY 자율 계획 요청을 보내지 못했습니다', reason=refusal_text(exc, '관측 배치·지시 대상·Fleet 연결 상태를 확인하세요.'), return_url=f'/plans/{pk}/'), status=409)
            if dispatch.state == 'UNCERTAIN':
                messages.warning(request, '요청 응답을 받지 못해 기록된 요청 키로 진행 상태를 확인 중입니다.')
            else:
                messages.success(request, f'ROSY 자율 계획 {len(dispatch.rosy_task_ids)}단계를 제안했습니다.')
            return redirect(f'/plans/{pk}/')
        if request.POST.get('action') == 'cell_plan_sync':
            try:
                dispatch_id = int(request.POST.get('dispatch_id', ''))
            except (TypeError, ValueError):
                return render(request, 'error.html', dict(reason='셀 계획 식별자가 올바르지 않습니다.', return_url=f'/plans/{pk}/'), status=400)
            target_row = get_object_or_404(CellPlanDispatch, pk=dispatch_id, order_id=plan.order_id)
            try:
                synced = cell_plan_dispatch.sync_cell_plan_dispatch(target_row.pk)
            except (services.Conflict, ValidationError, OSError, ValueError) as exc:
                return render(request, 'error.html', dict(heading='셀 계획 진행 상태를 확인하지 못했습니다', reason=refusal_text(exc, 'Fleet 연결 상태를 확인한 뒤 다시 시도하세요.'), return_url=f'/plans/{pk}/'), status=409)
            messages.info(request, f'셀 계획 상태를 확인했습니다: {cell_plan_dispatch._STATE_LABELS.get(synced.state, synced.state)}')
            return redirect(f'/plans/{pk}/')
        if request.POST.get('action') == 'cell_plan_outbound_dispatch':
            try:
                dispatch_id = int(request.POST.get('dispatch_id', ''))
            except (TypeError, ValueError):
                return render(request, 'error.html', dict(reason='셀 계획 식별자가 올바르지 않습니다.', return_url=f'/plans/{pk}/'), status=400)
            target_row = get_object_or_404(CellPlanDispatch, pk=dispatch_id, order_id=plan.order_id)
            try:
                outbound_loop.prepare_outbound_dispatch_from_cell_plan(request.user, cell_plan_dispatch_id=target_row.pk)
            except (services.Conflict, ValidationError) as exc:
                msg = exc.messages[0] if isinstance(exc, ValidationError) and exc.messages else str(exc)
                return render(request, 'error.html', dict(heading='반출대 차량 호출을 준비할 수 없습니다', reason=msg, return_url=f'/plans/{pk}/'), status=409)
            messages.info(request, '반출대 차량 호출을 등록했습니다. ROSY 독립 승인 후 도킹이 진행됩니다.')
            return redirect(f'/plans/{pk}/')
        if request.POST.get('action') == 'cell_plan_outbound_loaded':
            try:
                outbound_id = int(request.POST.get('outbound_dispatch_id', ''))
                expected_version = int(request.POST.get('expected_version', ''))
            except (TypeError, ValueError):
                return render(request, 'error.html', dict(reason='적재 확인 형식이 올바르지 않습니다.', return_url=f'/plans/{pk}/'), status=400)
            target_od = get_object_or_404(OutboundDispatch, pk=outbound_id, cell_plan_dispatch__order_id=plan.order_id)
            try:
                outbound_loop.confirm_outbound_loaded(request.user, dispatch_id=target_od.pk, expected_version=expected_version)
            except (services.Conflict, ValidationError):
                return render(request, 'error.html', dict(heading='적재 확인을 기록할 수 없습니다', reason='도킹 상태나 버전이 바뀌었습니다. 화면을 새로 열어 확인하세요.', return_url=f'/plans/{pk}/'), status=409)
            messages.info(request, '차량 적재를 확인했습니다. 출구 이동이 준비됩니다.')
            return redirect(f'/plans/{pk}/')
        if request.POST.get('action') == 'cell_plan_outbound_cancel':
            try:
                dispatch_id = int(request.POST.get('dispatch_id', ''))
            except (TypeError, ValueError):
                return render(request, 'error.html', dict(reason='셀 계획 식별자가 올바르지 않습니다.', return_url=f'/plans/{pk}/'), status=400)
            target_row = get_object_or_404(CellPlanDispatch, pk=dispatch_id, order_id=plan.order_id)
            target_od = get_object_or_404(OutboundDispatch, cell_plan_dispatch=target_row)
            try:
                outbound_loop.cancel_outbound_dispatch(request.user, dispatch_id=target_od.pk,
                                                       reason=request.POST.get('reason') or '운영자 반출 취소')
            except (services.Conflict, ValidationError):
                return render(request, 'error.html', dict(heading='반출을 취소할 수 없습니다', reason='이미 끝났거나 취소할 수 없는 상태입니다. 화면을 새로 열어 확인하세요.', return_url=f'/plans/{pk}/'), status=409)
            messages.info(request, '반출을 취소했습니다. 반출대와 차량 배정이 바로 해제됩니다.')
            return redirect(f'/plans/{pk}/')
        if request.POST.get('action') == 'cell_plan_cancel':
            try:
                dispatch_id = int(request.POST.get('dispatch_id', ''))
            except (TypeError, ValueError):
                return render(request, 'error.html', dict(reason='셀 계획 식별자가 올바르지 않습니다.', return_url=f'/plans/{pk}/'), status=400)
            target_row = get_object_or_404(CellPlanDispatch, pk=dispatch_id, order_id=plan.order_id)
            reason = (request.POST.get('reason') or '운영자 취소 요청').strip() or '운영자 취소 요청'
            try:
                cancelled = cell_plan_dispatch.cancel_cell_plan_dispatch(request.user, dispatch_id=target_row.pk, reason=reason)
            except (services.Conflict, ValidationError, OSError, ValueError) as exc:
                return render(request, 'error.html', dict(heading='셀 계획을 취소할 수 없습니다', reason=refusal_text(exc, '이미 실행되었거나 취소할 수 없는 상태입니다.'), return_url=f'/plans/{pk}/'), status=409)
            messages.info(request, f'셀 계획을 취소했습니다: {cell_plan_dispatch._STATE_LABELS.get(cancelled.state, cancelled.state)}')
            return redirect(f'/plans/{pk}/')
        if preview_result is None:
            if request.POST.get('action') == 'decide':  # a person accepts the algorithm's inbound placement (ADR 0012)
                from harbor_web.inbound_decision import PlacementDecisionForm, decide_placement
                decision_form = PlacementDecisionForm(request.POST)
                if not decision_form.is_valid():
                    return render(request, 'error.html', dict(reason='검토한 지시 버전이나 대안 순번이 올바르지 않습니다.', return_url=f'/plans/{pk}/'), status=400)
                try:
                    decide_placement(request.user, plan_id=pk, expected_version=decision_form.cleaned_data['expected_version'],
                        alternative_rank=decision_form.cleaned_data['alternative_rank'] or 0)
                except (services.Conflict, ValidationError):
                    return render(request, 'error.html', dict(heading='결정할 수 없습니다', reason='요청·계획 또는 선택한 자리의 조건이 맞지 않습니다. 최신 입고 추천을 다시 계산해 확인하세요.', return_url=f'/requests/{plan.order_id}/'), status=409)
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
        return page(request, 'workspace.html', section='outbound',
                    can_confirm_outbound_load=has_role(request.user, 'field', 'operator'),
                    cell_plan_preview=preview_result,
                    **workspace_context(plan.order, plan, review_observation=review_observation))
    if plan.result.get('plan_kind') == 'inbound-placement':
        from harbor_web.placement_view import placement_context
        return page(request, 'placement_plan.html', section='cargo', **placement_context(plan))
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
    from harbor_web.yard_scene import attach_cargo_metadata
    return page(request, 'plan.html', section='inventory', plan=plan, display_moves=display_moves, gate=gate,
                gate_reason=gate_text(gate['reason']), plan_reason=reason_text(plan.result['reason']), command=None,
                observation_stage=observation_stage, yard_scene=attach_cargo_metadata(scene_for([observation_stage])) if observation_stage else None,
                cell_plan_dispatches=cell_plan_dispatch.dispatch_views_for_order(plan.order),
                cell_plan_preview=preview_result,
                can_confirm_outbound_load=has_role(request.user, 'field', 'operator'),
                spatial_observation=bool(plan.observation.payload.get('schema') == 'grid-placement-v1'),
                harbor_cell_plan_ready=bool(plan.result.get('model') == 'grid-placement-v1'
                                            and plan.result.get('status') == 'READY' and plan.result.get('moves')))


@require_roles('viewer', 'operator', 'admin')
@require_http_methods(['GET'])
def candidate_preview(request, pk, rank):
    from harbor_web.recommendation_view import candidate_details
    from harbor_web.workspace import spatial_stages, plan_changes
    from harbor_web.yard_scene import attach_cargo_metadata, scene_for
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
        yard_stages=stages, display_moves=moves, yard_scene=attach_cargo_metadata(scene_for(stages)))


@require_roles('viewer', 'operator', 'admin')
@require_http_methods(['GET'])
def work_list(request):
    from harbor_web.work_hub import work_hub
    return work_hub(request)


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
    awaiting_outbound_load = outbound_loop.awaiting_load_confirmation(command)
    if request.method == 'GET' and not has_role(request.user, 'viewer', 'operator', 'admin') and not (
            (human_evidence.human_evidence_enabled() and human_evidence.awaiting(command) is not None)
            or awaiting_outbound_load):
        from django.core.exceptions import PermissionDenied
        raise PermissionDenied  # field staff see a command only while it waits for their confirmation
    attest_form = None
    if request.method == 'POST' and request.POST.get('action') == 'outbound_dispatch':
        if not has_role(request.user, 'operator'):
            from django.core.exceptions import PermissionDenied
            raise PermissionDenied
        form = CommandForm(request.POST)
        if not form.is_valid():
            return render(request, 'error.html', dict(reason='반출 요청 형식이 올바르지 않습니다.', return_url=f'/work/{pk}/'), status=400)
        try:
            outbound_loop.prepare_outbound_dispatch(
                request.user,
                command_id=pk,
                expected_version=form.cleaned_data['expected_version'],
            )
        except (services.Conflict, ValidationError) as exc:
            msg = exc.messages[0] if isinstance(exc, ValidationError) and exc.messages else str(exc)
            return render(request, 'error.html', dict(heading='반출대 차량 호출을 준비할 수 없습니다', reason=msg, return_url=f'/work/{pk}/'), status=409)
        messages.info(request, '반출대 차량 호출을 등록했습니다. ROSY 독립 승인 후 도킹이 진행됩니다.')
        return redirect(f'/work/{pk}/')
    elif request.method == 'POST' and request.POST.get('action') == 'outbound_loaded':
        if not has_role(request.user, 'field', 'operator'):
            from django.core.exceptions import PermissionDenied
            raise PermissionDenied
        try:
            dispatch_id = int(request.POST.get('dispatch_id', ''))
            expected_version = int(request.POST.get('expected_version', ''))
        except (TypeError, ValueError):
            return render(request, 'error.html', dict(reason='적재 확인 형식이 올바르지 않습니다.', return_url=f'/work/{pk}/'), status=400)
        try:
            outbound_loop.confirm_outbound_loaded(
                request.user,
                dispatch_id=dispatch_id,
                expected_version=expected_version,
            )
        except (services.Conflict, ValidationError):
            return render(request, 'error.html', dict(heading='적재 확인을 기록할 수 없습니다', reason='도킹 상태나 버전이 바뀌었습니다. 화면을 새로 열어 확인하세요.', return_url=f'/work/{pk}/'), status=409)
        messages.info(request, '차량 적재를 확인했습니다. 출구 이동이 준비됩니다.')
        return redirect(f'/work/{pk}/')
    elif request.method == 'POST' and request.POST.get('action') == 'outbound_cancel':
        if not has_role(request.user, 'operator'):
            from django.core.exceptions import PermissionDenied
            raise PermissionDenied
        try:
            dispatch_id = int(request.POST.get('dispatch_id', ''))
        except (TypeError, ValueError):
            return render(request, 'error.html', dict(reason='반출 요청 형식이 올바르지 않습니다.', return_url=f'/work/{pk}/'), status=400)
        target_od = get_object_or_404(OutboundDispatch, pk=dispatch_id, command=command)
        try:
            outbound_loop.cancel_outbound_dispatch(request.user, dispatch_id=target_od.pk,
                                                   reason=request.POST.get('reason') or '운영자 반출 취소')
        except (services.Conflict, ValidationError):
            return render(request, 'error.html', dict(heading='반출을 취소할 수 없습니다', reason='이미 끝났거나 취소할 수 없는 상태입니다. 화면을 새로 열어 확인하세요.', return_url=f'/work/{pk}/'), status=409)
        messages.info(request, '반출을 취소했습니다. 반출대와 차량 배정이 바로 해제됩니다.')
        return redirect(f'/work/{pk}/')
    elif request.method == 'POST' and request.POST.get('action') == 'attest':
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
        if form.is_valid() and action in {'submit', 'reconcile', 'cancel', 'close', 'arm_recovery_request'}:
            try:
                args = dict(command_id=pk, expected_version=form.cleaned_data['expected_version'])
                if action == 'cancel':
                    commands.cancel_command(request.user, reason=form.cleaned_data['reason'], **args)
                elif action == 'close':
                    commands.close_cancelled(request.user, **args)
                elif action == 'submit':
                    commands.submit_command(request.user, **args)
                elif action == 'arm_recovery_request':
                    commands.request_arm_recovery(request.user, **args)
                    messages.info(request, 'ROSY ARM 복구 요청을 제출했습니다. ROSY 기명 운영자 승인 후 진행됩니다.')
                    return redirect(f'/work/{pk}/')
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
                outbound_dispatch=outbound_loop.dispatch_view(command),
                can_confirm_outbound_load=has_role(request.user, 'field', 'operator'),
                gate_reason=gate_text(detail['reason']))
