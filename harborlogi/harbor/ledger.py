"""Business ledger read model; reads never initialize, recover, or mutate storage."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3


def plan_payload(row):
    payload = json.loads(row["payload"])
    if "target_id" in payload or "supply_slot" in payload:
        raise ValueError("Ledger row predates the ADR 0005 names; reconcile it and prepare a new request key")
    return payload

def business_row(c, key):
    row = c.execute("SELECT * FROM business WHERE request_key=?", (key,)).fetchone()
    if row is None:
        raise ValueError("Unknown business request")
    return dict(row)


class ReadOnlyLedger:
    def __init__(self, path):
        self.path = Path(path).resolve()

    def _read(self, key=None):
        with closing(sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True, timeout=3)) as c:
            c.row_factory = sqlite3.Row
            c.execute('PRAGMA query_only=ON')
            if key is None:
                rows = c.execute('SELECT * FROM business ORDER BY request_key LIMIT 201').fetchall()
                if len(rows) > 200:
                    raise ValueError('This local workbench supports up to 200 tasks')
                return [dict(row) for row in rows]
            try:
                return business_row(c, key)
            except ValueError as error:
                # Notification links carry only the opaque outbox id (ROSY D-997); resolve it here.
                try:
                    row = c.execute('SELECT request_key FROM hold_outbox WHERE notification_id=?',
                                    (key,)).fetchone()
                except sqlite3.OperationalError:  # A ledger predating the outbox has no such table.
                    row = None
                if row is None:
                    raise KeyError(key) from error
                return business_row(c, row[0])

    def tasks(self):
        return self._read()

    def read(self, key):
        return self._read(key)

    def consumed(self, evidence_ids):
        with closing(sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True)) as connection:
            connection.execute('PRAGMA query_only=ON')
            row = connection.execute('SELECT COUNT(*) FROM evidence WHERE evidence_id IN (?, ?)',
                                     evidence_ids).fetchone()
        return row[0] > 0
