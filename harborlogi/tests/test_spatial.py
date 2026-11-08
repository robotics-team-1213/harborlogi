import unittest
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from harbor.layout import YardCell, YardLayout
from harbor.model import CargoHolding, OutboundRequest
from harbor.spatial import Area, CargoShape, Placement, SpatialPolicy, SpatialSnapshot, plan_spatial


NOW = datetime(2026, 10, 25, tzinfo=timezone.utc)
LAYOUT = YardLayout(3, 3, 3, tuple(YardCell(f'Y{r}{c}', r, c)
                               for r in range(1, 4) for c in range(1, 4)))


def snapshot(shapes, placements, temp=Area('temp', 3, 3, 3), stand=Area('stand', 3, 3, 1)):
    return SpatialSnapshot(4, NOW, LAYOUT, (Area('yard', 3, 3, 3), temp, stand),
                           tuple(CargoShape(c, n, i + 1) for i, (c, n) in enumerate(shapes)), tuple(placements))


def policy():
    return SpatialPolicy('stand', ('temp',), frozenset({('yard', 'temp'), ('temp', 'yard'), ('yard', 'stand')}),
                         frozenset((a, p) for a in ('A', 'B') for p in
                                   ('temp', 'stand', *(c.bay_id for c in LAYOUT.cells))), timedelta(minutes=1))


