"""Pinned placement proposals and their source; reading never replans."""
from harbor_web import services
from harbor_web.models import Plan
from harbor_web.inbound_decision import placement_decision
from harbor_web.yard_view import spatial_yard_view
from harbor_web.yard_scene import present_stage, scene_for


def placement_context(plan):
    from harbor_web.workspace import plan_changes
    from harbor_web.presentation import reason_text
    from harbor_web.plan_explanation import explain_plan
    from harbor_web.yard_rows import place_context, placement_text
    source = services.spatial_snapshot(plan.observation)
    target = plan.order.cargo.code
    decision = placement_decision(plan)
    stages = [present_stage(dict(spatial_yard_view(source, target=target), index=0,
        panel_id=f'placement-{plan.pk}-0', label='관측한 배치 · 입고 화물 배치 전', phase='관측'), plan.condition.payload)]
    projected = services.placement_projection(plan)
    if projected:
        stages.append(present_stage(dict(spatial_yard_view(projected, target=target), index=1,
            panel_id=f'placement-{plan.pk}-1', label='자동 추천 배치 · 실제 관측 아님', phase='입고 추천'), plan.condition.payload))
    if decision and decision.detail['recommendation'] != plan.result.get('recommendation'):
        from harbor_web.inbound_decision import projected_snapshot
        chosen = projected_snapshot(source, {'rows': [{'decision': decision}]})
        stages.append(present_stage(dict(spatial_yard_view(chosen, target=target), index=2,
            panel_id=f'placement-{plan.pk}-2', label='결정한 배치 · 실제 배치 아님', phase='결정한 자리'), plan.condition.payload))
    previous = Plan.objects.filter(pk=plan.result.get('previous_plan_id'), order_id=plan.order_id).first()
    newer = Plan.objects.filter(order_id=plan.order_id, pk__gt=plan.pk).order_by('-pk').first()  # decide_placement refuses this plan
    labels, slot = place_context(plan.condition.payload)

    def where(placement):
        return placement_text(placement, labels, slot) if placement else None
    alternatives = [dict(alternative, rank=rank, place=where(alternative['placement']))
                    for rank, alternative in enumerate(plan.result.get('alternatives', []), start=1)]
    return dict(order=plan.order, plan=plan, decision=decision,
        decision_place=where(decision.detail['recommendation']) if decision else None, plan_reason=reason_text(plan.result['reason']), newer_plan=newer,
        recommendation_place=where(plan.result.get('recommendation')), alternatives=alternatives,
        yard_stages=stages, observation_stage=stages[0], yard_scene=scene_for(stages), previous_plan=previous,
        plan_changes=plan_changes(plan.order, plan), plan_explanation=explain_plan(plan))
