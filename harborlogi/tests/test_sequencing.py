import unittest
from datetime import datetime, timedelta, timezone

from harbor.layout import YardCell, YardLayout
from harbor.sequencing import rank_retrieval_orders
from harbor.spatial import Area, CargoShape, Placement, SpatialPolicy, SpatialSnapshot

NOW = datetime(2026, 11, 7, tzinfo=timezone.utc)
LAYOUT = YardLayout(3, 3, 3, tuple(YardCell(f'Y{r}{c}', r, c) for r in range(1, 4) for c in range(1, 4)))


def snapshot(placements):
    """1×3 containers only: each one fills a yard row at its level."""
    return SpatialSnapshot(7, NOW, LAYOUT, (Area('yard', 3, 3, 3), Area('temp', 1, 3, 3), Area('stand', 1, 3, 1)),
                           tuple(CargoShape(code, 3, i + 1) for i, (code, _, _) in enumerate(placements)),
                           tuple(Placement(code, 'yard', row, 1, level) for code, row, level in placements))


def policy():
    return SpatialPolicy('stand', ('temp',), frozenset({('yard', 'yard'), ('yard', 'temp'), ('temp', 'yard'), ('yard', 'stand')}),
                         frozenset((arm, place) for arm in ('A', 'B') for place in ('temp', 'stand', *(c.bay_id for c in LAYOUT.cells))),
                         timedelta(minutes=5))


class SequencingTests(unittest.TestCase):
    def test_taking_the_top_container_first_avoids_a_rehandle(self):
        # B rests on A in row 1. Taking B first needs no rehandle; taking A first must move B away and back.
        yard = snapshot([('A', 1, 1), ('B', 1, 2), ('C', 2, 1)])
        ranked = rank_retrieval_orders(yard, policy(), ('A', 'B'), now=NOW, schedule={}, group=2)
        best, other = ranked[0], ranked[1]
        self.assertEqual((best.order, best.feasible, best.rehandles), (('B', 'A'), True, 0))
        self.assertEqual(best.total_moves, 2)
        self.assertEqual((other.order, other.rehandles), (('A', 'B'), 1))
        self.assertGreater(other.total_moves, best.total_moves)
        self.assertEqual([step.cargo_id for step in best.steps], ['B', 'A'])
        self.assertNotIn('A', {p.cargo_id for p in best.final.placements})

    def test_ties_keep_the_given_order(self):
        yard = snapshot([('A', 1, 1), ('B', 2, 1)])
        ranked = rank_retrieval_orders(yard, policy(), ('B', 'A'), now=NOW, schedule={}, group=1)
        self.assertEqual([s.order for s in ranked], [('B', 'A'), ('A', 'B')])
        self.assertEqual({s.total_moves for s in ranked}, {2})

    def test_a_blocked_order_is_ranked_last_and_says_why(self):
        yard = snapshot([('A', 1, 1)])
        ranked = rank_retrieval_orders(yard, policy(), ('A', 'Z'), now=NOW + timedelta(hours=1), schedule={}, group=3)
        self.assertTrue(all(not s.feasible for s in ranked))
        self.assertEqual(ranked[0].steps[-1].status, 'HOLD')
        self.assertTrue(ranked[0].steps[-1].reason)

    def test_large_groups_use_a_bounded_greedy_search(self):
        yard = snapshot([('A', 1, 1), ('B', 1, 2), ('C', 1, 3), ('D', 2, 1), ('E', 3, 1)])
        ranked = rank_retrieval_orders(yard, policy(), ('A', 'B', 'C', 'D', 'E'), now=NOW, schedule={}, group=3, exact_limit=3)
        self.assertLessEqual(len(ranked), 2)
        self.assertEqual(ranked[0].order[:3], ('C', 'B', 'A'))
        self.assertFalse(ranked[0].exhaustive)
        exact = rank_retrieval_orders(yard, policy(), ('A', 'B', 'C'), now=NOW, schedule={}, group=3)
        self.assertTrue(exact[0].exhaustive)


if __name__ == '__main__':
    unittest.main()
