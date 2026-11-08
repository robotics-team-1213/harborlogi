import unittest
from dataclasses import asdict, astuple, replace
from itertools import product
from time import perf_counter
from unittest.mock import patch

from test_spatial import NOW, snapshot, policy
from harbor.layout import YardCell, YardLayout
from harbor.model import OutboundRequest
from harbor.spatial import Area, CargoShape, Placement, SpatialSnapshot, plan_spatial


class RankedSpatialTests(unittest.TestCase):
    def source(self):
        return snapshot([('target', 1), ('block', 1)], [
            Placement('target', 'yard', 1, 1, 1),
            Placement('block', 'yard', 1, 1, 2)],
            temp=Area('temp', 1, 1, 1), stand=Area('stand', 1, 1, 1))

    def plan(self, source=None):
        return plan_spatial(source or self.source(), OutboundRequest('target', 4), policy(), now=NOW)

    def test_reports_ranked_complete_alternatives_and_budget(self):
        source = self.source()
        original = source.placements
        result = self.plan(source)
        report = result.optimization
        self.assertEqual(report.objective_version, 'restoration_lex_v3')
        self.assertTrue(report.search_complete)
        self.assertIsNone(report.budget_reason)
        self.assertGreater(report.complete_plans_compared, 1)
        self.assertEqual(len(report.alternatives), 5)
        self.assertEqual(report.alternatives[0].moves, result.moves)
        self.assertEqual(asdict(report.score), dict(earlier_outbound_buried=0, unknown_outbound_order=0, different_area_count=0, rotation_count=0,
            different_position_count=0, same_area_manhattan_distance=0, final_level_sum=1))
        self.assertEqual(result, self.plan(source))
        self.assertEqual(source.placements, original)

    def test_budget_before_and_after_complete_incumbent(self):
        for limit, expected in ((0, 'HOLD'), (4, 'READY')):
            with self.subTest(limit=limit), patch('harbor.spatial.SPATIAL_SEARCH_LIMIT', limit):
                result = self.plan()
                self.assertEqual(result.status, expected)
                self.assertFalse(result.optimization.search_complete)
                self.assertEqual(result.optimization.budget_reason, 'STATE_LIMIT')
                self.assertEqual(bool(result.moves), expected == 'READY')
                self.assertEqual(result.optimization.expanded_states, limit)


    def ranked_fixture(self, rotated=False):
        rows, columns = (3, 1) if rotated else (1, 3)
        layout = YardLayout(rows, columns, 3, tuple(YardCell(f'Y{r}{c}', r, c)
            for r, c in product(range(1, rows+1), range(1, columns+1))))
        source = SpatialSnapshot(4, NOW, layout,
            (Area('yard', rows, columns, 3), Area('temp', rows, columns, 1),
             Area('stand', 2 if rotated else 1, 1 if rotated else 2, 1)),
            tuple(CargoShape(c, length, 1) for c, length in [('target', 2), ('b', 2), ('a', 1)]),
            tuple(Placement(c, 'yard', 1, 1, level, 90 if rotated else 0)
                  for c, level in [('target', 1), ('b', 2), ('a', 3)]))
        constraints = replace(policy(), arm_reach=frozenset(('A', location)
            for location in ['temp', 'stand', *(c.bay_id for c in layout.cells)]))
        return source, constraints

    def test_calculated_winner_beats_first_feasible_and_matches_exhaustive_oracle(self):
        for rotated in (False, True):
            with self.subTest(rotated=rotated):
                source, constraints = self.ranked_fixture(rotated)
                # 'a' leaves before 'b', so restacking 'a' on 'b' is free and the search must find it.
                schedule = {'a': (0, None), 'b': (1, None)}
                result = plan_spatial(source, OutboundRequest('target', 4), constraints, now=NOW, schedule=schedule)
                expected = exhaustive_oracle(source, constraints, {'a', 'b'}, schedule)
                self.assertTrue(result.optimization.search_complete)
                self.assertEqual(astuple(result.optimization.score), expected[0])
                self.assertEqual(tuple(astuple(m)[:7] for m in result.moves), expected[1])
                self.assertLess(astuple(result.optimization.score), astuple(result.optimization.first_feasible_score))
                self.assertEqual(astuple(result.optimization.score), (0, 0, 0, 0, 0, 0, 3))
                signatures = []
                for alternative in result.optimization.alternatives:
                    final = replay(self, source, constraints, alternative.moves)
                    self.assertEqual(astuple(alternative.score), restoration_score(source, final, {'a', 'b'}, schedule))
                    signatures.append(tuple(astuple(m) for m in alternative.moves))
                self.assertEqual(len(signatures), len(set(signatures)))
                shuffled = replace(source, placements=tuple(reversed(source.placements)))
                self.assertEqual(result.moves, plan_spatial(shuffled, OutboundRequest('target', 4),
                    constraints, now=NOW, schedule=schedule).moves)

    def test_candidate_budget_after_incumbent_retains_replayable_complete_plan(self):
        source, constraints = self.ranked_fixture()
        with patch('harbor.spatial.SPATIAL_CANDIDATE_LIMIT', 32):
            result = plan_spatial(source, OutboundRequest('target', 4), constraints, now=NOW)
        self.assertEqual(result.status, 'READY')
        self.assertFalse(result.optimization.search_complete)
        self.assertEqual(result.optimization.budget_reason, 'CANDIDATE_LIMIT')
        self.assertEqual(result.optimization.examined_candidates, 32)
        replay(self, source, constraints, result.moves)

    def test_bounded_fixture_benchmark_counts_and_repeatability(self):
        source, constraints = self.ranked_fixture()
        results = [plan_spatial(source, OutboundRequest('target', 4), constraints, now=NOW) for _ in range(3)]
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[1], results[2])
        report = results[0].optimization
        self.assertEqual((report.expanded_states, report.examined_candidates, report.complete_plans_compared),
                         (34, 142, 16))

    def test_later_better_prefix_reopens_identical_state_for_stable_tie(self):
        layout = YardLayout(1, 2, 2, (YardCell('Y1', 1, 1), YardCell('Y2', 1, 2)))
        source = SpatialSnapshot(4, NOW, layout,
            (Area('yard', 1, 2, 2), Area('ztemp', 1, 1, 1), Area('mtemp', 1, 1, 1), Area('atemp', 1, 1, 1), Area('stand', 1, 2, 1)),
            (CargoShape('target', 2, 1), CargoShape('a', 1, 1), CargoShape('b', 1, 1)),
            (Placement('target', 'yard', 1, 1, 1), Placement('a', 'yard', 1, 1, 2),
             Placement('b', 'yard', 1, 2, 2)))
        constraints = replace(policy(), temporary_areas=('ztemp', 'mtemp', 'atemp'),
            allowed_moves=frozenset({('yard', 'ztemp'), ('yard', 'atemp'),
                                     ('yard', 'mtemp'), ('mtemp', 'yard'), ('ztemp', 'yard'), ('atemp', 'yard'), ('yard', 'stand')}),
            arm_reach=frozenset(('A', location) for location in ('Y1', 'Y2', 'ztemp', 'mtemp', 'atemp', 'stand')))
        expected_score, expected_signature = exhaustive_oracle(source, constraints, {'a', 'b'})
        result = plan_spatial(source, OutboundRequest('target', 4), constraints, now=NOW)
        self.assertTrue(result.optimization.search_complete)
        self.assertEqual(astuple(result.optimization.score), expected_score)
        self.assertEqual(tuple(astuple(m)[:7] for m in result.moves), expected_signature)
        self.assertEqual(result.moves[0].destination_placement.area_id, 'atemp')
        reordered = replace(constraints, temporary_areas=tuple(reversed(constraints.temporary_areas)))
        self.assertEqual(result.moves, plan_spatial(source, OutboundRequest('target', 4), reordered, now=NOW).moves)

    def test_nonzero_displacement_and_cross_area_scores_match_oracle(self):
        crossing = snapshot([('target', 3), ('support', 1), ('a', 2)], [
            Placement('target', 'yard', 1, 1, 1), Placement('support', 'yard', 2, 2, 1),
            Placement('a', 'yard', 1, 2, 2, 90)],
            temp=Area('temp', 2, 1, 1), stand=Area('stand', 1, 3, 1))
        other_area = replace(self.source(), areas=self.source().areas+(Area('store', 1, 2, 1),))
        cross_policy = replace(policy(), storage_areas=('yard', 'store'),
            allowed_moves=(policy().allowed_moves-{('temp', 'yard')}) | {('temp', 'store')},
            arm_reach=policy().arm_reach | {('A', 'store')})
        for source, constraints, blockers, expected in [
                (crossing, policy(), {'a'}, (0, 0, 0, 0, 1, 1, 1)),
                (other_area, cross_policy, {'block'}, (0, 0, 1, 0, 0, 0, 1))]:
            with self.subTest(expected=expected):
                result = plan_spatial(source, OutboundRequest('target', 4), constraints, now=NOW)
                self.assertTrue(result.optimization.search_complete)
                self.assertEqual(astuple(result.optimization.score), expected)
                oracle_score, signature = exhaustive_oracle(source, constraints, blockers)
                self.assertEqual(astuple(result.optimization.score), oracle_score)
                self.assertEqual(tuple(astuple(m)[:7] for m in result.moves), signature)
                replay(self, source, constraints, result.moves)

    def test_restoration_avoids_burying_cargo_that_leaves_earlier(self):
        source = snapshot([('c', 1), ('target', 1), ('b', 1)], [
            Placement('c', 'yard', 1, 1, 1), Placement('target', 'yard', 1, 1, 2),
            Placement('b', 'yard', 1, 1, 3)])
        for schedule, restored, expected in [
                # Unknown order now counts (design 32): with no schedule 'b' no longer goes back onto 'c'.
                ({}, Placement('b', 'yard', 1, 2, 1), (0, 0, 0, 0, 1, 1, 1)),
                ({'c': (0, None)}, Placement('b', 'yard', 1, 2, 1), (0, 0, 0, 0, 1, 1, 1)),
                ({'b': (0, None), 'c': (1, None)}, Placement('b', 'yard', 1, 1, 2), (0, 0, 0, 0, 0, 0, 2)),
                ({'b': (2, 1), 'c': (2, 0)}, Placement('b', 'yard', 1, 2, 1), (0, 0, 0, 0, 1, 1, 1))]:
            with self.subTest(schedule=schedule):
                result = plan_spatial(source, OutboundRequest('target', 4), policy(), now=NOW, schedule=schedule)
                self.assertEqual(result.status, 'READY')
                self.assertEqual(result.optimization.objective_version, 'restoration_lex_v3')
                self.assertEqual(result.moves[-1].destination_placement, restored)
                self.assertEqual(astuple(result.optimization.score), expected)
                replay(self, source, policy(), result.moves)
        with self.assertRaises(ValueError):
            plan_spatial(source, OutboundRequest('target', 4), policy(), now=NOW, schedule={'c': (-1, None)})


