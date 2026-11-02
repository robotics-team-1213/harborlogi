"""Bounded inbound placement recommendations, without transport or admission."""
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from harbor.model import YARD_ARMS, aware_time
from harbor.spatial import (CargoShape, Placement, SpatialPolicy, SpatialSnapshot, order_relation,
                           validate_schedule)


PLACEMENT_CANDIDATE_LIMIT = 20_000


@dataclass(frozen=True)
class PlacementAlternative:
    placement: Placement
    footprint_revision: int
    score: tuple[int, int, int]
    rationale: tuple[str, ...]


@dataclass(frozen=True)
class PlacementProposal:
    status: str
    reason: str
    source_revision: int
    footprint_revision: int
    model: str = 'grid-placement-v1'
    plan_kind: str = 'inbound-placement'
    recommendation: Placement | None = None
    score: tuple[int, int, int] | None = None
    rationale: tuple[str, ...] = ()
    alternatives: tuple[PlacementAlternative, ...] = ()
    candidate_count: int = 0
    moves: tuple = ()
    required_completion: tuple[str, ...] = ('inbound_placed_at_target', 'arms_retreated')


def plan_placement(snapshot: SpatialSnapshot, incoming: CargoShape, policy: SpatialPolicy,
                   schedule: dict[str, tuple[int, int | None]], *, now: datetime,
                   reserved_cells: frozenset[tuple[str, int, int, int]] = frozenset()) -> PlacementProposal:
    """Rank legal destinations by outbound obstruction, unknown order, then height.

    Schedule ranks describe outbound instructions only. This deterministic local
    heuristic makes no global optimality, timing, or physical execution claim.
    Reserved cells exclude pending destinations and never supply physical support.
    """
    aware_time(now)
    validate_schedule(schedule)
    areas = {a.area_id: a for a in snapshot.areas}
    referenced = {policy.outbound_area, *policy.temporary_areas, *policy.storage_areas}
    referenced.update(a for edge in policy.allowed_moves for a in edge)
    if not referenced <= areas.keys():
        raise ValueError('Policy references an unknown area')
    if not isinstance(reserved_cells, frozenset):
        raise ValueError('Expected immutable reserved cells')
    for cell in reserved_cells:
        if not isinstance(cell, tuple) or len(cell) != 4 or cell[0] not in areas:
            raise ValueError('Reserved cells require a known area and complete address')
        area = areas[cell[0]]
        if any(type(value) is not int or not 1 <= value <= limit
               for value, limit in zip(cell[1:], (area.rows, area.columns, area.levels))):
            raise ValueError('Reserved cells must lie inside their configured area')

    def hold(reason):
        return PlacementProposal('HOLD', reason, snapshot.revision, incoming.footprint_revision)

    if not timedelta(0) <= now-snapshot.observed_at <= policy.max_observation_age:
        return hold('OBSERVATION_NOT_CURRENT')
    if snapshot.holdings:
        return hold('PHYSICAL_HOLDING_REQUIRES_RECONCILIATION')
    if incoming.cargo_id in {p.cargo_id for p in snapshot.placements}:
        return hold('INBOUND_ALREADY_OBSERVED')
    if len(snapshot.placements) >= 256:
        return hold('OBSERVATION_CARGO_LIMIT_REACHED')
    if not policy.arm_reach:
        return hold('ARM_REACH_UNVERIFIED')

    incoming_rank = schedule.get(incoming.cargo_id)
    occupied = snapshot.occupied()
    supporting = {p.cargo_id: {occupied[a, r, c, level-1]
                              for a, r, c, level in snapshot.cells(p) if level > 1}
                  for p in snapshot.placements}
    yard_bays = {(cell.row, cell.column): cell.bay_id for cell in snapshot.layout.cells}
    choices = []
    examined = 0
    for area_id in sorted(policy.storage_areas):
        area = areas[area_id]
        for row in range(1, area.rows+1):
            for column in range(1, area.columns+1):
                for level in range(1, area.levels+1):
                    for orientation in ((0, 90) if incoming.length_cells > 1 else (0,)):
                        examined += 1
                        if examined > PLACEMENT_CANDIDATE_LIMIT:
                            return hold('PLACEMENT_SEARCH_LIMIT_REACHED')
                        cells = tuple((area_id, row+(n if orientation == 90 else 0),
                                       column+(n if orientation == 0 else 0), level)
                                      for n in range(incoming.length_cells))
                        if any(r > area.rows or c > area.columns or (area_id, r, c, level) in occupied
                               or (area_id, r, c, level) in reserved_cells
                               or (level > 1 and (area_id, r, c, level-1) not in occupied)
                               for _, r, c, _ in cells):
                            continue
                        locations = {yard_bays[r, c] for _, r, c, _ in cells} if area_id == 'yard' else {area_id}
                        if not any(all((arm, location) in policy.arm_reach for location in locations)
                                   for arm in YARD_ARMS):
                            continue
                        destination = Placement(incoming.cargo_id, area_id, row, column, level, orientation)
                        # Use the observation validator as the final geometry/support authority.
                        replace(snapshot, shapes=snapshot.shapes+(incoming,),
                                placements=snapshot.placements+(destination,))
                        lower = set()
                        pending = [occupied[a, r, c, level-1] for a, r, c, level in cells if level > 1]
                        # A crossing support can obstruct cargo outside the incoming footprint.
                        while pending:
                            cargo = pending.pop()
                            if cargo not in lower:
                                lower.add(cargo)
                                pending.extend(supporting[cargo] - lower)
                        earlier = []
                        unknown = []
                        for cargo in sorted(lower):
                            relation = order_relation(schedule.get(cargo), incoming_rank)
                            if relation == 'blocked':
                                earlier.append(cargo)
                            elif relation == 'unknown':
                                unknown.append(cargo)
                        rationale = ('DESTINATION_FOOTPRINT_REACH_VERIFIED',)
                        if incoming_rank is None:
                            rationale += ('INCOMING_OUTBOUND_ORDER_UNKNOWN',)
                        blocked_reason = ('KNOWN_OUTBOUND_BLOCKED_WITH_INCOMING_ORDER_UNKNOWN'
                                          if incoming_rank is None else 'KNOWN_EARLIER_OUTBOUND_BLOCKED')
                        rationale += tuple(f'{blocked_reason}:{cargo}' for cargo in earlier)
                        rationale += tuple(f'LOWER_OUTBOUND_ORDER_UNKNOWN:{cargo}' for cargo in unknown)
                        choices.append(PlacementAlternative(destination, incoming.footprint_revision,
                                                            (len(earlier), len(unknown), level), rationale))
    if not choices:
        return hold('NO_LEGAL_PLACEMENT')
    choices.sort(key=lambda choice: (choice.score, choice.placement.area_id, choice.placement.row,
                                    choice.placement.column, choice.placement.orientation))
    best = choices[0]
    return PlacementProposal('READY', 'PLACEMENT_RECOMMENDATION_REQUIRES_EXECUTION_BINDINGS',
                             snapshot.revision, incoming.footprint_revision,
                             recommendation=best.placement, score=best.score, rationale=best.rationale,
                             alternatives=tuple(choices[1:6]), candidate_count=len(choices))
