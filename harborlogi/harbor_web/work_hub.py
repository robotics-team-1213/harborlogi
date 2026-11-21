"""Read-only work hub built from registered orders and saved execution records."""
from django.db.models import CharField, OuterRef, Q, Subquery
from django.db.models.functions import Cast
from django.utils import timezone

from harbor_web.models import Audit, Command, Order, Plan
from harbor_web.order_queue import completed_order_ids
from harbor_web.rearrangement import current_authorization
from harbor_web.views import page
from harbor_web.work_state import work_state
from harbor_web.yard_rows import place_context, placement_text


def hub_context(*, now=None):
    now = timezone.now() if now is None else now
    start = timezone.localtime(now).replace(hour=0, minute=0, second=0, microsecond=0)
    today_audits = Audit.objects.filter(action='command.completed', object_type='Command',
        created_at__gte=start, created_at__lte=now).values('object_id')
    today_orders = Command.objects.filter(state='COMPLETED').annotate(
        audit_id=Cast('pk', CharField())).filter(audit_id__in=Subquery(today_audits)).values('order_id')
    latest_plan = Plan.objects.filter(order_id=OuterRef('pk')).order_by('-pk')
    orders = list(Order.objects.filter(
        ~Q(pk__in=completed_order_ids()) | Q(pk__in=Subquery(today_orders))
    ).select_related('cargo').annotate(
        latest_plan_id=Subquery(latest_plan.values('pk')[:1])
    ).order_by('priority_group', 'sequence', 'pk'))
    latest_command = Command.objects.filter(order_id=OuterRef('order_id')).order_by('-pk')
    first_completion = Audit.objects.filter(action='command.completed', object_type='Command',
        object_id=Cast(OuterRef('pk'), CharField())).order_by('created_at').values('created_at')[:1]
    commands, completed = {}, {}
    for command in Command.objects.filter(order_id__in=[o.pk for o in orders]).filter(
            Q(pk=Subquery(latest_command.values('pk')[:1])) | Q(state='COMPLETED')
    ).annotate(completed_at=Subquery(first_completion)).order_by('pk'):
        commands[command.order_id] = command
        if command.state == 'COMPLETED':
            previous = completed.get(command.order_id)
            if (previous is None or (command.completed_at is not None and
                    (previous.completed_at is None or command.completed_at < previous.completed_at))):
                completed[command.order_id] = command

    orders = [order for order in orders if order.pk not in completed or (
        completed[order.pk].completed_at is not None and start <= completed[order.pk].completed_at <= now)]
    plans = {p.pk: p for p in Plan.objects.filter(
        pk__in=[o.latest_plan_id for o in orders if o.latest_plan_id]).select_related('condition')}
    past, pending = [], []
    for order in orders:
        command = completed.get(order.pk) or commands.get(order.pk)
        plan = plans.get(order.latest_plan_id)
        recorded_at = command.completed_at if order.pk in completed else None
        state = work_state(command, plan)
        row = dict(order=order, command=command, plan=plan, recorded_at=recorded_at,
            kind={'INBOUND': '입고', 'OUTBOUND': '반출', 'REARRANGE': '재배치'}[order.kind],
            state=state, tone={'진행 중': 'running', '완료': 'done', '처리할 것': 'attention'}.get(state, 'waiting'),
            href=f'/work/{command.pk}/' if command else f'/plans/{plan.pk}/' if plan else f'/requests/{order.pk}/')
        if recorded_at:
            past.append(row)
        else:
            row['place'] = None
            if (order.kind == 'INBOUND' and plan and plan.order_version == order.version
                    and plan.result.get('status') == 'READY'
                    and plan.result.get('plan_kind') == 'inbound-placement'
                    and plan.result.get('recommendation')):
                labels, slot = place_context(plan.condition.payload)
                row['place'] = placement_text(plan.result['recommendation'], labels, slot)
            pending.append(row)

    past.sort(key=lambda row: (row['recorded_at'], row['order'].pk))
    upcoming = sorted(pending, key=lambda row: row['state'] != '진행 중')
    if past and upcoming and upcoming[0]['state'] == '대기':
        past[-1]['state'] = '완료 · 다음 작업 대기 중'
    return dict(timeline=past + upcoming,
        outbound=[row for row in pending if row['order'].kind == 'OUTBOUND'],
        inbound=[row for row in pending if row['order'].kind == 'INBOUND'],
        rearrangements=[row for row in pending if row['order'].kind == 'REARRANGE'],
        authorization=current_authorization(now))


def work_hub(request):
    return page(request, 'work.html', section='work', **hub_context())
