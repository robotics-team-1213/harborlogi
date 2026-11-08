"""Transfer-vehicle departure permission from ROSY's `vehicle.release_permitted` (ADR 0029).

Supply arm S is a ROSY Fleet job; whether its gripper left the vehicle's load surface is ROSY's
verdict, delivered as a port event. Harbor no longer judges S clearance. It keeps each permission
once and spends it on one departure of that vehicle, only when the permission was observed at or
after the vehicle's current arrival. Driving and stopping authority stay with Fleet and CORE.
"""
from contextlib import closing
from datetime import datetime
import sqlite3

from harbor.model import aware_time, identifier

SCHEMA = ("CREATE TABLE IF NOT EXISTS release_permits (event_id TEXT PRIMARY KEY, vehicle_id TEXT NOT NULL, "
          "observed_at TEXT NOT NULL, state TEXT NOT NULL)")


def record_release(connection, event):
    """Store one parsed release event inside the caller's transaction; a replay changes nothing."""
    connection.execute("INSERT OR IGNORE INTO release_permits VALUES(?,?,?,'PERMITTED')",
                       (event["event_id"], event["vehicle_id"], event["observed_at"].isoformat()))


class DepartureLedger:
    """Spends ROSY release permissions, one per departure."""

    def __init__(self, path):
        self.path = str(path)
        with closing(self._connect()) as c, c:
            c.execute(SCHEMA)

    def _connect(self):
        c = sqlite3.connect(self.path, timeout=10)
        c.row_factory = sqlite3.Row
        return c

    def authorize(self, vehicle_id, *, since):
        """Permit one departure against the newest unspent permission observed at or after `since`."""
        identifier(vehicle_id)
        aware_time(since)
        with closing(self._connect()) as c, c:
            c.execute("BEGIN IMMEDIATE")
            rows = c.execute("SELECT event_id, observed_at FROM release_permits WHERE vehicle_id=? AND state='PERMITTED'",
                             (vehicle_id,)).fetchall()
            # ISO strings are not safely orderable across offsets, so compare parsed times.
            current = [r for r in rows if datetime.fromisoformat(r["observed_at"]) >= since]
            if not current:
                raise ValueError("ROSY has not permitted this vehicle to leave since it arrived")
            newest = max(current, key=lambda r: datetime.fromisoformat(r["observed_at"]))
            c.execute("UPDATE release_permits SET state='AUTHORIZED' WHERE event_id=?", (newest["event_id"],))
            return newest["event_id"]
