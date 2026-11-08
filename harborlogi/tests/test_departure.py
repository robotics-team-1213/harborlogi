"""Departure needs ROSY's release permission, not a Harbor S-clearance judgement (ADR 0029)."""
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
import sqlite3
import tempfile
import unittest

from harbor.departure import DepartureLedger, record_release
from test_policy import NOW


class DepartureLedgerTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / 'departure.sqlite3'
        self.ledger = DepartureLedger(self.path)

    def permit(self, event_id, vehicle='pinky-1', when=NOW):
        with closing(sqlite3.connect(self.path)) as c, c:
            record_release(c, dict(event_id=event_id, vehicle_id=vehicle, observed_at=when))

    def test_no_permission_no_departure(self):
        with self.assertRaises(ValueError):
            self.ledger.authorize('pinky-1', since=NOW)
        self.permit('e1', vehicle='pinky-2')
        with self.assertRaises(ValueError):
            self.ledger.authorize('pinky-1', since=NOW)

    def test_each_permission_is_spent_once_and_survives_reopening(self):
        self.permit('e1')
        self.permit('e2', when=NOW + timedelta(seconds=5))
        self.assertEqual(self.ledger.authorize('pinky-1', since=NOW), 'e2')
        self.assertEqual(DepartureLedger(self.path).authorize('pinky-1', since=NOW), 'e1')
        with self.assertRaises(ValueError):
            self.ledger.authorize('pinky-1', since=NOW)

    def test_arrival_time_must_be_aware(self):
        with self.assertRaises(ValueError):
            self.ledger.authorize('pinky-1', since=datetime(2026, 11, 8))


if __name__ == '__main__':
    unittest.main()
