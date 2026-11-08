"""Durable transport intentions and Fleet references, never cargo completion or motion authority."""
from contextlib import closing
from dataclasses import dataclass
import json
import re
import sqlite3

from harbor.model import identifier, identifiers
from harbor.execution import (read_trip_execution, read_trip_proposal_execution,
                              submit_trip_execution, submit_trip_proposal_execution)


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


@dataclass(frozen=True)
class TripIntent:
    request_key: str
    candidates: tuple[str, ...]
    destination: str
    via: tuple[str, ...] = ()
    object_ref: str = ''  # Harbor cargo code ROSY carries unread onto the S job (ADR 0029); empty = none.

    def __post_init__(self):
        identifier(self.request_key)
        identifiers(self.candidates)
        if (len(self.request_key) > 128 or any(ord(c) < 32 for c in self.request_key)
                or not 1 <= len(self.candidates) <= 8 or not isinstance(self.via, tuple) or len(self.via) > 8):
            raise ValueError("Invalid bounded transport intent")
        for candidate in self.candidates:
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}", candidate):
                raise ValueError("Invalid candidate robot ID")
        for place in (self.destination, *self.via):
            identifier(place)
            if len(place) > 64 or any(ord(c) < 32 for c in place):
                raise ValueError("Invalid registered place reference")
        if self.object_ref and (self.object_ref != self.object_ref.strip() or len(self.object_ref) > 128
                                or any(ord(c) < 32 for c in self.object_ref)):
            raise ValueError("Invalid cargo object reference")

    @property
    def steps(self):
        from harbor.fleet_contract import trip_steps
        return trip_steps(self)

    @property
    def digest(self):
        from harbor.fleet_contract import trip_digest
        return trip_digest(self)

    def check(self, job):
        from harbor.fleet_contract import validate_trip_job
        return validate_trip_job(self, job)


class TransportLedger:
    """Save intents before POST; use one ledger per configured Fleet principal/origin.

    ACKNOWLEDGED means a matching Fleet reference was read, including a held job.
    Business constraints and authenticated cargo/clearance observations remain with the caller.
    """
    def __init__(self, path):
        self.path = str(path)
        with closing(self._connect()) as connection, connection:
            connection.execute("CREATE TABLE IF NOT EXISTS transport_intents (request_key TEXT PRIMARY KEY, "
                               "payload TEXT NOT NULL,digest TEXT NOT NULL,state TEXT NOT NULL, "
                               "mission_id TEXT,version INTEGER NOT NULL DEFAULT 0)")
            connection.execute("UPDATE transport_intents SET state='UNCERTAIN',version=version+1 "
                               "WHERE state='SUBMITTING'")

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def read(self, key):
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT * FROM transport_intents WHERE request_key=?", (key,)).fetchone()
        if row is None:
            raise ValueError("Unknown transport intent")
        return dict(row)

    @staticmethod
    def _intent(row):
        value = json.loads(row["payload"])
        return TripIntent(row["request_key"], tuple(value["candidates"]), value["destination"], tuple(value["via"]),
                          value.get("object_ref", ""))

    def prepare(self, intent):
        if not isinstance(intent, TripIntent):
            raise ValueError("Expected an immutable transport intent")
        cargo = {"object_ref": intent.object_ref} if intent.object_ref else {}  # Older rows keep their bytes.
        payload = _json({"candidates": intent.candidates, "destination": intent.destination, "via": intent.via, **cargo})
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            old = connection.execute("SELECT digest, payload FROM transport_intents WHERE request_key=?",
                                     (intent.request_key,)).fetchone()
            if old is not None and (old["digest"] != intent.digest or old["payload"] != payload):
                raise ValueError("Transport request key conflicts with existing contents")
            connection.execute("INSERT OR IGNORE INTO transport_intents(request_key,payload,digest,state) "
                               "VALUES(?,?,?,'PLANNED')", (intent.request_key, payload, intent.digest))

    def _adopt(self, row, execution):
        # A proposal waiting for another operator has no job yet; it must not be mistaken for an accepted trip.
        state = "AWAITING_APPROVAL" if execution.phase == "awaiting_approval" else "ACKNOWLEDGED"
        if row["mission_id"] is not None and row["mission_id"] != execution.mission_id:
            raise ValueError("Fleet transport identity changed")
        with closing(self._connect()) as connection, connection:
            result = connection.execute("UPDATE transport_intents SET state=?,mission_id=?,version=version+1 "
                "WHERE request_key=? AND version=?", (state, execution.mission_id, row["request_key"], row["version"]))
            if result.rowcount != 1:
                raise ValueError("Transport intent changed while the response was pending; reconcile again")
        return execution.job

    def submit(self, key, client, *, operator_confirmed):
        """Direct named-operator TRIP start; a service identity must use propose()."""
        if operator_confirmed is not True:
            raise ValueError("Explicit operator confirmation is required to start transport")
        return self._submit(key, client, submit_trip_execution)

    def propose(self, key, client):
        """D-908: ask Fleet for approval once. Nothing moves until a different named operator admits."""
        return self._submit(key, client, submit_trip_proposal_execution)

    def _submit(self, key, client, send):
        row = self.read(key)
        if row["state"] != "PLANNED":
            raise ValueError("Transport was already submitted or is uncertain; reconcile without resubmitting")
        with closing(self._connect()) as connection, connection:
            result = connection.execute("UPDATE transport_intents SET state='SUBMITTING',version=version+1 "
                                       "WHERE request_key=? AND version=? AND state='PLANNED'", (key, row["version"]))
            if result.rowcount != 1:
                raise ValueError("Transport intent changed before submission")
        row = {**row, "version": row["version"] + 1, "state": "SUBMITTING"}
        intent = self._intent(row)
        try:
            job = send(client, intent)
            return self._adopt(row, job)
        except BaseException:
            with closing(self._connect()) as connection, connection:
                connection.execute("UPDATE transport_intents SET state='UNCERTAIN',version=version+1 "
                                   "WHERE request_key=? AND version=? AND state='SUBMITTING'", (key, row["version"]))
            raise

    def reconcile(self, key, client):
        return self._reconcile(key, client, read_trip_execution)

    def reconcile_proposal(self, key, client):
        return self._reconcile(key, client, read_trip_proposal_execution)

    def _reconcile(self, key, client, read):
        row = self.read(key)
        if row["state"] == "SUBMITTING":
            raise ValueError("Submission is still pending")
        return self._adopt(row, read(client, self._intent(row)))
