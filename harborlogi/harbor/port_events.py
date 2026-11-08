"""ROSY port events become per-cargo intake states (ADR 0029); nothing here moves a device.

Harbor reads R's `GET /api/fleet/port-events` with an opaque cursor it stores per Fleet connection
and never parses. Each `event_id` is consumed once, and a cargo only moves forward:
EXPECTED (declared on a trip) -> TRIP_ARRIVED -> RECEIVED_AT_INTAKE -> STORING -> COMPLETED.
HOLD reasons are MISMATCH, UNKNOWN, LOST and DAMAGED. Identity is only what ROSY read (`observed`) or a
named person confirmed (`attested`); an UNREADABLE cargo waits for that person. The LOST timeout only
runs once R has answered on the connection, so a missing endpoint never ages cargo into LOST. Storing runs through the
existing business ledger and a ROSY ARM proposal that a named operator approves.
"""
from contextlib import closing
from datetime import datetime, timedelta
import json
import re
import sqlite3

from harbor.departure import SCHEMA as RELEASE_SCHEMA, record_release
from harbor.model import aware_time, identifier

FIELDS = ("event_id", "kind", "observation_ref", "object_ref", "intake_point", "slot_ref", "vehicle_id",
          "reason", "identity", "observed_at")
# kind -> (required, optional) payload fields; every other payload field must be null.
KINDS = {
    "container.received_at_intake": ({"object_ref", "intake_point", "identity"}, set()),
    "container.stored": ({"object_ref", "slot_ref", "identity"}, set()),
    "container.handling_failed": ({"reason"}, {"object_ref"}),
    "vehicle.release_permitted": ({"vehicle_id"}, set()),
}
REASONS = {"MISMATCH", "UNREADABLE", "LOST", "DAMAGED"}
IDENTITIES = {"observed", "attested"}
RANK = {"EXPECTED": 0, "TRIP_ARRIVED": 1, "RECEIVED_AT_INTAKE": 2, "STORING": 3, "COMPLETED": 4}
WAITING_ON_ROSY = ("TRIP_ARRIVED", "STORING")  # RECEIVED_AT_INTAKE waits on Harbor's own storing request.


def _ref(value, limit=128):
    identifier(value)
    if len(value) > limit or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in value):
        raise ValueError("Invalid port event reference")


def parse_event(value):
    """Validate one closed PortEvent; unknown fields, kinds or misplaced fields are refused."""
    if not isinstance(value, dict) or not set(value) <= set(FIELDS):
        raise ValueError("Port event has unknown fields")
    event = {name: value.get(name) for name in FIELDS}
    if event["kind"] not in KINDS:
        raise ValueError("Unknown port event kind")
    if not isinstance(event["event_id"], str) or not re.fullmatch(r"[0-9a-f]{64}", event["event_id"]):
        raise ValueError("Port event id must be 64 lowercase hex")
    _ref(event["observation_ref"])
    required, optional = KINDS[event["kind"]]
    for name in ("object_ref", "intake_point", "slot_ref", "vehicle_id", "reason", "identity"):
        if event[name] is None:
            if name in required:
                raise ValueError(f"Port event {event['kind']} requires {name}")
        elif name not in required | optional:
            raise ValueError(f"Port event {event['kind']} must not carry {name}")
        else:
            _ref(event[name])
    if event["reason"] is not None and event["reason"] not in REASONS:
        raise ValueError("Unknown handling failure reason")
    if event["identity"] is not None and event["identity"] not in IDENTITIES:
        raise ValueError("Identity is observed or attested")
    if event["slot_ref"] is not None and not re.fullmatch(r".+-r\d+-c\d+-l\d+", event["slot_ref"]):
        raise ValueError("Slot reference must be a Harbor anchor address")
    text = event["observed_at"]
    try:
        when = datetime.fromisoformat(text.replace("Z", "+00:00")) if isinstance(text, str) else None
    except ValueError:
        when = None
    if when is None or when.utcoffset() != timedelta(0):
        raise ValueError("observed_at must be an RFC3339 UTC time")
    event["observed_at"] = when
    return event


