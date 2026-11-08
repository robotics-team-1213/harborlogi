"""Recommended retrieval order per priority tier on one observation (KAN-116). Read-only: a GET never writes.

Tiers are simulated in order: a later tier starts from the yard that the earlier tier's best order leaves,
because earlier tiers leave first. A blocked tier stops the simulation of the tiers after it.
"""
from django.utils import timezone

from harbor.sequencing import rank_retrieval_orders
from harbor_web.order_queue import outbound_lanes
from harbor_web.plan_service import _outbound_schedule
from harbor_web.site_config import spatial_policy, spatial_snapshot


def _scenario_view(scenario, orders):
    # Requests whose container is not in the observation keep their place after the simulated ones.
    codes = list(scenario.order) + [code for code in orders if code not in scenario.order]
    return dict(order=[orders[code] for code in scenario.order], codes=scenario.order, feasible=scenario.feasible,
                total_moves=scenario.total_moves, rehandles=scenario.rehandles, exhaustive=scenario.exhaustive,
                steps=scenario.steps, entries=','.join(f'{orders[c].pk}:{orders[c].version}' for c in codes))


def tier_recommendations(observation, lanes=None, *, now=None):
    """{group: recommendation} for tiers with two or more open orders observed in the yard."""
    if observation is None or observation.payload.get('schema') != 'grid-placement-v1':
        return {}
    now = timezone.now() if now is None else now
    lanes = outbound_lanes() if lanes is None else lanes
    current = spatial_snapshot(observation)
    policy = spatial_policy(observation.condition.payload)
    schedule = _outbound_schedule()
    result = {}
    for lane in lanes:
        orders = {order.cargo.code: order for order in lane['orders']}
        placed = {placement.cargo_id for placement in current.placements}
        codes = tuple(code for code in orders if code in placed)
        if not codes:
            continue
        try:
            ranked = rank_retrieval_orders(current, policy, codes, now=now, schedule=schedule, group=lane['group'])
        except ValueError:
            break  # invalid site inputs are reported where plans are made; no recommendation here
        best = ranked[0]
        if len(lane['orders']) >= 2:
            result[lane['group']] = dict(
                best=_scenario_view(best, orders), others=[_scenario_view(s, orders) for s in ranked[1:]],
                current_is_best=tuple(code for code in orders if code in placed) == best.order,
                missing=[code for code in orders if code not in placed], observation=observation)
        if not (best.feasible and best.final is not None):
            break
        current = best.final
    return result
