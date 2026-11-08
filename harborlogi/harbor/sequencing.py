"""Retrieval order inside one priority tier, found by simulating each order on the observed yard (KAN-116).

Each candidate order is played out with the existing single-retrieval planner: the planned moves are
applied, the retrieved container leaves the yard, and the next container is planned on the result.
Orders are compared by feasibility, total moves, rehandles, then the order they were given in. Small
tiers are searched exhaustively; larger ones take the cheapest next retrieval at each step.
"""
from dataclasses import dataclass, field, replace
from datetime import datetime
from itertools import permutations

from harbor.model import OutboundRequest
from harbor.spatial import SpatialPolicy, SpatialSnapshot, plan_spatial

EXACT_LIMIT = 4  # 4! = 24 complete simulations per tier


@dataclass(frozen=True)
class RetrievalStep:
    cargo_id: str
    status: str
    reason: str
    move_count: int = 0
    rehandle_count: int = 0


@dataclass(frozen=True)
class RetrievalScenario:
    order: tuple[str, ...]
    feasible: bool
    total_moves: int
    rehandles: int
    steps: tuple[RetrievalStep, ...]
    exhaustive: bool
    final: SpatialSnapshot | None = field(default=None, compare=False, repr=False)


def _after(snapshot, proposal, target):
    """The yard after one READY retrieval: moved cargo at their last destination, the target gone."""
    final = {p.cargo_id: p for p in snapshot.placements}
    for move in proposal.moves:
        final[move.cargo_id] = move.destination_placement
    final.pop(target, None)
    return replace(snapshot, shapes=tuple(s for s in snapshot.shapes if s.cargo_id != target),
                   placements=tuple(final.values()))


def _retrieve(snapshot, policy, code, *, now, ranks):
    proposal = plan_spatial(snapshot, OutboundRequest(code, snapshot.revision), policy, now=now, schedule=ranks)
    if proposal.status != 'READY':
        return RetrievalStep(code, proposal.status, proposal.reason), None
    rehandles = next(i for i, move in enumerate(proposal.moves) if move.cargo_id == code)
    return RetrievalStep(code, 'READY', proposal.reason, len(proposal.moves), rehandles), _after(snapshot, proposal, code)


def _play(snapshot, policy, order, *, now, schedule, group, exhaustive):
    current, steps = snapshot, []
    ranks = dict(schedule)
    ranks.update({code: (group, position) for position, code in enumerate(order)})  # the simulated order is the tier's sequence
    for code in order:
        step, after = _retrieve(current, policy, code, now=now, ranks=ranks)
        steps.append(step)
        if after is None:
            return RetrievalScenario(tuple(order), False, sum(s.move_count for s in steps),
                                     sum(s.rehandle_count for s in steps), tuple(steps), exhaustive)
        current = after
        ranks.pop(code)
    return RetrievalScenario(tuple(order), True, sum(s.move_count for s in steps),
                             sum(s.rehandle_count for s in steps), tuple(steps), exhaustive, current)


def _greedy(snapshot, policy, codes, *, now, schedule, group, first=None):
    """Cheapest next retrieval at each step; ties keep the given order. `first` forces the opening pick."""
    remaining, order = list(codes), []
    current, ranks = snapshot, dict(schedule)
    ranks.update({code: (group, position) for position, code in enumerate(codes)})
    while remaining:
        options = []
        for code in ([first] if first and not order else remaining):
            step, after = _retrieve(current, policy, code, now=now, ranks=ranks)
            options.append((after is None, step.move_count, codes.index(code), code, after))
        blocked, _, _, code, after = min(options, key=lambda option: option[:3])
        order.append(code)
        remaining.remove(code)
        if blocked:
            order.extend(remaining)
            break
        current = after
        ranks.pop(code)
    return _play(snapshot, policy, order, now=now, schedule=schedule, group=group, exhaustive=False)


def _key(codes):
    return lambda s: (not s.feasible, s.total_moves, s.rehandles, [codes.index(code) for code in s.order])


def rank_retrieval_orders(snapshot: SpatialSnapshot, policy: SpatialPolicy, codes: tuple[str, ...], *, now: datetime,
                          schedule: dict, group: int, keep: int = 2, exact_limit: int = EXACT_LIMIT) -> list[RetrievalScenario]:
    """Best `keep` retrieval orders of `codes`; `schedule` ranks every other cargo as the planner expects."""
    codes = tuple(codes)
    if len(codes) <= exact_limit:
        ranked = sorted((_play(snapshot, policy, order, now=now, schedule=schedule, group=group, exhaustive=True)
                         for order in permutations(codes)), key=_key(codes))
        return ranked[:keep]
    best = _greedy(snapshot, policy, codes, now=now, schedule=schedule, group=group)
    ranks = dict(schedule)
    ranks.update({code: (group, position) for position, code in enumerate(codes)})
    openings = []
    for code in codes:  # alternatives open with the next cheapest container, then continue greedily
        step, after = _retrieve(snapshot, policy, code, now=now, ranks=ranks)
        openings.append((after is None, step.move_count, codes.index(code), code))
    others = [_greedy(snapshot, policy, codes, now=now, schedule=schedule, group=group, first=code)
              for *_, code in sorted(openings) if code != best.order[0]][:keep - 1]
    return sorted([best, *others], key=_key(codes))[:keep]
