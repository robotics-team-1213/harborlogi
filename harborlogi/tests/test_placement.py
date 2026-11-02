import unittest
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from harbor.layout import YardCell, YardLayout
from harbor.model import CargoHolding
from harbor.placement import plan_placement
from harbor.spatial import Area, CargoShape, Placement, SpatialPolicy, SpatialSnapshot


NOW = datetime(2026, 10, 26, tzinfo=timezone.utc)


def scene(rows=1, columns=2, levels=2, placements=(), lengths=None):
    layout = YardLayout(rows, columns, levels, tuple(
        YardCell(f'Y{r}-{c}', r, c) for r in range(1, rows+1) for c in range(1, columns+1)))
    source = SpatialSnapshot(4, NOW, layout,
        (Area('yard', rows, columns, levels), Area('stand', 1, 1, 1)),
        tuple(CargoShape(p.cargo_id, (lengths or {}).get(p.cargo_id, 1), 1) for p in placements),
        tuple(placements))
    policy = SpatialPolicy('stand', (), frozenset(),
        frozenset(('A', c.bay_id) for c in layout.cells), timedelta(minutes=1))
    return source, policy


class PlacementTests(unittest.TestCase):
    def plan(self, source, policy, length=1, schedule=None):
        return plan_placement(source, CargoShape('incoming', length, 7), policy, schedule or {}, now=NOW)

    def test_schedule_change_selects_other_stack_and_preserves_source(self):
        source, policy = scene(placements=(Placement('left', 'yard', 1, 1, 1),
                                           Placement('right', 'yard', 1, 2, 1)))
        before = asdict(source)
        first = self.plan(source, policy, schedule={'left': (1, 1), 'right': (3, 1), 'incoming': (2, 1)})
        second = self.plan(source, policy, schedule={'left': (3, 1), 'right': (1, 1), 'incoming': (2, 1)})
        self.assertEqual((first.recommendation.column, second.recommendation.column), (2, 1))
        sequenced = self.plan(source, policy, schedule={'left': (1, 1), 'right': (1, 3), 'incoming': (1, 2)})
        reversed_sequence = self.plan(source, policy, schedule={'left': (1, 3), 'right': (1, 1), 'incoming': (1, 2)})
        self.assertEqual((sequenced.recommendation.column, reversed_sequence.recommendation.column), (2, 1))
        self.assertEqual(first.score, (0, 0, 2))
        self.assertEqual(first.footprint_revision, 7)
        self.assertEqual(first.moves, ())
        self.assertEqual(first.required_completion, ('inbound_placed_at_target', 'arms_retreated'))
        self.assertEqual((first.model, first.plan_kind), ('grid-placement-v1', 'inbound-placement'))
        self.assertEqual(asdict(source), before)

    def test_multi_cell_geometry_rotation_and_full_support(self):
        for length in (2, 3):
            with self.subTest(length=length):
                source, policy = scene(rows=length, columns=1, levels=2,
                    placements=tuple(Placement(f'base{r}', 'yard', r, 1, 1) for r in range(1, length+1)))
                result = self.plan(source, policy, length)
                self.assertEqual(result.status, 'READY')
                self.assertEqual((result.recommendation.orientation, result.recommendation.level), (90, 2))
                self.assertEqual(result.candidate_count, 1)
                unsupported = replace(source, shapes=source.shapes[:-1], placements=source.placements[:-1])
                self.assertEqual(self.plan(unsupported, policy, length).status, 'HOLD')
                floor, constraints = scene(rows=1, columns=length, levels=1)
                self.assertEqual(self.plan(floor, constraints, length).recommendation.orientation, 0)
                narrow, constraints = scene(rows=1, columns=length-1, levels=1)
                self.assertEqual(self.plan(narrow, constraints, length).status, 'HOLD')

    def test_whole_footprint_must_be_reached_by_one_verified_arm(self):
        source, policy = scene(rows=1, columns=2, levels=1)
        split = replace(policy, arm_reach=frozenset({('A', 'Y1-1'), ('B', 'Y1-2')}))
        self.assertEqual(self.plan(source, split, 2).status, 'HOLD')
        self.assertEqual(self.plan(source, policy, 2).status, 'READY')
        self.assertEqual(self.plan(source, replace(policy, arm_reach=frozenset()), 2).reason,
                         'ARM_REACH_UNVERIFIED')

    def test_only_storage_and_auxiliary_area_reach(self):
        source, policy = scene(rows=1, columns=1, levels=1,
                               placements=(Placement('base', 'yard', 1, 1, 1),))
        source = replace(source, areas=source.areas+(Area('store', 1, 2, 1), Area('temp', 1, 2, 1)))
        policy = replace(policy, temporary_areas=('temp',), storage_areas=('yard', 'store'),
                         arm_reach=policy.arm_reach | {('B', 'store'), ('A', 'temp'), ('A', 'stand')})
        result = self.plan(source, policy, 2)
        self.assertEqual(result.recommendation.area_id, 'store')
        self.assertEqual(self.plan(source, replace(policy, arm_reach=policy.arm_reach-{('B', 'store')}), 2).status, 'HOLD')

    def test_unknown_order_is_explicit_and_conservative(self):
        source, policy = scene(placements=(Placement('unknown', 'yard', 1, 1, 1),
                                           Placement('later', 'yard', 1, 2, 1)))
        result = self.plan(source, policy, schedule={'incoming': (1, 1), 'later': (2, 1)})
        self.assertEqual(result.recommendation.column, 2)
        self.assertIn('LOWER_OUTBOUND_ORDER_UNKNOWN:unknown', result.alternatives[0].rationale)
        unknown = self.plan(source, policy, schedule={'later': (2, 1)})
        self.assertEqual(unknown.recommendation.column, 1)
        self.assertIn('INCOMING_OUTBOUND_ORDER_UNKNOWN', unknown.rationale)
        # Same group without a full sequence is free, not unknown (design 32 Q4).
        any_group = self.plan(source, policy, schedule={'incoming': (1, None), 'later': (1, 1)})
        self.assertEqual(any_group.score[1], 0)

    def test_distinct_lower_cargo_is_counted_once_across_cells_and_levels(self):
        source, policy = scene(rows=1, columns=2, levels=3,
            placements=(Placement('base', 'yard', 1, 1, 1), Placement('top', 'yard', 1, 1, 2)),
            lengths={'base': 2, 'top': 2})
        result = self.plan(source, policy, 2, {'base': (1, 1), 'top': (1, 2), 'incoming': (2, 1)})
        self.assertEqual(result.score, (2, 0, 3))

    def test_crossing_bridge_counts_earlier_cargo_outside_incoming_column(self):
        source, policy = scene(rows=1, columns=3, levels=3,
            placements=(Placement('earlier', 'yard', 1, 1, 1),
                        Placement('middle', 'yard', 1, 2, 1),
                        Placement('right', 'yard', 1, 3, 1),
                        Placement('bridge', 'yard', 1, 1, 2)), lengths={'bridge': 3})
        # Only the far-right bridge cell can receive incoming cargo.
        policy = replace(policy, arm_reach=frozenset({('A', 'Y1-3')}))
        before = asdict(source)
        result = self.plan(source, policy, schedule={'earlier': (1, 1), 'middle': (3, 1),
            'right': (3, 2), 'bridge': (3, 3), 'incoming': (2, 1)})
        self.assertEqual((result.recommendation.column, result.recommendation.level), (3, 3))
        self.assertEqual(result.score, (1, 0, 3))
        self.assertIn('KNOWN_EARLIER_OUTBOUND_BLOCKED:earlier', result.rationale)
        unknown = self.plan(source, policy, schedule={'middle': (3, 1),
            'right': (3, 2), 'bridge': (3, 3), 'incoming': (2, 1)})
        self.assertEqual(unknown.score, (0, 1, 3))
        self.assertIn('LOWER_OUTBOUND_ORDER_UNKNOWN:earlier', unknown.rationale)
        self.assertEqual(asdict(source), before)

    def test_stale_future_holding_already_observed_and_full_are_atomic_holds(self):
        source, policy = scene()
        for changed in (replace(source, observed_at=NOW-timedelta(minutes=2)),
                        replace(source, observed_at=NOW+timedelta(seconds=1)),
                        replace(source, holdings=(CargoHolding('held', None),))):
            result = self.plan(changed, policy)
            self.assertEqual((result.status, result.recommendation, result.moves, result.alternatives),
                             ('HOLD', None, (), ()))
        observed, policy = scene(placements=(Placement('incoming', 'yard', 1, 1, 1),))
        self.assertEqual(self.plan(observed, policy).reason, 'INBOUND_ALREADY_OBSERVED')
        full, policy = scene(rows=1, columns=1, levels=1,
                             placements=(Placement('base', 'yard', 1, 1, 1),))
        self.assertEqual(self.plan(full, policy).reason, 'NO_LEGAL_PLACEMENT')

    def test_search_budget_is_atomic_and_alternatives_are_bounded_deterministic(self):
        source, policy = scene(rows=3, columns=3, levels=1)
        result = self.plan(source, policy)
        self.assertEqual(result.candidate_count, 9)
        self.assertEqual(len(result.alternatives), 5)
        self.assertEqual(result, self.plan(source, policy))
        self.assertEqual((result.recommendation.row, result.recommendation.column), (1, 1))
        with patch('harbor.placement.PLACEMENT_CANDIDATE_LIMIT', 2):
            limited = self.plan(source, policy)
        self.assertEqual((limited.status, limited.reason, limited.recommendation, limited.alternatives),
                         ('HOLD', 'PLACEMENT_SEARCH_LIMIT_REACHED', None, ()))

    def test_reservations_separate_incoming_destinations_without_observations(self):
        source, policy = scene(rows=1, columns=2, levels=1)
        before = asdict(source)
        first = self.plan(source, policy)
        reserved = frozenset({('yard', first.recommendation.row, first.recommendation.column, 1)})
        second = plan_placement(source, CargoShape('second', 1, 8), policy, {}, now=NOW,
                                reserved_cells=reserved)
        self.assertEqual((first.recommendation.column, second.recommendation.column), (1, 2))
        self.assertEqual(asdict(source), before)
        full = plan_placement(source, CargoShape('second', 1, 8), policy, {}, now=NOW,
            reserved_cells=reserved | {('yard', 1, 2, 1)})
        self.assertEqual((full.status, full.reason), ('HOLD', 'NO_LEGAL_PLACEMENT'))

    def test_reservations_never_supply_physical_support_and_cover_whole_footprint(self):
        source, policy = scene(rows=1, columns=2, levels=2)
        reserved = frozenset({('yard', 1, 1, 1), ('yard', 1, 2, 1)})
        result = plan_placement(source, CargoShape('incoming', 2, 7), policy, {}, now=NOW,
                                reserved_cells=reserved)
        self.assertEqual(result.status, 'HOLD')
        floor = replace(source, areas=(Area('yard', 1, 2, 1), source.areas[1]),
                        layout=replace(source.layout, levels=1))
        partial = plan_placement(floor, CargoShape('incoming', 2, 7), policy, {}, now=NOW,
            reserved_cells=frozenset({('yard', 1, 2, 1)}))
        self.assertEqual(partial.status, 'HOLD')

    def test_reservation_bounds_and_immutable_collection_are_validated(self):
        source, policy = scene()
        invalid = [set(), [], frozenset({('unknown', 1, 1, 1)}),
                   frozenset({('yard', True, 1, 1)}), frozenset({('yard', 0, 1, 1)}),
                   frozenset({('yard', 1, 3, 1)}), frozenset({('yard', 1, 1, 3)}),
                   frozenset({('yard', 1, 1)}), frozenset({'yard'})]
        for reserved in invalid:
            with self.subTest(reserved=reserved), self.assertRaises(ValueError):
                plan_placement(source, CargoShape('incoming', 1, 7), policy, {}, now=NOW,
                                reserved_cells=reserved)

    def test_input_boundaries_and_cargo_capacity(self):
        source, policy = scene(rows=12, columns=12, levels=2)
        placements = tuple(Placement(f'c{i}', 'yard', i//12+1, i%12+1, 1) for i in range(144))
        placements += tuple(Placement(f'u{i}', 'yard', i//12+1, i%12+1, 2) for i in range(112))
        full = replace(source, placements=placements, shapes=tuple(CargoShape(p.cargo_id, 1, 1) for p in placements))
        self.assertEqual(self.plan(full, policy).reason, 'OBSERVATION_CARGO_LIMIT_REACHED')
        for length in (0, 4, True):
            with self.assertRaises(ValueError):
                CargoShape('incoming', length, 1)
        for schedule in ({'base': (True, 1)}, {'base': (1, -1)}, {'base': (1,)}, {'base': [1, 1]}):
            with self.assertRaises(ValueError):
                self.plan(source, policy, schedule=schedule)


if __name__ == '__main__':
    unittest.main()