class IntakeLedger:
    def __init__(self, path, *, lost_after=timedelta(minutes=30)):
        if not isinstance(lost_after, timedelta) or lost_after <= timedelta(0):
            raise ValueError("A positive LOST timeout is required")
        self.path, self.lost_after = str(path), lost_after
        with closing(self._connect()) as c, c:
            c.executescript(f"""
                CREATE TABLE IF NOT EXISTS port_cursor (connection TEXT PRIMARY KEY, cursor TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS port_events (
                    event_id TEXT PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL, outcome TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS intake (
                    object_ref TEXT PRIMARY KEY, state TEXT NOT NULL, hold_reason TEXT, attention TEXT,
                    identity TEXT, attested_by TEXT, intake_point TEXT, slot_ref TEXT,
                    updated_at TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 0);
                {RELEASE_SCHEMA};
            """)

    def _connect(self):
        c = sqlite3.connect(self.path, timeout=10)
        c.row_factory = sqlite3.Row
        return c

    def read(self, object_ref):
        with closing(self._connect()) as c:
            row = c.execute("SELECT * FROM intake WHERE object_ref=?", (object_ref,)).fetchone()
        if row is None:
            raise ValueError("Cargo is not on any declared trip")
        return dict(row)

    def unknown_events(self):
        """Events held as UNKNOWN because their cargo was never declared (or ROSY could not name it)."""
        with closing(self._connect()) as c:
            return [json.loads(r["payload"]) for r in c.execute(
                "SELECT payload FROM port_events WHERE outcome='HOLD_UNKNOWN' ORDER BY rowid")]

    def expect(self, object_ref, *, now):
        """Declare a cargo on a trip: the manifest that makes later events KNOWN."""
        _ref(object_ref)
        aware_time(now)
        with closing(self._connect()) as c, c:
            c.execute("INSERT OR IGNORE INTO intake(object_ref,state,updated_at) VALUES(?,'EXPECTED',?)",
                      (object_ref, now.isoformat()))

    @staticmethod
    def _advance(c, row, state, now, **fields):
        """Forward-only move; returns False (no change) when the cargo is held or already at/after state."""
        if row["state"] == "HOLD" or RANK[row["state"]] >= RANK[state]:
            return False
        sets = ",".join(f"{k}=?" for k in fields)
        c.execute(f"UPDATE intake SET state=?,updated_at=?,version=version+1{',' + sets if sets else ''} "
                  "WHERE object_ref=?", (state, now.isoformat(), *fields.values(), row["object_ref"]))
        return True

    def _row(self, c, object_ref):
        row = c.execute("SELECT * FROM intake WHERE object_ref=?", (object_ref,)).fetchone()
        if row is None:
            raise ValueError("Cargo is not on any declared trip")
        return row

    def arrive(self, object_ref, *, now):
        """The trip carrying this cargo reached its destination (Fleet GOAL_CONFIRMED)."""
        aware_time(now)
        with closing(self._connect()) as c, c:
            c.execute("BEGIN IMMEDIATE")
            return self._advance(c, self._row(c, object_ref), "TRIP_ARRIVED", now)

    def attest_received(self, object_ref, *, attested_by, now):
        """Named-person fallback (ADR 0023 style): the person saw this cargo at the intake point."""
        identifier(attested_by)
        aware_time(now)
        with closing(self._connect()) as c, c:
            c.execute("BEGIN IMMEDIATE")
            return self._advance(c, self._row(c, object_ref), "RECEIVED_AT_INTAKE", now,
                                 identity="attested", attested_by=attested_by, attention=None)

    def storing(self, object_ref, *, now):
        """Harbor opened the storing request (business ledger + ROSY proposal awaiting named approval)."""
        aware_time(now)
        with closing(self._connect()) as c, c:
            c.execute("BEGIN IMMEDIATE")
            row = self._row(c, object_ref)
            if row["state"] != "RECEIVED_AT_INTAKE":
                raise ValueError("Only a cargo received at the intake point can be stored")
            return self._advance(c, row, "STORING", now)

    def _apply(self, c, event, now):
        kind, ref = event["kind"], event["object_ref"]
        if kind == "vehicle.release_permitted":
            record_release(c, event)
            return "RELEASE_PERMITTED"
        row = None if ref is None else c.execute("SELECT * FROM intake WHERE object_ref=?", (ref,)).fetchone()
        if row is None:
            return "HOLD_UNKNOWN"
        if kind == "container.received_at_intake":
            done = self._advance(c, row, "RECEIVED_AT_INTAKE", now, identity=event["identity"],
                                 intake_point=event["intake_point"], attention=None)
        elif kind == "container.stored":
            done = self._advance(c, row, "COMPLETED", now, identity=event["identity"], slot_ref=event["slot_ref"])
        elif event["reason"] != "UNREADABLE":  # handling_failed means a person must handle it (MISMATCH, LOST, DAMAGED)
            done = row["state"] not in ("HOLD", "COMPLETED")
            if done:
                c.execute("UPDATE intake SET state='HOLD',hold_reason=?,updated_at=?,version=version+1 "
                          "WHERE object_ref=?", (event["reason"], now.isoformat(), ref))
        else:  # UNREADABLE waits for a named person's attestation instead of a HOLD.
            done = row["state"] not in ("HOLD", "COMPLETED")
            if done:
                c.execute("UPDATE intake SET attention=?,version=version+1 WHERE object_ref=?", (event["reason"], ref))
        return "APPLIED" if done else "IGNORED"

    def consume(self, events, *, now, connection=None, cursor=None):
        """Apply parsed events in order inside one transaction; replays are skipped. Returns outcomes."""
        aware_time(now)
        outcomes = {}
        with closing(self._connect()) as c, c:
            c.execute("BEGIN IMMEDIATE")
            for event in events:
                if c.execute("SELECT 1 FROM port_events WHERE event_id=?", (event["event_id"],)).fetchone():
                    outcomes[event["event_id"]] = "REPLAYED"
                    continue
                outcome = self._apply(c, event, now)
                payload = json.dumps(event, sort_keys=True, default=datetime.isoformat)
                c.execute("INSERT INTO port_events VALUES(?,?,?,?)", (event["event_id"], event["kind"], payload, outcome))
                outcomes[event["event_id"]] = outcome
            if connection is not None:
                c.execute("INSERT INTO port_cursor VALUES(?,?) ON CONFLICT(connection) DO UPDATE SET cursor=excluded.cursor",
                          (connection, cursor))
        return outcomes

    def expire(self, *, now, connection):
        """HOLD LOST every cargo that waited on ROSY longer than the timeout; returns their refs.

        Only after R has answered at least once on this connection (a stored cursor): before that the
        endpoint may be missing or unreachable, and silence says nothing about the cargo.
        """
        aware_time(now)
        with closing(self._connect()) as c, c:
            c.execute("BEGIN IMMEDIATE")
            if c.execute("SELECT 1 FROM port_cursor WHERE connection=?", (connection,)).fetchone() is None:
                return []
            rows = c.execute(f"SELECT object_ref, updated_at FROM intake WHERE state IN {WAITING_ON_ROSY}").fetchall()
            lost = [r["object_ref"] for r in rows
                    if now - datetime.fromisoformat(r["updated_at"]) > self.lost_after]
            for ref in lost:
                c.execute("UPDATE intake SET state='HOLD',hold_reason='LOST',updated_at=?,version=version+1 "
                          "WHERE object_ref=?", (now.isoformat(), ref))
        return lost

    def poll(self, client, *, connection, now, limit=100):
        """Read one page from R and apply it. complete:false only means wait; the timeout decides LOST."""
        identifier(connection)
        with closing(self._connect()) as c:
            row = c.execute("SELECT cursor FROM port_cursor WHERE connection=?", (connection,)).fetchone()
        body = client.port_events(after=None if row is None else row["cursor"], limit=limit)
        if (not isinstance(body, dict) or set(body) != {"events", "cursor", "complete"}
                or not isinstance(body["events"], list) or not isinstance(body["cursor"], str) or not body["cursor"]
                or type(body["complete"]) is not bool or len(body["events"]) > limit):
            raise ValueError("Unexpected port event page")
        events = [parse_event(e) for e in body["events"]]  # One bad event refuses the page; the cursor stays.
        outcomes = self.consume(events, now=now, connection=connection, cursor=body["cursor"])
        return dict(outcomes=outcomes, complete=body["complete"], lost=self.expire(now=now, connection=connection))