class SpatialTests(unittest.TestCase):
    def test_rotation_only_on_installed_area_pairs(self):
        # A two-cell cargo lying at 90 degrees does not fit the 1x3 stand unless it may turn there.
        source = snapshot([('target', 2)], [Placement('target', 'yard', 1, 1, 1, 90)], stand=Area('stand', 1, 3, 1))
        request = OutboundRequest('target', 4)
        self.assertEqual(plan_spatial(source, request, policy(), now=NOW).status, 'HOLD')
        ready = plan_spatial(source, request, policy(), now=NOW, rotation_moves=frozenset({('yard', 'stand')}))
        self.assertEqual(ready.status, 'READY')
        final = ready.moves[-1].destination_placement
        self.assertEqual((final.area_id, final.orientation), ('stand', 0))

    def test_variable_rectangular_layout_and_large_coordinates(self):
        layout = YardLayout(12, 11, 6, tuple(YardCell(f'Y{r}-{c}', r, c)
            for r in range(1, 13) for c in range(1, 12)))
        source = SpatialSnapshot(4, NOW, layout, (Area('yard', 12, 11, 6),),
            (CargoShape('target', 1, 1),), (Placement('target', 'yard', 12, 11, 1),))
        self.assertEqual(source.position_id(source.placements[0]), 'Y12-11-L1')
        with self.assertRaises(ValueError):
            replace(source, areas=(Area('yard', 11, 11, 6),))

    def test_extra_storage_target_and_original_storage_restoration(self):
        source = snapshot([('target', 1), ('block', 1)], [
            Placement('target', 'yard', 1, 1, 1), Placement('block', 'yard', 1, 1, 2)])
        source = replace(source, areas=source.areas+(Area('store', 1, 2, 2),),
            placements=tuple(replace(p, area_id='store') for p in source.placements))
        constraints = replace(policy(), storage_areas=('yard', 'store'),
            allowed_moves=policy().allowed_moves | {('store', 'temp'), ('store', 'stand'), ('temp', 'store')},
            arm_reach=policy().arm_reach | {('A', 'store')})
        result = self.plan(source, constraints)
        self.assertEqual(result.status, 'READY')
        self.assertEqual(result.moves[-1].destination_placement.area_id, 'store')
        self.assertEqual(self.plan(source, replace(constraints, arm_reach=policy().arm_reach)).status, 'HOLD')

    def test_candidate_budget_returns_no_partial_moves(self):
        source = snapshot([('target', 1)], [Placement('target', 'yard', 1, 1, 1)])
        with patch('harbor.spatial.SPATIAL_CANDIDATE_LIMIT', 0):
            result = self.plan(source)
        self.assertEqual((result.status, result.reason, result.moves),
                         ('HOLD', 'SPATIAL_SEARCH_LIMIT_REACHED', ()))

    def test_workload_limits_reject_large_area_sets_cells_and_cargo(self):
        source = snapshot([('target', 1)], [Placement('target', 'yard', 1, 1, 1)])
        for areas in (source.areas + tuple(Area(f'a{i}', 1, 1, 1) for i in range(14)),
                      source.areas + tuple(Area(f'a{i}', 12, 12, 6) for i in range(5)),
                      source.areas + (Area('Y11', 1, 1, 1),)):
            with self.assertRaises(ValueError):
                replace(source, areas=areas)
        # Unknown holders are valid reconciliations individually, but still count as cargo.
        with self.assertRaises(ValueError):
            replace(source, holdings=tuple(CargoHolding(f'held{i}', None) for i in range(256)))

    def plan(self, source, constraints=None):
        return plan_spatial(source, OutboundRequest('target', 4), constraints or policy(), now=NOW)

    def project(self, source, proposal):
        current = source
        for move in proposal.moves:
            self.assertEqual(current.placement(move.cargo_id), move.source_placement)
            self.assertEqual(move.source_placement.orientation, move.destination_placement.orientation)
            current = current.with_placements(tuple(move.destination_placement if p.cargo_id == move.cargo_id else p
                                                    for p in current.placements))
            self.assertEqual(asdict(move)['footprint_revision'], source.shape(move.cargo_id).footprint_revision)
        return current

    def test_crossing_long_blocker_moves_once_and_restores_on_floor(self):
        source = snapshot([('target', 3), ('support', 1), ('crossing', 2)], [
            Placement('target', 'yard', 1, 1, 1), Placement('support', 'yard', 2, 2, 1),
            Placement('crossing', 'yard', 1, 2, 2, 90)])
        proposal = self.plan(source)
        self.assertEqual(proposal.status, 'READY')
        self.assertEqual([m.cargo_id for m in proposal.moves], ['crossing', 'target', 'crossing'])
        final = self.project(source, proposal)
        self.assertEqual(final.placement('crossing').level, 1)
        self.assertEqual(source.placement('crossing').level, 2)
        self.assertEqual(proposal.moves[0].alternate_arms, ('B',))

    def test_all_overlapping_dependencies_and_top_dependencies_are_cleared(self):
        source = snapshot([('target', 3), ('left', 1), ('right', 1), ('top', 1)], [
            Placement('target', 'yard', 1, 1, 1), Placement('left', 'yard', 1, 1, 2),
            Placement('right', 'yard', 1, 3, 2), Placement('top', 'yard', 1, 1, 3)])
        result = self.plan(source)
        self.assertEqual(result.status, 'READY')
        self.assertEqual(len(result.moves), 7)
        order = [m.cargo_id for m in result.moves[:3]]
        self.assertLess(order.index('top'), order.index('left'))
        self.project(source, result)

    def test_fit_and_fixed_orientation(self):
        for temp, stand in ((Area('temp', 1, 1, 1), Area('stand', 3, 3, 1)),
                            (Area('temp', 3, 3, 1), Area('stand', 1, 1, 1))):
            source = snapshot([('target', 2), ('block', 2)], [
                Placement('target', 'yard', 1, 1, 1), Placement('block', 'yard', 1, 1, 2)], temp, stand)
            result = self.plan(source)
            self.assertEqual(result.status, 'HOLD')
            self.assertEqual(result.moves, ())
        source = snapshot([('target', 3)], [Placement('target', 'yard', 1, 1, 1, 90)],
                          stand=Area('stand', 1, 3, 1))
        self.assertEqual(self.plan(source).status, 'HOLD')

    def test_whole_footprint_reach_and_alternate(self):
        source = snapshot([('target', 3)], [Placement('target', 'yard', 1, 1, 1)])
        constraints = replace(policy(), arm_reach=policy().arm_reach - {('A', 'Y12')})
        result = self.plan(source, constraints)
        self.assertEqual(result.moves[0].primary_arm, 'B')
        constraints = replace(constraints, arm_reach=constraints.arm_reach - {('B', 'Y13')})
        self.assertEqual(self.plan(source, constraints).status, 'HOLD')

    def test_no_valid_restoration_returns_no_partial_plan(self):
        # The remaining middle-row support splits the free floor into strips too short for the rotated cargo.
        source = snapshot([('target', 3), ('support', 3), ('crossing', 2)], [
            Placement('target', 'yard', 1, 1, 1), Placement('support', 'yard', 2, 1, 1),
            Placement('crossing', 'yard', 1, 2, 2, 90)])
        result = self.plan(source)
        self.assertEqual((result.status, result.reason, result.moves), ('HOLD', 'NO_LEGAL_SPATIAL_PLAN', ()))

    def test_one_temp_cell_cannot_be_replaced_by_empty_stand_or_conveyor(self):
        source = snapshot([('target', 2), ('left', 1), ('right', 1)], [
            Placement('target', 'yard', 1, 1, 1), Placement('left', 'yard', 1, 1, 2),
            Placement('right', 'yard', 1, 2, 2)], temp=Area('temp', 1, 1, 1))
        result = self.plan(source)
        self.assertEqual(result.status, 'HOLD')
        self.assertEqual(result.moves, ())
        for temporary in (('stand',), ('conveyor',), ('yard',)):
            with self.assertRaises(ValueError):
                replace(policy(), temporary_areas=temporary)

    def test_existing_auxiliary_occupancy_and_mixed_orientations_are_preserved(self):
        source = snapshot([('target', 2), ('block', 2), ('parked', 1)], [
            Placement('target', 'yard', 1, 1, 1, 90), Placement('block', 'yard', 1, 1, 2, 90),
            Placement('parked', 'temp', 1, 1, 1)])
        result = self.plan(source)
        self.assertEqual(result.status, 'READY')
        final = self.project(source, result)
        self.assertEqual(final.placement('parked'), source.placement('parked'))
        self.assertEqual(final.placement('block').orientation, 90)
        self.assertEqual(result, self.plan(source))

    def test_snapshot_support_requires_every_cell_not_only_anchor(self):
        with self.assertRaises(ValueError):
            snapshot([('target', 2), ('support', 1)], [Placement('target', 'yard', 1, 1, 2),
                                                     Placement('support', 'yard', 1, 1, 1)])
        with self.assertRaises(ValueError):
            snapshot([('target', 3)], [Placement('target', 'yard', 2, 1, 1, 90)])

    def test_hold_gates_and_search_limit(self):
        source = snapshot([('target', 1)], [Placement('target', 'yard', 1, 1, 1)])
        held = replace(source, holdings=(CargoHolding('unknown-size', 'A'),))
        self.assertEqual(self.plan(held).reason, 'PHYSICAL_HOLDING_REQUIRES_RECONCILIATION')
        for changed, reason in ((replace(source, revision=5), 'REVISION_CONFLICT'),
                                (replace(source, observed_at=NOW-timedelta(days=1)), 'OBSERVATION_NOT_CURRENT'),
                                (replace(source, observed_at=NOW+timedelta(seconds=1)), 'OBSERVATION_NOT_CURRENT'),
                                (replace(source, holdings=(CargoHolding('held', 'A'),),
                                         shapes=source.shapes+(CargoShape('held', 1, 2),)),
                                 'PHYSICAL_HOLDING_REQUIRES_RECONCILIATION')):
            self.assertEqual(self.plan(changed).reason, reason)
        with patch('harbor.spatial.SPATIAL_SEARCH_LIMIT', 0):
            self.assertEqual(self.plan(source).reason, 'SPATIAL_SEARCH_LIMIT_REACHED')
        self.assertEqual(self.plan(source, replace(policy(), allowed_moves=frozenset())).status, 'HOLD')
        with self.assertRaises(ValueError):
            self.plan(source, replace(policy(), temporary_areas=('unknown',)))

    def test_snapshot_rejects_bounds_overlap_support_duplicates_and_types(self):
        source = snapshot([('target', 2)], [Placement('target', 'yard', 1, 1, 1)])
        for invalid in (Placement('target', 'yard', 1, 3, 1), Placement('target', 'yard', 1, 1, 2),
                        Placement('target', 'missing', 1, 1, 1)):
            with self.assertRaises(ValueError):
                source.with_placements((invalid,))
        for changes in (dict(shapes=source.shapes*2), dict(placements=source.placements*2),
                        dict(placements=list(source.placements)), dict(areas=(Area('yard', 2, 3, 3),)),
                        dict(holdings=(CargoHolding('target', 'A'),))):
            with self.assertRaises(ValueError):
                replace(source, **changes)
        for constructor, args in ((CargoShape, ('x', True, 1)), (CargoShape, ('x', 1, 0)),
                                  (Placement, ('x', 'yard', True, 1, 1)),
                                  (Placement, ('x', 'yard', 1, 1, 1, 180)), (Area, ('x', 13, 1, 1))):
            with self.assertRaises(ValueError):
                constructor(*args)
        overlap = source.placements + (Placement('other', 'yard', 1, 2, 1),)
        with self.assertRaises(ValueError):
            replace(source, shapes=source.shapes+(CargoShape('other', 1, 2),), placements=overlap)
        self.assertEqual(len(source.cells(source.placements[0])), 2)
        self.assertEqual(source.position_id(source.placements[0]), 'Y11-L1')
        self.assertEqual(source.position_id(Placement('target', 'temp', 1, 1, 1)), 'temp-R1C1-L1')


if __name__ == '__main__':
    unittest.main()
