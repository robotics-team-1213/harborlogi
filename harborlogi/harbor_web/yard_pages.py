"""Read-only context for the yard page: open READY plans, move arrows, and outbound chain 3D."""
from dataclasses import asdict, replace
import json

from django.utils import timezone

from harbor.model import OutboundRequest
from harbor.spatial import plan_spatial
from harbor_web import services
from harbor_web.inbound_decision import placement_decision
from harbor_web.models import Command, Order, Plan
from harbor_web.order_queue import completed_order_ids, outbound_lanes
from harbor_web.plan_service import _outbound_schedule
from harbor_web.tier_simulation import tier_recommendations
from harbor_web.workspace import _yard_stages
from harbor_web.yard_occupancy import occupancy_map
from harbor_web.yard_rows import place_context, placement_text, route_text
from harbor_web.yard_scene import present_stage, scene_for
from harbor_web.yard_view import spatial_yard_view

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


def outbound_chain_replay(observation):
    """Build one chained multi-frame 3D scene across feasible open OUTBOUND orders (never INBOUND)."""
    if observation is None or observation.payload.get('schema') != 'grid-placement-v1':
        return None
    lanes = outbound_lanes()
    if not any(lane['orders'] for lane in lanes):
        return None
    try:
        current = services.spatial_snapshot(observation)
        policy = services.spatial_policy(observation.condition.payload)
    except ValueError:
        return None
    recommendations = tier_recommendations(observation)
    schedule = _outbound_schedule()
    now = timezone.now()
    labels, slot = place_context(observation.condition.payload)

    stages = [present_stage(
        dict(spatial_yard_view(current), index=0, phase='현재', label='현재 야드 배치', move=None, panel_id='yard-chain-0'),
        observation.condition.payload,
    )]
    arrows = []
    chained_codes = []
    rehandled = set()
    move_number = 0
    ranks = dict(schedule)

    for lane in lanes:
        group = lane['group']
        placed = {placement.cargo_id for placement in current.placements}
        rec = recommendations.get(group)
        if rec is not None:
            best = rec.get('best') or {}
            if not best.get('feasible'):
                break
            raw_steps = best.get('steps') or ()
            tier_codes = [
                getattr(step, 'cargo_id', None) or step['cargo_id']
                for step in raw_steps
                if (getattr(step, 'status', None) or step.get('status')) == 'READY'
            ]
        else:
            tier_codes = [order.cargo.code for order in lane['orders'] if order.cargo.code in placed]
        if not tier_codes:
            continue
        tier_ranks = dict(ranks)
        for pos, code in enumerate(tier_codes):
            tier_ranks[code] = (group, pos)
        tier_failed = False
        for cargo_id in tier_codes:
            if cargo_id not in {placement.cargo_id for placement in current.placements}:
                tier_failed = True
                break
            proposal = plan_spatial(
                current, OutboundRequest(cargo_id, current.revision), policy, now=now, schedule=tier_ranks,
            )
            if proposal.status != 'READY':
                tier_failed = True
                break
            chained_codes.append(cargo_id)
            step_blockers = {m.cargo_id for m in proposal.moves if m.phase_label == '재취급'}
            rehandled.update(step_blockers)
            for move in proposal.moves:
                move_number += 1
                current = current.with_placements(tuple(
                    move.destination_placement if p.cargo_id == move.cargo_id else p
                    for p in current.placements
                ))
                move_dict = json.loads(json.dumps(asdict(move)))
                display = dict(
                    move_dict,
                    number=move_number,
                    source_level=move.source_placement.level,
                    destination_level=move.destination_placement.level,
                    spatial=True,
                    route=route_text(move_dict, labels, slot),
                    source_place=placement_text(move_dict['source_placement'], labels, slot),
                    destination_place=placement_text(move_dict['destination_placement'], labels, slot),
                )
                arrows.append(dict(
                    number=move_number,
                    cargo=move.cargo_id,
                    phase=move.phase_label,
                    source=display['source_place'],
                    destination=display['destination_place'],
                    arm=move.primary_arm,
                ))
                stages.append(present_stage(dict(
                    spatial_yard_view(current, target=cargo_id, blockers=step_blockers),
                    index=move_number,
                    phase=move.phase_label,
                    label=f'{move_number}단계 후 예상 · {cargo_id} {move.phase_label}',
                    move=display,
                    panel_id=f'yard-chain-{move_number}',
                ), observation.condition.payload))
            # Clear the retrieved target from the outbound stand before planning the next container.
            current = replace(
                current,
                shapes=tuple(shape for shape in current.shapes if shape.cargo_id != cargo_id),
                placements=tuple(placement for placement in current.placements if placement.cargo_id != cargo_id),
            )
            tier_ranks.pop(cargo_id, None)
            ranks.pop(cargo_id, None)
        if tier_failed:
            break

    if len(chained_codes) < 2 or move_number == 0:
        return None
    scene = scene_for(stages)
    if scene is None:
        return None
    scene['kind'] = 'outbound-chain'
    scene['rehandle'] = sorted(rehandled)
    scene['arrows'] = arrows
    scene['title'] = '남은 반출 연쇄 3D'
    scene['order'] = chained_codes
    return dict(
        key='outbound-chain',
        scene_id='upcoming-scene-outbound-chain',
        title='남은 반출 연쇄 3D',
        kind='OUTBOUND_CHAIN',
        kind_label='반출 연쇄',
        cargo_codes=chained_codes,
        summary=f"{' → '.join(chained_codes)} · 총 {move_number}회 이동",
        stages=stages,
        scene=scene,
        arrows=arrows,
    )


def yard_page_context(observation):
    """Build upcoming READY plans, outbound chain replay, and current move arrows for one observation."""
    if observation is None:
        return dict(upcoming=[], active_arrows=[], active_upcoming=None, outbound_chain=None)
    open_orders = list(
        Order.objects.exclude(pk__in=completed_order_ids())
        .select_related('cargo')
        .order_by('priority_group', 'sequence', 'pk')
    )
    if not open_orders:
        return dict(upcoming=[], active_arrows=[], active_upcoming=None, outbound_chain=None)
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
    outbound_chain = outbound_chain_replay(observation)
    return dict(
        upcoming=upcoming,
        active_arrows=active_arrows,
        active_upcoming=active_upcoming,
        outbound_chain=outbound_chain,
    )
