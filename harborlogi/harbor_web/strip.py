"""Global status strip context processor (WP05); read-only with O(1) bounded queries."""
from datetime import timedelta

from django.db.models import Max, Subquery
from django.utils import timezone

from harbor_web import access, executor, services
from harbor_web.auth import roles
from harbor_web.models import Audit, Command, FieldEpisode, Observation, Order, Plan
from harbor_web.order_queue import completed_order_ids
from harbor_web.work_state import day_progress, work_state

KIND_LABELS = {'INBOUND': '입고', 'OUTBOUND': '반출', 'REARRANGE': '재배치'}
STATE_TONES = {'진행 중': 'info', '완료': 'good', '완료 · 다음 작업 대기 중': 'good', '처리할 것': 'attention', '대기': 'neutral'}


def _job_title(order):
    kind = KIND_LABELS.get(order.kind, order.kind)
    if order.cargo_id:
        return f'{order.cargo.code} {kind}'
    return '자동 재배치' if order.kind == 'REARRANGE' else kind


def _selected_or_latest_observation(request):
    raw = request.GET.get('observation', '')
    if raw and raw.isascii() and raw.isdigit():
        obs = Observation.objects.select_related('condition').filter(pk=raw).first()
        if obs is not None:
            return obs
    return Observation.objects.select_related('condition').order_by('-pk').first()


def _unmatched_completed_count(commands):
    latest = {}
    for command in commands:
        if command.state == 'COMPLETED':
            latest.setdefault(command.order_id, command)
    count = 0
    for command in latest.values():
        if command.order.kind != 'OUTBOUND':
            continue
        outbound_id = command.binding.get('outbound_id')
        if not any(move.get('cargo_id') == outbound_id for move in command.binding.get('moves', [])):
            count += 1
    return count


def _alert_count(request, *, held, can_read, observation, stale, superseded, commands, now):
    total = 0
    if can_read:
        if observation is None:
            total += 1
        else:
            if stale:
                total += 1
            if superseded:
                total += 1
            commanded_ids = {c.order_id for c in commands}
            latest_ids = Plan.objects.values('order_id').annotate(latest_id=Max('pk')).values('latest_id')
            plans = list(
                Plan.objects.filter(
                    pk__in=Subquery(latest_ids),
                    observation=observation,
                    condition=observation.condition,
                )
                .exclude(order_id__in=commanded_ids)
                .select_related('order')
            )
            if plans:
                digest = services.queue_digest()
                hold = excluded = 0
                for plan in plans:
                    status = plan.result.get('status') if isinstance(plan.result, dict) else None
                    included = (
                        status == 'READY'
                        and not stale
                        and not superseded
                        and plan.order_version == plan.order.version
                        and plan.queue_digest == digest
                    )
                    if status == 'HOLD':
                        hold += 1
                    elif not included:
                        excluded += 1
                total += hold
                if excluded and not (stale or superseded):
                    total += excluded
        total += sum(1 for c in commands if work_state(c) == '처리할 것')
        total += _unmatched_completed_count(commands)

    if held & {'operator', 'admin'} or can_read:
        cached = getattr(request, '_open_field_count', None)
        open_episodes = cached if cached is not None else FieldEpisode.objects.filter(state='OPEN').count()
    else:
        open_episodes = FieldEpisode.objects.filter(state='OPEN', assigned_to=request.user).count()
    return total + open_episodes


def _latest_completed_today(commands, *, start, now):
    completed = {str(c.pk): c for c in commands if c.state == 'COMPLETED'}
    if not completed:
        return None
    audit_id = (
        Audit.objects.filter(
            action='command.completed',
            object_type='Command',
            object_id__in=completed,
            created_at__gte=start,
            created_at__lte=now,
        )
        .order_by('-created_at', '-pk')
        .values_list('object_id', flat=True)
        .first()
    )
    return completed.get(audit_id) if audit_id else None


