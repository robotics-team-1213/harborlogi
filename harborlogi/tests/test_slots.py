import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from harbor.layout import YardLayout
from harbor.model import OutboundRequest
from harbor.placement import plan_placement
from harbor.spatial import Area, CargoShape, Placement, SpatialPolicy, SpatialSnapshot, plan_spatial

NOW = datetime(2026, 11, 7, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parents[1]
LAYOUT = YardLayout.from_dict(json.loads((ROOT / 'config/yard-layout-3x6.json').read_text(encoding='utf-8')))
AREAS = (Area('yard', 3, 6, 3), Area('temp_l', 3, 3, 1), Area('temp_r', 3, 3, 1), Area('stand', 1, 3, 1))


def policy(slot_cells=3):
    cells = [c.bay_id for c in LAYOUT.cells]
    reach = {('A', b) for b in cells} | {('B', b) for b in cells} | {('A', 'temp_l'), ('B', 'temp_r'), ('A', 'stand'), ('B', 'stand')}
    moves = {('yard', 'yard'), ('yard', 'temp_l'), ('temp_l', 'yard'), ('yard', 'temp_r'), ('temp_r', 'yard'), ('yard', 'stand')}
    return SpatialPolicy('stand', ('temp_l', 'temp_r'), frozenset(moves), frozenset(reach), timedelta(minutes=5), ('yard',), slot_cells)


def snapshot(placements):
    return SpatialSnapshot(3, NOW, LAYOUT, AREAS, tuple(CargoShape(code, 3, i + 1) for i, (code, *_) in enumerate(placements)),
                           tuple(Placement(code, 'yard', row, column, level) for code, row, column, level in placements))


class SlotTests(unittest.TestCase):
    def test_every_planned_move_starts_at_a_slot_boundary(self):
        yard = snapshot([('A', 1, 1, 1), ('B', 1, 1, 2), ('C', 1, 1, 3), ('D', 2, 4, 1)])
        proposal = plan_spatial(yard, OutboundRequest('A', 3), policy(), now=NOW)
        self.assertEqual(proposal.status, 'READY')
        for move in proposal.moves:
            self.assertIn(move.destination_placement.column, (1, 4))
        self.assertIn(proposal.moves[-1].destination_placement.area_id, ('yard', 'stand'))

    def test_inbound_recommendations_use_slots_only(self):
        yard = snapshot([('A', 1, 1, 1)])
        proposal = plan_placement(yard, CargoShape('N', 3, 9), policy(), {}, now=NOW)
        self.assertEqual(proposal.status, 'READY')
        placements = [proposal.recommendation, *(alternative.placement for alternative in proposal.alternatives)]
        self.assertTrue(all(p.column in (1, 4) and p.orientation == 0 for p in placements))

    def test_without_slots_any_cell_may_anchor(self):
        self.assertTrue(policy(None).aligned(1, 2, 0))
        self.assertFalse(policy().aligned(1, 2, 0))
        self.assertTrue(policy().aligned(1, 4, 0))
        with self.assertRaises(ValueError):
            policy(0)


if __name__ == '__main__':
    unittest.main()
