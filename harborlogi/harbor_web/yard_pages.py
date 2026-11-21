"""Read-only context for the yard page: open READY plans, move arrows, and inline 3D scenes."""
from harbor_web.inbound_decision import placement_decision
from harbor_web.models import Command, Order, Plan
from harbor_web.order_queue import completed_order_ids
from harbor_web.workspace import _yard_stages
from harbor_web.yard_occupancy import occupancy_map
from harbor_web.yard_rows import place_context, placement_text
from harbor_web.yard_scene import scene_for

KIND_LABELS = {'OUTBOUND': '반출', 'INBOUND': '입고', 'REARRANGE': '재배치'}
ACTIVE_COMMAND_STATES = ('DISPATCHING', 'RECONCILED', 'PREPARED')


def _arrows_from_moves(display_moves):
    return [
        dict(
            number=move['number'],
            cargo=move['cargo_id'],
            phase=move['phase_label'],
            source=move['source_place'] if move.get('spatial') else f"{move['source']} {move['source_level']}층",
            destination=move['destination_place'] if move.get('spatial') else f"{move['destination']} {move['destination_level']}층",
            arm=move.get('primary_arm', ''),
        )
        for move in display_moves
    ]


def _order_title(order):
    if order.title:
        return order.title
    if order.cargo_id:
        return f'{order.cargo.code} {KIND_LABELS.get(order.kind, order.kind)}'
    return '자동 재배치'


def _command_priority(command):
    if command is None or command.state not in ACTIVE_COMMAND_STATES:
        return (99, 0)
    rank = 0 if command.state in ('DISPATCHING', 'RECONCILED') else 1
    return (rank, -command.pk)


def yard_page_context(observation):
    """Build upcoming READY plans and current move arrows for one selected observation."""
    if observation is None:
        return dict(upcoming=[], active_arrows=[], active_upcoming=None)
    open_orders = list(
        Order.objects.exclude(pk__in=completed_order_ids())
        .select_related('cargo')
        .order_by('priority_group', 'sequence', 'pk')
    )
    if not open_orders:
        return dict(upcoming=[], active_arrows=[], active_upcoming=None)
    order_ids = [order.pk for order in open_orders]
    latest_plans = {}
    for plan in (
        Plan.objects.filter(order_id__in=order_ids, observation=observation)
        .select_related('order__cargo', 'observation__condition', 'condition')
        .order_by('-pk')
    ):
        latest_plans.setdefault(plan.order_id, plan)
    active_commands = {}
    for command in (
        Command.objects.filter(order_id__in=order_ids)
        .exclude(state__in=('COMPLETED', 'CANCELLED'))
        .select_related('plan')
        .order_by('-pk')
    ):
        active_commands.setdefault(command.order_id, command)

    labels, slot = (
        place_context(observation.condition.payload)
        if observation.payload.get('schema') == 'grid-placement-v1'
        else ({}, None)
    )
    upcoming = []
    for order in open_orders:
        plan = latest_plans.get(order.pk)
        if not plan or plan.result.get('status') != 'READY':
            continue
        command = active_commands.get(order.pk)
        kind_label = KIND_LABELS.get(order.kind, order.kind)
        title = _order_title(order)
        stages, scene, arrows, target_label = [], None, [], ''
        summary_parts = []
        if order.kind in ('OUTBOUND', 'REARRANGE'):
            if order.kind == 'OUTBOUND':
                summary_parts.append(order.tier_label)
            moves = plan.result.get('moves') or []
            if moves:
                stages, display_moves = _yard_stages(order, plan, observation)
                arrows = _arrows_from_moves(display_moves)
                occupancy = occupancy_map(stages[0] if stages else None, display_moves)
                scene = scene_for(stages)
                if scene:
                    scene['rehandle'] = occupancy['rehandle_cargo'] if occupancy else []
                    scene['arrows'] = arrows
                    scene['title'] = title
                arms = sorted({a['arm'] for a in arrows if a['arm']})
                summary_parts.append(f'이동 {len(arrows)}회')
                if arms:
                    summary_parts.append(f"팔 {', '.join(arms)}")
            else:
                summary_parts.append('이동 없음')
        elif order.kind == 'INBOUND':
            summary_parts.append(order.tier_label)
            recommendation = plan.result.get('recommendation')
            if recommendation:
                target_label = f'→ {placement_text(recommendation, labels, slot)}'
            summary_parts.append('자리 결정 완료' if placement_decision(plan) else '자리 결정 대기')
        upcoming.append(dict(
            key=f'plan-{plan.pk}',
            order=order,
            plan=plan,
            command=command,
            kind=order.kind,
            kind_label=kind_label,
            title=title,
            summary=' · '.join(summary_parts),
            target_label=target_label,
            stages=stages,
            scene=scene,
            scene_id=f'upcoming-scene-{plan.pk}',
            arrows=arrows,
            active=bool(command and command.state in ACTIVE_COMMAND_STATES),
        ))

    active_candidates = [item for item in upcoming if item['active'] and item['arrows']]
    active_upcoming = (
        min(active_candidates, key=lambda item: _command_priority(item['command']))
        if active_candidates
        else None
    )
    active_arrows = active_upcoming['arrows'] if active_upcoming else []
    return dict(upcoming=upcoming, active_arrows=active_arrows, active_upcoming=active_upcoming)