def restoration_score(original, placements, blockers, schedule=None):
    pairs = [(original.placement(c), next(p for p in placements if p.cargo_id == c)) for c in blockers]
    final = original.with_placements(tuple(placements))
    occupied = final.occupied()

    def lower(cargo):  # every cargo in the support chain below; without a schedule each is an unknown pair
        found, pending = set(), [cargo]
        while pending:
            for a, r, c, level in final.cells(final.placement(pending.pop())):
                below = occupied.get((a, r, c, level-1))
                if below is not None and below not in found:
                    found.add(below)
                    pending.append(below)
        return found

    def relation(below, above):  # independent copy of the design 32 order table
        if below is None:
            return 'unknown'
        if above is None or below[0] < above[0]:
            return 'blocked'
        return 'blocked' if below[0] == above[0] and None not in (below[1], above[1]) and below[1] < above[1] else None
    ranks = schedule or {}
    relations = [relation(ranks.get(other), ranks.get(c)) for c in blockers for other in lower(c)]
    return (relations.count('blocked'), relations.count('unknown'), sum(a.area_id != b.area_id for a, b in pairs),
            sum(a.orientation != b.orientation for a, b in pairs),
            sum((a.row, a.column) != (b.row, b.column) for a, b in pairs),
            sum(abs(a.row-b.row)+abs(a.column-b.column) for a, b in pairs if a.area_id == b.area_id),
            sum(b.level for _, b in pairs))


