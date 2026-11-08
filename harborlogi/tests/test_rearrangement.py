import unittest
from dataclasses import replace
from datetime import timedelta

from harbor.layout import YardCell, YardLayout
from harbor.model import CargoHolding
from harbor.spatial import (Area, CargoShape, Placement, SpatialPolicy, SpatialSnapshot, order_relation,
                            plan_rearrangement)
from test_spatial import LAYOUT, NOW


TEMP_MOVES = frozenset({('yard', 'temp'), ('temp', 'yard'), ('yard', 'stand')})
DIRECT_MOVES = TEMP_MOVES | {('yard', 'yard')}


def snapshot(shapes, placements, layout=LAYOUT):
    return SpatialSnapshot(4, NOW, layout, (Area('yard', layout.rows, layout.columns, layout.levels),
                                            Area('temp', 1, 3, 3), Area('stand', 1, 3, 1)),
                           tuple(CargoShape(c, n, i + 1) for i, (c, n) in enumerate(shapes)), tuple(placements))


def policy(moves=DIRECT_MOVES, layout=LAYOUT):
    bays = tuple(c.bay_id for c in layout.cells)
    reach = {('A', p) for p in ('temp', *bays)} | {('B', p) for p in ('stand', *bays)}
    return SpatialPolicy('stand', ('temp',), frozenset(moves), frozenset(reach), timedelta(minutes=1))


def plan(source, schedule=None, rules=None, **kwargs):
    return plan_rearrangement(source, rules or policy(), schedule or {}, now=NOW,
                              expected_revision=source.revision, **kwargs)


def replay(test, source, moves):
    current = source
    for move in moves:
        test.assertEqual(current.placement(move.cargo_id), move.source_placement)
        current = current.with_placements(tuple(move.destination_placement if p.cargo_id == move.cargo_id else p
                                                for p in current.placements))
    return current


# 'early' leaves first but sits under 'late': one blocked pair.
INVERTED = snapshot([('early', 1), ('late', 1)], [
    Placement('early', 'yard', 1, 1, 1), Placement('late', 'yard', 1, 1, 2)])
SCHEDULE = {'early': (0, None), 'late': (1, None)}
ROW = YardLayout(1, 4, 1, tuple(YardCell(f'Y1{c}', 1, c) for c in range(1, 5)))


