"""Immutable logical footprints and bounded, atomic outbound proposals."""
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from harbor.layout import YardLayout
from harbor.model import (YARD_ARMS, CargoHolding, OutboundRequest, aware_time,
                          identifier, identifiers, revision)


SPATIAL_SEARCH_LIMIT = 20_000
SPATIAL_CANDIDATE_LIMIT = 20_000


def _integer(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError('Expected an integer within the configured bounds')


@dataclass(frozen=True)
class CargoShape:
    cargo_id: str
    length_cells: int
    footprint_revision: int

    def __post_init__(self):
        identifier(self.cargo_id)
        _integer(self.length_cells, 1, 3)
        if type(self.footprint_revision) is not int or self.footprint_revision < 1:
            raise ValueError('Footprint revision must be positive')


@dataclass(frozen=True)
class Placement:
    cargo_id: str
    area_id: str
    row: int
    column: int
    level: int
    orientation: int = 0

    def __post_init__(self):
        identifier(self.cargo_id)
        identifier(self.area_id)
        for n, limit in ((self.row, 12), (self.column, 12), (self.level, 6)):
            _integer(n, 1, limit)
        if type(self.orientation) is not int or self.orientation not in (0, 90):
            raise ValueError('Orientation must be 0 or 90')


@dataclass(frozen=True)
class Area:
    area_id: str
    rows: int
    columns: int
    levels: int

    def __post_init__(self):
        identifier(self.area_id)
        for n, limit in ((self.rows, 12), (self.columns, 12), (self.levels, 6)):
            _integer(n, 1, limit)


@dataclass(frozen=True)
class SpatialSnapshot:
    revision: int
    observed_at: datetime
    layout: YardLayout
    areas: tuple[Area, ...]
    shapes: tuple[CargoShape, ...]
    placements: tuple[Placement, ...]
    holdings: tuple[CargoHolding, ...] = ()

    def __post_init__(self):
        revision(self.revision)
        aware_time(self.observed_at)
        if not isinstance(self.layout, YardLayout):
            raise ValueError('Expected a yard layout')
        for values, kind in ((self.areas, Area), (self.shapes, CargoShape),
                             (self.placements, Placement), (self.holdings, CargoHolding)):
            if not isinstance(values, tuple) or not all(isinstance(v, kind) for v in values):
                raise ValueError('Expected immutable spatial observations')
        identifiers(tuple(a.area_id for a in self.areas))
        identifiers(tuple(s.cargo_id for s in self.shapes))
        identifiers(tuple(p.cargo_id for p in self.placements) + tuple(h.cargo_id for h in self.holdings))
        identifiers(tuple(h.holder_id for h in self.holdings if h.holder_id is not None))
        areas = {a.area_id: a for a in self.areas}
        if len(areas) > 16 or sum(a.rows*a.columns*a.levels for a in self.areas) > 4096:
            raise ValueError('Map workload exceeds the configured bounds')
        if len(self.placements) + len(self.holdings) > 256:
            raise ValueError('Observation exceeds the cargo limit')
        if areas.get('yard') != Area('yard', self.layout.rows, self.layout.columns, self.layout.levels):
            raise ValueError('The yard dimensions must match its layout')
        if (areas.keys() - {'yard'}) & {c.bay_id for c in self.layout.cells}:
            raise ValueError('Auxiliary areas must not alias primary yard cell identifiers')
        placed = {p.cargo_id for p in self.placements}
        shaped = {s.cargo_id for s in self.shapes}
        if not placed <= shaped <= placed | {h.cargo_id for h in self.holdings}:
            raise ValueError('Every placement requires a pinned shape; unrelated shapes are invalid')
        occupied = {}
        for p in self.placements:
            if p.area_id not in areas:
                raise ValueError('Unknown placement area')
            area = areas[p.area_id]
            for cell in self.cells(p):
                _, row, column, level = cell
                if row > area.rows or column > area.columns or level > area.levels:
                    raise ValueError('Footprint extends outside its area')
                if cell in occupied:
                    raise ValueError('Cargo footprints overlap')
                occupied[cell] = p.cargo_id
        if any(level > 1 and (area, row, column, level-1) not in occupied
               for area, row, column, level in occupied):
            raise ValueError('Cargo footprint requires full support')

    def shape(self, cargo_id):
        return next(s for s in self.shapes if s.cargo_id == cargo_id)

    def placement(self, cargo_id):
        return next(p for p in self.placements if p.cargo_id == cargo_id)

    def cells(self, placement):
        return frozenset((placement.area_id, placement.row + (n if placement.orientation == 90 else 0),
                          placement.column + (n if placement.orientation == 0 else 0), placement.level)
                         for n in range(self.shape(placement.cargo_id).length_cells))

    def occupied(self):
        return {cell: p.cargo_id for p in self.placements for cell in self.cells(p)}

    def with_placements(self, placements):
        return replace(self, placements=placements)

    def position_id(self, placement):
        if placement.area_id == 'yard':
            return f'{self.layout.at(placement.row, placement.column).bay_id}-L{placement.level}'
        return f'{placement.area_id}-R{placement.row}C{placement.column}-L{placement.level}'


@dataclass(frozen=True)
class SpatialPolicy:
    outbound_area: str
    temporary_areas: tuple[str, ...]
    allowed_moves: frozenset[tuple[str, str]]
    arm_reach: frozenset[tuple[str, str]]
    max_observation_age: timedelta
    storage_areas: tuple[str, ...] = ('yard',)

    def __post_init__(self):
        identifier(self.outbound_area)
        identifiers(self.temporary_areas)
        identifiers(self.storage_areas)
        if 'yard' not in self.storage_areas or set(self.storage_areas) & {*self.temporary_areas, self.outbound_area, 'conveyor'}:
            raise ValueError('Storage, temporary and outbound roles must be disjoint')
        if self.outbound_area == 'yard' or any(a in ('yard', 'conveyor', self.outbound_area)
                                               for a in self.temporary_areas):
            raise ValueError('Temporary storage must exclude yard, conveyor and outbound areas')
        if not isinstance(self.allowed_moves, frozenset) or not isinstance(self.arm_reach, frozenset):
            raise ValueError('Expected immutable movement and reach pairs')
        for pair in self.allowed_moves | self.arm_reach:
            if not isinstance(pair, tuple) or len(pair) != 2:
                raise ValueError('Expected pairs')
            for item in pair:
                identifier(item)
        if any(arm not in YARD_ARMS for arm, _ in self.arm_reach):
            raise ValueError('Expected verified yard arms')
        if not isinstance(self.max_observation_age, timedelta) or self.max_observation_age <= timedelta(0):
            raise ValueError('Observation freshness must be positive')


@dataclass(frozen=True)
class SpatialMove:
    cargo_id: str
    source: str
    destination: str
    primary_arm: str
    alternate_arms: tuple[str, ...]
    source_placement: Placement
    destination_placement: Placement
    phase_label: str
    footprint_revision: int


@dataclass(frozen=True)
class SpatialScore:
    earlier_outbound_buried: int
    unknown_outbound_order: int
    different_area_count: int
    rotation_count: int
    different_position_count: int
    same_area_manhattan_distance: int
    final_level_sum: int


@dataclass(frozen=True)
class SpatialAlternative:
    score: SpatialScore
    moves: tuple[SpatialMove, ...]


@dataclass(frozen=True)
class SpatialOptimization:
    objective_version: str
    score: SpatialScore | None
    first_feasible_score: SpatialScore | None
    alternatives: tuple[SpatialAlternative, ...]
    expanded_states: int
    examined_candidates: int
    complete_plans_compared: int
    search_complete: bool
    budget_reason: str | None
    state_limit: int
    candidate_limit: int


@dataclass(frozen=True)
class SpatialProposal:
    status: str
    reason: str
    source_revision: int
    moves: tuple[SpatialMove, ...] = ()
    required_completion: tuple[str, ...] = (
        'outbound_placed_on_stand', 'rehandles_restored', 'arms_retreated',
    )
    optimization: SpatialOptimization | None = None


def validate_schedule(schedule):
    if not isinstance(schedule, dict):
        raise ValueError('Expected outbound schedule ranks')
    for cargo, rank in schedule.items():
        identifier(cargo)
        if (not isinstance(rank, tuple) or len(rank) != 2
                or type(rank[0]) is not int or rank[0] < 0
                or (rank[1] is not None and (type(rank[1]) is not int or rank[1] < 0))):
            raise ValueError('Expected an outbound priority group and optional sequence')


def leaves_earlier(below, above):
    """Whether known-ranked cargo below must leave before the cargo resting on it."""
    if below is None:
        return False
    if above is None or below[0] < above[0]:
        return True
    return below[0] == above[0] and None not in (below[1], above[1]) and below[1] < above[1]


def plan_spatial(snapshot: SpatialSnapshot, request: OutboundRequest, policy: SpatialPolicy,
                 *, now: datetime, schedule: dict[str, tuple[int, int | None]] | None = None,
                 rotation_moves: frozenset = frozenset()) -> SpatialProposal:
    """Compare complete legal plans by future obstruction, restoration cost, then move signature.

    Multi-cell cargo may turn 0<->90 degrees only on (source area, destination
    area) pairs in `rotation_moves`, the installed rotation bindings (design 32 Q5);
    restored rotations count right after changed areas.

    The objective first counts restored cargo resting on cargo scheduled to leave
    earlier, then on cargo of unknown order (design 32 order table), then changed areas, changed row/column anchors, same-area Manhattan
    displacement, and final levels, in that lexicographic order. Without a
    schedule the first term is zero. A bounded search returns its best complete
    incumbent, never a partial plan.
    """
    aware_time(now)
    schedule = {} if schedule is None else schedule
    validate_schedule(schedule)
    areas = {a.area_id: a for a in snapshot.areas}
    referenced = {policy.outbound_area, *policy.temporary_areas, *policy.storage_areas}
    referenced.update(a for edge in policy.allowed_moves for a in edge)
    if not referenced <= areas.keys():
        raise ValueError('Policy references an unknown area')

    def hold(reason):
        return SpatialProposal('HOLD', reason, snapshot.revision)

    if request.expected_revision != snapshot.revision:
        return hold('REVISION_CONFLICT')
    if not timedelta(0) <= now-snapshot.observed_at <= policy.max_observation_age:
        return hold('OBSERVATION_NOT_CURRENT')
    if snapshot.holdings:
        return hold('PHYSICAL_HOLDING_REQUIRES_RECONCILIATION')
    if request.cargo_id not in {p.cargo_id for p in snapshot.placements}:
        return hold('OUTBOUND_NOT_OBSERVED')
    target = snapshot.placement(request.cargo_id)
    if target.area_id == policy.outbound_area:
        return hold('OUTBOUND_ALREADY_ON_STAND_REQUIRES_EVIDENCE')
    if target.area_id not in policy.storage_areas:
        return hold('OUTBOUND_NOT_IN_A_BAY')
    if not policy.arm_reach:
        return hold('ARM_REACH_UNVERIFIED')

    # Every directly supported cargo joins the closure, even across another support column.
    occupied = snapshot.occupied()
    dependencies = {target.cargo_id}
    pending = [target.cargo_id]
    while pending:
        cargo = pending.pop()
        for area, row, column, level in snapshot.cells(snapshot.placement(cargo)):
            above = occupied.get((area, row, column, level+1))
            if above is not None and above not in dependencies:
                dependencies.add(above)
                pending.append(above)
    blockers = dependencies - {target.cargo_id}
    originals = {p.cargo_id: p for p in snapshot.placements}
    visited = {}
    count = 0
    candidates = 0
    complete = 0
    alternatives = []
    first_score = None

    def reach(p):
        if p.area_id != 'yard':
            return {p.area_id}
        return {snapshot.layout.at(r, c).bay_id for _, r, c, _ in snapshot.cells(p)}

    def destinations(current, cargo, area_ids, restore):
        nonlocal candidates
        origin = current.placement(cargo)
        choices = []
        for area_id in area_ids:
            if (origin.area_id, area_id) not in policy.allowed_moves:
                continue
            area = areas[area_id]
            for level in range(1, area.levels+1):
                for row in range(1, area.rows+1):
                    for column in range(1, area.columns+1):
                        if candidates >= SPATIAL_CANDIDATE_LIMIT:
                            raise SearchLimit('CANDIDATE_LIMIT')
                        for orientation in ((origin.orientation,) if snapshot.shape(cargo).length_cells == 1
                                            or (origin.area_id, area_id) not in rotation_moves
                                            else (origin.orientation, 90-origin.orientation)):
                            candidates += 1
                            choices.append(Placement(cargo, area_id, row, column, level, orientation))
        if restore:
            original = originals[cargo]
            choices.sort(key=lambda p: (p != original,
                                       p.area_id != original.area_id,
                                       p.orientation != original.orientation,
                                       (p.row, p.column) != (original.row, original.column),
                                       p.level, p.row, p.column, p.orientation))
        return choices

    class SearchLimit(Exception):
        pass

    def placement_signature(p):
        return (p.cargo_id, p.area_id, p.row, p.column, p.level, p.orientation)

    def move_signature(move):
        return (move.cargo_id, move.source, move.destination, move.primary_arm,
                move.alternate_arms, placement_signature(move.source_placement),
                placement_signature(move.destination_placement), move.phase_label,
                move.footprint_revision)

    def score(current):
        restored = [(originals[cargo], current.placement(cargo)) for cargo in sorted(blockers)]
        occupied = current.occupied()
        buried = unknown = 0
        for cargo in sorted(blockers):
            # Follow the full support chain, as inbound placement does for crossing cargo.
            lower, pending = set(), [cargo]
            while pending:
                for a, r, c, level in current.cells(current.placement(pending.pop())):
                    below = occupied.get((a, r, c, level-1))
                    if below is not None and below not in lower:
                        lower.add(below)
                        pending.append(below)
            relations = [order_relation(schedule.get(other), schedule.get(cargo)) for other in lower]
            buried += relations.count('blocked')
            unknown += relations.count('unknown')
        values = (buried, unknown,
                  sum(a.area_id != b.area_id for a, b in restored),
                  sum(a.orientation != b.orientation for a, b in restored),
                  sum((a.row, a.column) != (b.row, b.column) for a, b in restored),
                  sum(abs(a.row-b.row) + abs(a.column-b.column) for a, b in restored
                      if a.area_id == b.area_id),
                  sum(b.level for _, b in restored))
        return values, SpatialScore(*values)

    def search(current, remaining, restoring, exported, moves, signature):
        nonlocal count, complete, first_score
        if count >= SPATIAL_SEARCH_LIMIT:
            raise SearchLimit('STATE_LIMIT')
        count += 1
        if exported and not restoring:
            values, result_score = score(current)
            complete += 1
            if first_score is None:
                first_score = result_score
            alternatives.append((values, signature, SpatialAlternative(result_score, moves)))
            alternatives.sort(key=lambda item: item[:2])
            del alternatives[5:]
            return
        key = (tuple(sorted(placement_signature(p) for p in current.placements)),
               remaining, restoring, exported)
        # Equal states have identical future costs; only a better prefix can improve a tie.
        if key in visited and visited[key] <= signature:
            return
        visited[key] = signature
        occupied = current.occupied()
        if remaining:
            tasks = [(cargo, policy.temporary_areas, '재취급') for cargo in sorted(remaining)]
        elif not exported:
            tasks = [(target.cargo_id, (policy.outbound_area,), '반출')]
        else:
            tasks = [(cargo, policy.storage_areas, '복귀') for cargo in sorted(restoring)]
        for cargo, area_ids, phase in tasks:
            origin = current.placement(cargo)
            if any((a, r, c, l+1) in occupied for a, r, c, l in current.cells(origin)):
                continue
            for destination in destinations(current, cargo, area_ids, exported):
                try:
                    projected = current.with_placements(tuple(destination if p.cargo_id == cargo else p
                                                               for p in current.placements))
                except ValueError:
                    continue
                locations = reach(origin) | reach(destination)
                arms = tuple(a for a in YARD_ARMS if all((a, loc) in policy.arm_reach for loc in locations))
                if not arms:
                    continue
                move = SpatialMove(cargo, current.position_id(origin), current.position_id(destination),
                                   arms[0], arms[1:], origin, destination, phase,
                                   snapshot.shape(cargo).footprint_revision)
                search(projected, remaining-{cargo},
                       restoring-{cargo} if exported else restoring, exported or cargo == target.cargo_id,
                       moves + (move,), signature + (move_signature(move),))

    budget_reason = None
    try:
        search(snapshot, frozenset(blockers), frozenset(blockers), False, (), ())
    except SearchLimit as exc:
        budget_reason = str(exc)
    report = SpatialOptimization('restoration_lex_v3', alternatives[0][2].score if alternatives else None,
        first_score, tuple(item[2] for item in alternatives), count, candidates, complete,
        budget_reason is None, budget_reason, SPATIAL_SEARCH_LIMIT, SPATIAL_CANDIDATE_LIMIT)
    if not alternatives:
        reason = 'SPATIAL_SEARCH_LIMIT_REACHED' if budget_reason else 'NO_LEGAL_SPATIAL_PLAN'
        return replace(hold(reason), optimization=report)
    return SpatialProposal('READY', 'PROPOSAL_REQUIRES_ADMISSION_AND_EVIDENCE', snapshot.revision,
                           alternatives[0][2].moves, optimization=report)


REARRANGE_BEAM_WIDTH = 8
REARRANGE_COMPLETION = ('rearranged_at_targets', 'arms_retreated')


class SearchLimitReached(Exception):
    pass


def order_relation(below, above):
    """Design 32 order table for cargo resting on `below`: 'blocked', 'unknown' or None (free)."""
    if below is None:
        return 'unknown'
    return 'blocked' if leaves_earlier(below, above) else None


@dataclass(frozen=True)
class RearrangementScore:
    outbound_unfit: int
    inbound_without_place: int
    blocked_pairs: int
    unknown_pairs: int
    cross_supported: int
    free_positions: int
    moves: int
    rotations: int
    changed_cargo: int
    level_sum: int
    blocked_pairs_before: int
    unknown_pairs_before: int
    gain: int

    def key(self):
        return (self.outbound_unfit, self.inbound_without_place, self.blocked_pairs, self.unknown_pairs,
                self.cross_supported, -self.free_positions, self.moves, self.rotations,
                self.changed_cargo, self.level_sum)


def plan_rearrangement(snapshot: SpatialSnapshot, policy: SpatialPolicy,
                       schedule: dict[str, tuple[int, int | None]], *, now: datetime, expected_revision: int,
                       k: int = 2, max_moves: int = 6, outbound_pending: bool = False, requested: bool = False,
                       pending_inbound: tuple[int, ...] = (), rotation_moves: frozenset = frozenset(),
                       reserved: frozenset = frozenset()) -> SpatialProposal:
    """Design 32 R2: reorder stored cargo without an outbound order when the gain pays for the moves.

    A beam search over legal single moves (temporary areas only as waypoints)
    compares complete plans, which leave the temporary areas empty, by
    RearrangementScore.key. READY needs G >= 1, moves <= max_moves and
    moves <= k*G; a user request may instead only add free positions (F).
    Rotation is a candidate only for (source, destination) area pairs in
    `rotation_moves`, the installed rotation bindings (design 29).
    """
    aware_time(now)
    validate_schedule(schedule)
    _integer(k, 1, 10)
    _integer(max_moves, 1, 12)
    if any(n not in (1, 2, 3) for n in pending_inbound):
        raise ValueError('Expected pending inbound lengths of 1 to 3 cells')
    areas = {a.area_id: a for a in snapshot.areas}
    if not {policy.outbound_area, *policy.temporary_areas, *policy.storage_areas} <= areas.keys():
        raise ValueError('Policy references an unknown area')

    def hold(reason, optimization=None):
        return SpatialProposal('HOLD', reason, snapshot.revision, required_completion=REARRANGE_COMPLETION,
                               optimization=optimization)

    if expected_revision != snapshot.revision:
        return hold('REVISION_CONFLICT')
    if not timedelta(0) <= now-snapshot.observed_at <= policy.max_observation_age:
        return hold('OBSERVATION_NOT_CURRENT')
    if snapshot.holdings:
        return hold('PHYSICAL_HOLDING_REQUIRES_RECONCILIATION')
    if outbound_pending:
        return hold('OUTBOUND_PENDING')
    if not policy.arm_reach:
        return hold('ARM_REACH_UNVERIFIED')
    storage, temporary = set(policy.storage_areas), set(policy.temporary_areas)
    if any(p.area_id in temporary for p in snapshot.placements):
        return hold('TEMPORARY_AREA_OCCUPIED')

    outbound = areas[policy.outbound_area]
    lengths = {s.cargo_id: s.length_cells for s in snapshot.shapes}
    originals = {p.cargo_id: p for p in snapshot.placements}

    def footprint(p, n):
        return tuple((p.area_id, p.row + (i if p.orientation == 90 else 0),
                      p.column + (i if p.orientation == 0 else 0), p.level) for i in range(n))

    def fits(cells, occupied):
        area = areas[cells[0][0]]
        return all(r <= area.rows and c <= area.columns and l <= area.levels
                   and (a, r, c, l) not in occupied and (a, r, c, l) not in reserved
                   and (l == 1 or (a, r, c, l-1) in occupied) for a, r, c, l in cells)

    def occupancy(placements):
        return {cell: p.cargo_id for p in placements for cell in footprint(p, lengths[p.cargo_id])}

    def fits_outbound(p):
        n = lengths[p.cargo_id]
        if (p.area_id, policy.outbound_area) in rotation_moves:
            return n <= max(outbound.rows, outbound.columns)
        return n <= (outbound.rows if p.orientation == 90 else outbound.columns)

    def free_positions(occupied, n):
        total = 0
        for area_id in policy.storage_areas:
            area = areas[area_id]
            for orientation in ((0,) if n == 1 else (0, 90)):
                for level in range(1, area.levels+1):
                    for row in range(1, area.rows+1):
                        for column in range(1, area.columns+1):
                            probe = Placement('probe', area_id, row, column, level, orientation)
                            total += fits(footprint(probe, n), occupied)
        return total

    def metrics(placements):
        occupied = occupancy(placements)
        by_id = {p.cargo_id: p for p in placements}
        blocked = unknown = cross = 0
        stored = [p for p in placements if p.area_id in storage]
        for p in stored:
            direct = {occupied.get((a, r, c, l-1)) for a, r, c, l in footprint(p, lengths[p.cargo_id])} - {None}
            cross += len(direct) > 1
            lower, pending = set(), [p.cargo_id]
            while pending:
                cargo = pending.pop()
                for a, r, c, l in footprint(by_id[cargo], lengths[cargo]):
                    below = occupied.get((a, r, c, l-1))
                    if below is not None and below not in lower:
                        lower.add(below)
                        pending.append(below)
            for other in lower:
                relation = order_relation(schedule.get(other), schedule.get(p.cargo_id))
                blocked += relation == 'blocked'
                unknown += relation == 'unknown'
        free = {n: free_positions(occupied, n) for n in (1, 2, 3)}
        return (sum(not fits_outbound(p) for p in stored), sum(free[n] == 0 for n in pending_inbound),
                blocked, unknown, cross, min(free.values()))

    start = metrics(snapshot.placements)

    def score(placements, moves):
        unfit, inbound, blocked, unknown, cross, free = metrics(placements)
        gain = 3*(start[0]-unfit) + 3*(start[1]-inbound) + (start[2]-blocked) + (start[4]-cross)
        final = {p.cargo_id: p for p in placements}
        return RearrangementScore(
            unfit, inbound, blocked, unknown, cross, free, len(moves),
            sum(m.source_placement.orientation != m.destination_placement.orientation for m in moves),
            sum(final[c] != p for c, p in originals.items()), sum(p.level for p in placements),
            start[2], start[3], gain)

    def acceptable(s):
        if s.moves > max_moves:
            return False
        if s.gain >= 1:
            return s.moves <= k*s.gain
        return requested and s.gain == 0 and s.free_positions > start[5]

    def reach(p):
        if p.area_id != 'yard':
            return {p.area_id}
        return {snapshot.layout.at(r, c).bay_id for _, r, c, _ in footprint(p, lengths[p.cargo_id])}

    rotation_skipped = False

    def children(placements):
        nonlocal rotation_skipped
        occupied = occupancy(placements)
        for origin in placements:
            n = lengths[origin.cargo_id]
            if (origin.area_id not in storage | temporary
                    or any((a, r, c, l+1) in occupied for a, r, c, l in footprint(origin, n))):
                continue
            rest = {cell: cargo for cell, cargo in occupied.items() if cargo != origin.cargo_id}
            for area_id in (*policy.storage_areas, *policy.temporary_areas):
                if (origin.area_id, area_id) not in policy.allowed_moves:
                    continue
                area = areas[area_id]
                for orientation in ((0,) if n == 1 else (origin.orientation, 90-origin.orientation)):
                    if orientation != origin.orientation and (origin.area_id, area_id) not in rotation_moves:
                        rotation_skipped = True
                        continue
                    for level in range(1, area.levels+1):
                        for row in range(1, area.rows+1):
                            for column in range(1, area.columns+1):
                                destination = Placement(origin.cargo_id, area_id, row, column, level, orientation)
                                if destination == origin or not fits(footprint(destination, n), rest):
                                    continue
                                arms = tuple(a for a in YARD_ARMS if all(
                                    (a, loc) in policy.arm_reach for loc in reach(origin) | reach(destination)))
                                if arms:
                                    yield origin, destination, arms

    def signature(placements):
        return tuple(sorted((p.cargo_id, p.area_id, p.row, p.column, p.level, p.orientation) for p in placements))

    count = examined = 0
    budget_reason = None
    complete = []
    seen = {signature(snapshot.placements)}
    beam = [(snapshot.placements, ())]
    try:
        for _ in range(max_moves):
            expanded = []
            for placements, moves in beam:
                for origin, destination, arms in children(placements):
                    count += 1
                    if count > SPATIAL_SEARCH_LIMIT:
                        raise SearchLimitReached('STATE_LIMIT')
                    following = tuple(destination if p.cargo_id == origin.cargo_id else p for p in placements)
                    key = signature(following)
                    if key in seen:
                        continue
                    seen.add(key)
                    plan = moves + (SpatialMove(
                        origin.cargo_id, snapshot.position_id(origin), snapshot.position_id(destination),
                        arms[0], arms[1:], origin, destination, '재배치',
                        snapshot.shape(origin.cargo_id).footprint_revision),)
                    result = score(following, plan)
                    waypoints = sum(p.area_id in temporary for p in following)
                    if not waypoints:
                        examined += 1
                        if acceptable(result):
                            complete.append((result, plan))
                    expanded.append(((waypoints,) + result.key(), key, following, plan))
            expanded.sort(key=lambda item: item[:2])
            beam = [(following, plan) for *_, following, plan in expanded[:REARRANGE_BEAM_WIDTH]]
            if not beam:
                break
    except SearchLimitReached as exc:
        budget_reason = str(exc)
    complete.sort(key=lambda item: (item[0].key(), [m.destination for m in item[1]]))
    report = SpatialOptimization(
        'rearrangement_lex_v1', complete[0][0] if complete else None, None,
        tuple(SpatialAlternative(s, m) for s, m in complete[:5]), count, count, examined,
        budget_reason is None, budget_reason, SPATIAL_SEARCH_LIMIT, SPATIAL_CANDIDATE_LIMIT)
    if not complete:
        if start[0] and rotation_skipped:
            return hold('ROTATION_NOT_INSTALLED', report)
        return hold('SPATIAL_SEARCH_LIMIT_REACHED' if budget_reason else 'REARRANGE_NOT_WORTH', report)
    return SpatialProposal('READY', 'PROPOSAL_REQUIRES_ADMISSION_AND_EVIDENCE', snapshot.revision,
                           complete[0][1], required_completion=REARRANGE_COMPLETION, optimization=report)