def replay(case, source, constraints, moves):
    current = source
    for move in moves:
        origin, destination = move.source_placement, move.destination_placement
        case.assertEqual(current.placement(move.cargo_id), origin)
        case.assertEqual(origin.orientation, destination.orientation)
        case.assertIn((origin.area_id, destination.area_id), constraints.allowed_moves)
        occupied = current.occupied()
        case.assertFalse(any((a, r, c, level+1) in occupied for a, r, c, level in current.cells(origin)))
        for placement in (origin, destination):
            locations = ({current.layout.at(r, c).bay_id for _, r, c, _ in current.cells(placement)}
                         if placement.area_id == 'yard' else {placement.area_id})
            for arm in (move.primary_arm, *move.alternate_arms):
                case.assertTrue(all((arm, location) in constraints.arm_reach for location in locations))
        current = current.with_placements(tuple(destination if p.cargo_id == move.cargo_id else p
                                               for p in current.placements))
    case.assertEqual(current.placement('target').area_id, constraints.outbound_area)
    return current.placements


def exhaustive_oracle(source, constraints, blockers, schedule=None):
    """Tiny independent exhaustive enumeration, without production search/dominance helpers."""
    shapes = {s.cargo_id: s.length_cells for s in source.shapes}
    areas = {a.area_id: a for a in source.areas}
    winners = []

    def cells(p):
        return [(p.area_id, p.row+(i if p.orientation else 0),
                 p.column+(0 if p.orientation else i), p.level) for i in range(shapes[p.cargo_id])]

    def valid(placements):
        occupied = set()
        for p in placements:
            area = areas[p.area_id]
            for a, row, column, level in cells(p):
                if row > area.rows or column > area.columns or level > area.levels:
                    return False
                cell = (a, row, column, level)
                if cell in occupied:
                    return False
                occupied.add(cell)
        return all(level == 1 or (a, r, c, level-1) in occupied for a, r, c, level in occupied)

    def walk(placements, removed, restored, exported, moves):
        if exported and restored == blockers:
            winners.append((restoration_score(source, placements, blockers, schedule), moves))
            return
        if removed != blockers:
            tasks = [(c, constraints.temporary_areas) for c in blockers-removed]
        elif not exported:
            tasks = [('target', (constraints.outbound_area,))]
        else:
            tasks = [(c, constraints.storage_areas) for c in blockers-restored]
        occupied = {cell for p in placements for cell in cells(p)}
        for cargo, destination_areas in tasks:
            origin = next(p for p in placements if p.cargo_id == cargo)
            if any((a, r, c, level+1) in occupied for a, r, c, level in cells(origin)):
                continue
            for area_id in destination_areas:
                if (origin.area_id, area_id) not in constraints.allowed_moves:
                    continue
                area = areas[area_id]
                for row, column, level in product(range(1, area.rows+1), range(1, area.columns+1), range(1, area.levels+1)):
                    destination = Placement(cargo, area_id, row, column, level, origin.orientation)
                    projected = tuple(destination if p.cargo_id == cargo else p for p in placements)
                    if not valid(projected):
                        continue
                    locations = set()
                    for p in (origin, destination):
                        if p.area_id == 'yard':
                            locations.update(source.layout.at(r, c).bay_id for _, r, c, _ in cells(p))
                        else:
                            locations.add(p.area_id)
                    arms = tuple(a for a in ('A', 'B') if all((a, loc) in constraints.arm_reach for loc in locations))
                    if not arms:
                        continue
                    # Phase and shape revision are fixed once these move fields agree.
                    signature = (cargo, source.position_id(origin), source.position_id(destination), arms[0],
                                 arms[1:], astuple(origin), astuple(destination))
                    walk(projected, removed | ({cargo} if cargo in blockers else set()),
                         restored | ({cargo} if exported else set()), exported or cargo == 'target', moves+(signature,))

    walk(source.placements, set(), set(), False, ())
    return min(winners)


def benchmark():
    source, constraints = RankedSpatialTests().ranked_fixture()
    started = perf_counter()
    result = plan_spatial(source, OutboundRequest('target', 4), constraints, now=NOW)
    elapsed = perf_counter() - started
    report = result.optimization
    return dict(elapsed_seconds=elapsed, expanded_states=report.expanded_states,
                examined_candidates=report.examined_candidates,
                complete_plans_compared=report.complete_plans_compared,
                search_complete=report.search_complete, score=asdict(report.score),
                first_feasible_score=asdict(report.first_feasible_score))
