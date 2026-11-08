"""One order's flow as two lines, assembled from existing records (ADR 0019, 0020).

Plan line: the plan and its expected steps; steps have an order but no time, so they are only "expected".
Actual line: only records that carry their own time. Nothing is synthesized, interpolated or inferred,
and completion is only a Command in COMPLETED (the business ledger's reconciliation result).
"""
from harbor_web.command_models import EvidenceSubmission, HumanAttestation
from harbor_web.models import Audit, Command, CommandFact, DepartureIntent, FieldConfirmation, Plan
from harbor_web.operations import move_phases
from harbor_web.workspace import COMMAND_LABELS

LABELS = dict(COMMAND_LABELS, CANCELLED='취소됨', COMPLETED='업무 완료(업무 장부 대조)')


def _place(move, side):
    placement = move.get(f'{side}_placement')
    return placement.get('bay_id') if isinstance(placement, dict) and placement.get('bay_id') else move.get(side, '?')


def _steps(plan):
    return [dict(phase=phase, text=f"{move['cargo_id']} · {_place(move, 'source')} → {_place(move, 'destination')}")
            for move, phase in zip(plan.result.get('moves') or [], move_phases(plan, plan.order))]


def order_timeline(order):
    plans = list(Plan.objects.filter(order=order).select_related('order__cargo').order_by('created_at', 'pk'))
    plan_line = [dict(at=plan.created_at, source=f'Plan #{plan.pk}', href=f'/plans/{plan.pk}/',
                      text=f"계획 검토 {plan.result.get('status')} (요청 v{plan.order_version})", steps=_steps(plan))
                 for plan in plans]
    commands = list(Command.objects.filter(order=order))
    events = []
    for command in commands:
        events.append(dict(at=command.created_at, kind='실행 지시', source=f'Plan #{command.plan_id} 지시',
                           href=f'/plans/{command.plan_id}/', text='실행 지시 생성'))
    # Every state change is saved with an immutable audit row in the same transaction (commands._save).
    plans_by_command = {str(c.pk): c.plan_id for c in commands}
    for audit in Audit.objects.filter(object_type='Command', object_id__in=plans_by_command,
                                      action__startswith='command.').exclude(action='command.prepare').order_by('created_at', 'pk'):
        state = audit.action.removeprefix('command.').upper()
        plan_id = plans_by_command[audit.object_id]
        events.append(dict(at=audit.created_at, kind='지시 상태', source=f'Harbor 저장 시각 · 감사 기록 #{audit.pk}',
                           text=f'Plan #{plan_id} 지시 → {LABELS.get(state, state)}'
                           + (f" · 사유 코드 {audit.detail['reason']}" if audit.detail.get('reason') else '')))
    for fact in CommandFact.objects.filter(command__in=commands):
        events.append(dict(at=fact.read_at, kind='ROSY 조회', source=f'ROSY 조회 사실 #{fact.pk}',
                           text=f"작업 {(fact.job or {}).get('status', '상태 미상')}" + (' · 물리 receipt 있음' if fact.receipt else '')))
    for evidence in EvidenceSubmission.objects.filter(command__in=commands):
        events.append(dict(at=evidence.received_at, kind='업무 근거', source=f'근거 제출 #{evidence.pk}',
                           text='내려놓기·퇴피 근거 접수(대조 전)'))
    for human in HumanAttestation.objects.filter(command__in=commands).select_related('created_by'):
        events.append(dict(at=human.created_at, kind='사람 확인', source=f'현장 확인 기록 #{human.pk} · {human.created_by.get_username()}',
                           href=f'/work/{human.command_id}/',
                           text=f"{'최종' if human.phase == 'final' else str(human.step_index + 1) + '번째 이동'} "
                                f"{human.cargo_id} → {human.location} 육안 확인, 팔 {'·'.join(human.arms)} 구역 밖 대기 확인 (측정값 아님)"))
    for confirmation in FieldConfirmation.objects.filter(episode__order=order).select_related('episode'):
        events.append(dict(at=confirmation.observed_at, kind='현장 확인', source=f'현장 확인 #{confirmation.pk}',
                           href=f'/field/{confirmation.episode_id}/',
                           text=f'{confirmation.episode.vehicle_id} 현장 확인 {confirmation.outcome}'))
    for departure in DepartureIntent.objects.filter(episode__order=order):
        events.append(dict(at=departure.created_at, kind='출발', source=f'출발 의도 #{departure.pk}',
                           href=f'/field/{departure.episode_id}/', text=f'{departure.vehicle_id} 출발 의도 기록'))
    events.sort(key=lambda event: event['at'])  # stable: same-time records keep their source order
    return dict(plan_line=plan_line, actual_line=events)