class RearrangementTests(unittest.TestCase):
    def test_order_relation_matches_the_design_table(self):
        cases = [((0, None), (1, None), 'blocked'), ((0, 1), (0, 2), 'blocked'), ((0, None), (0, 2), None),
                 ((0, 1), None, 'blocked'), (None, (0, 1), 'unknown'), (None, None, 'unknown'),
                 ((1, None), (0, None), None)]
        for below, above, expected in cases:
            self.assertEqual(order_relation(below, above), expected, (below, above))

    def test_ordered_yard_needs_no_moves(self):
        result = plan(INVERTED, {'early': (1, None), 'late': (0, None)})
        self.assertEqual((result.status, result.reason, result.moves), ('HOLD', 'REARRANGE_NOT_WORTH', ()))

    def test_inverted_order_is_fixed_with_one_direct_move_that_replays(self):
        result = plan(INVERTED, SCHEDULE)
        self.assertEqual(result.status, 'READY')
        self.assertEqual(result.required_completion, ('rearranged_at_targets', 'arms_retreated'))
        score = result.optimization.score
        self.assertEqual((score.blocked_pairs_before, score.blocked_pairs, score.gain), (1, 0, 1))
        # Free positions (F) outrank the move count, so k=2 may spend a second move on them.
        self.assertLessEqual(len(result.moves), 2)
        self.assertEqual(len(plan(INVERTED, SCHEDULE, k=1).moves), 1)
        self.assertEqual({m.phase_label for m in result.moves}, {'재배치'})
        final = replay(self, INVERTED, result.moves)
        self.assertNotIn(('yard', 1, 1, 2), final.occupied())  # nothing left resting where 'late' blocked

    def test_without_yard_to_yard_every_move_goes_through_temp(self):
        result = plan(INVERTED, SCHEDULE, policy(TEMP_MOVES))
        self.assertEqual(result.status, 'READY')
        self.assertEqual([(m.source_placement.area_id, m.destination_placement.area_id) for m in result.moves],
                         [('yard', 'temp'), ('temp', 'yard')])
        self.assertTrue(all(m.primary_arm == 'A' and not m.alternate_arms for m in result.moves))
        replay(self, INVERTED, result.moves)
        self.assertEqual(plan(INVERTED, SCHEDULE, policy(TEMP_MOVES), k=1).reason, 'REARRANGE_NOT_WORTH')
        self.assertEqual(plan(INVERTED, SCHEDULE, k=1).status, 'READY')

    def test_k_limits_moves_per_gain(self):
        self.assertEqual(plan(INVERTED, SCHEDULE, max_moves=1, rules=policy(TEMP_MOVES)).status, 'HOLD')

    def test_preconditions_hold_without_moves(self):
        self.assertEqual(plan(INVERTED, SCHEDULE, outbound_pending=True).reason, 'OUTBOUND_PENDING')
        stale = plan_rearrangement(INVERTED, policy(), SCHEDULE, now=NOW + timedelta(minutes=2),
                                   expected_revision=4)
        self.assertEqual(stale.reason, 'OBSERVATION_NOT_CURRENT')
        conflict = plan_rearrangement(INVERTED, policy(), SCHEDULE, now=NOW, expected_revision=3)
        self.assertEqual(conflict.reason, 'REVISION_CONFLICT')
        held = replace(INVERTED, placements=INVERTED.placements[:1], holdings=(CargoHolding('late', 'A'),))
        self.assertEqual(plan(held, SCHEDULE).reason, 'PHYSICAL_HOLDING_REQUIRES_RECONCILIATION')
        parked = snapshot([('early', 1)], [Placement('early', 'temp', 1, 1, 1)])
        self.assertEqual(plan(parked).reason, 'TEMPORARY_AREA_OCCUPIED')

    def test_rotated_long_cargo_needs_installed_rotation(self):
        # The 1x3 stand cannot take a two-cell cargo lying at 90 degrees.
        source = snapshot([('long', 2)], [Placement('long', 'yard', 1, 1, 1, 90)])
        self.assertEqual(plan(source).reason, 'ROTATION_NOT_INSTALLED')
        result = plan(source, rotation_moves=frozenset({('yard', 'yard')}))
        self.assertEqual(result.status, 'READY')
        self.assertEqual(result.optimization.score.gain, 3)
        self.assertEqual(result.moves[0].destination_placement.orientation, 0)
        replay(self, source, result.moves)

    def test_free_positions_alone_need_a_user_request(self):
        rules = policy(layout=ROW)
        source = snapshot([('box', 1)], [Placement('box', 'yard', 1, 2, 1)], ROW)
        self.assertEqual(plan(source, rules=rules).reason, 'REARRANGE_NOT_WORTH')
        requested = plan(source, rules=rules, requested=True)
        self.assertEqual(requested.status, 'READY')
        self.assertEqual(requested.optimization.score.gain, 0)

    def test_waiting_inbound_without_a_place_counts_as_gain(self):
        rules = policy(layout=ROW)
        source = snapshot([('box', 1)], [Placement('box', 'yard', 1, 2, 1)], ROW)
        result = plan(source, rules=rules, pending_inbound=(3,))
        self.assertEqual(result.status, 'READY')
        self.assertEqual(result.optimization.score.inbound_without_place, 0)
        self.assertEqual(result.optimization.score.gain, 3)

    def test_reserved_cells_are_never_targets(self):
        reserved = frozenset(('yard', r, c, 1) for r in range(1, 4) for c in range(1, 4) if (r, c) != (3, 3))
        result = plan(INVERTED, SCHEDULE, reserved=reserved)
        self.assertEqual(result.status, 'READY')
        target = result.moves[-1].destination_placement
        self.assertEqual((target.area_id, target.row, target.column, target.level), ('yard', 3, 3, 1))


if __name__ == '__main__':
    unittest.main()