def _current_job(*, commands, can_read, now):
    running = next((c for c in commands if work_state(c) == '진행 중'), None)
    if running is not None:
        return dict(
            title=_job_title(running.order),
            state='진행 중',
            tone=STATE_TONES['진행 중'],
            href=f'/work/{running.pk}/' if can_read else None,
        )
    attention = next((c for c in commands if work_state(c) == '처리할 것'), None)
    if attention is not None:
        return dict(
            title=_job_title(attention.order),
            state='처리할 것',
            tone=STATE_TONES['처리할 것'],
            href=f'/work/{attention.pk}/' if can_read else None,
        )

    start = timezone.localtime(now).replace(hour=0, minute=0, second=0, microsecond=0)
    next_order = (
        Order.objects.exclude(pk__in=completed_order_ids())
        .select_related('cargo')
        .order_by('priority_group', 'sequence', 'pk')
        .first()
    )
    if next_order is not None:
        next_command = next((c for c in commands if c.order_id == next_order.pk), None)
        next_plan = Plan.objects.filter(order=next_order).order_by('-pk').first()
        safe_plan = next_plan if (next_plan is None or isinstance(next_plan.result, dict)) else Plan(result={})
        next_state = work_state(next_command, safe_plan)
        if next_state == '대기':
            done_today = _latest_completed_today(commands, start=start, now=now)
            if done_today is not None:
                state = (
                    work_state(done_today, next_command=next_command, next_plan=next_plan)
                    if (next_command is not None or next_plan is not None)
                    else '완료 · 다음 작업 대기 중'
                )
                return dict(
                    title=_job_title(done_today.order),
                    state=state,
                    tone=STATE_TONES.get(state, 'neutral'),
                    href=f'/work/{done_today.pk}/' if can_read else None,
                )
        href = None
        if can_read:
            if next_command is not None:
                href = f'/work/{next_command.pk}/'
            elif next_plan is not None:
                href = f'/plans/{next_plan.pk}/'
            else:
                href = f'/requests/{next_order.pk}/'
        return dict(
            title=_job_title(next_order),
            state=next_state,
            tone=STATE_TONES.get(next_state, 'neutral'),
            href=href,
        )

    done_today = _latest_completed_today(commands, start=start, now=now)
    if done_today is not None:
        return dict(
            title=_job_title(done_today.order),
            state='완료',
            tone=STATE_TONES['완료'],
            href=f'/work/{done_today.pk}/' if can_read else None,
        )
    return None


def status_strip(request):
    user = getattr(request, 'user', None)
    if user is None or not user.is_authenticated or not user.is_active or user.pk is None:
        return {}
    now = timezone.now()
    held = roles(user)
    can_read = bool(held & access.READ)
    observation = _selected_or_latest_observation(request) if can_read else Observation.objects.order_by('-pk').first()
    stale = superseded = False
    if observation is not None and can_read:
        age = now - observation.observed_at
        stale = not timedelta(0) <= age <= services.constraints(observation.condition.payload).max_observation_age
        superseded = Observation.objects.filter(source_id=observation.source_id, pk__gt=observation.pk).exists()
    elif observation is not None:
        age = now - observation.observed_at
        stale = age < timedelta(0)

    commands = list(Command.objects.select_related('order__cargo').order_by('-pk'))
    alert_count = _alert_count(
        request,
        held=held,
        can_read=can_read,
        observation=observation,
        stale=stale,
        superseded=superseded,
        commands=commands,
        now=now,
    )
    progress = day_progress(now=now)
    current_job = _current_job(commands=commands, can_read=can_read, now=now)
    yard_updated = timezone.localtime(observation.observed_at).strftime('%H:%M') if observation is not None else None
    exec_status = executor.status()
    return dict(
        status_strip=dict(
            alert_count=alert_count,
            drive_label=f'처리할 것 {alert_count}건' if alert_count else '자동 운전 중',
            drive_tone='critical' if alert_count else 'good',
            day_progress=progress,
            current_job=current_job,
            yard_updated=yard_updated,
            yard_stale=stale,
            equipment_label='장비 상태 연동 전',
            executor=exec_status,
            executor_label=exec_status['label'],
            executor_tone=exec_status['tone'],
            estop_note='ROSY Fleet에서 정지 · 연동 전',
            state_note='상태는 ROSY 실행 결과로 바뀜',
        )
    )
