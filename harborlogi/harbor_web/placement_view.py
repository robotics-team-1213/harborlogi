"""Pinned placement proposals and their source; reading never replans."""
from harbor_web import services
from harbor_web.models import Plan
from harbor_web.yard_view import spatial_yard_view
from harbor_web.yard_scene import present_stage, scene_for


def placement_context(plan):
    from harbor_web.workspace import plan_changes
    from harbor_web.views import reason_text
    from harbor_web.plan_explanation import explain_plan
    source = services.spatial_snapshot(plan.observation)
    target = plan.order.cargo.code
    stages = [present_stage(dict(spatial_yard_view(source, target=target), index=0,
        panel_id=f'placement-{plan.pk}-0', label='관측한 배치 · 입고 화물 배치 전', phase='관측'), plan.condition.payload)]
    projected = services.placement_projection(plan)
    if projected:
        stages.append(present_stage(dict(spatial_yard_view(projected, target=target), index=1,
            panel_id=f'placement-{plan.pk}-1', label='자동 추천 배치 · 실제 관측 아님', phase='입고 추천'), plan.condition.payload))
    previous = Plan.objects.filter(pk=plan.result.get('previous_plan_id'), order_id=plan.order_id).first()
    newer = Plan.objects.filter(order_id=plan.order_id, pk__gt=plan.pk).order_by('-pk').first()  # decide_placement refuses this plan
    return dict(order=plan.order, plan=plan, plan_reason=reason_text(plan.result['reason']), newer_plan=newer,
        yard_stages=stages, observation_stage=stages[0], yard_scene=scene_for(stages), previous_plan=previous,
        plan_changes=plan_changes(plan.order, plan), plan_explanation=explain_plan(plan))
