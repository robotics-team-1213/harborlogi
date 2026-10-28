"""Durable transport intentions and Fleet references, never cargo completion or motion authority."""
from contextlib import closing
from dataclasses import dataclass
import hashlib
import json
import re
import sqlite3

from harbor.model import identifier, identifiers


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


@dataclass(frozen=True)
class TripIntent:
    request_key: str
    candidates: tuple[str, ...]
    destination: str
    via: tuple[str, ...] = ()

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

    @property
    def steps(self):
        return [{"to": self.destination, "via": list(self.via)}]

    @property
    def digest(self):
        return hashlib.sha256(_json({"candidates": list(self.candidates), "steps": self.steps}).encode()).hexdigest()

    def check(self, job):
        if (not isinstance(job, dict) or job.get("request_key") != self.request_key
                or job.get("request_digest") != self.digest or job.get("robot_id") not in self.candidates):
            raise ValueError("Fleet transport identity does not match the saved intent")
        identifier(job.get("mission_id"))
        if len(job["mission_id"]) > 192 or any(ord(c) < 32 for c in job["mission_id"]):
            raise ValueError("Invalid Fleet execution ID")
        steps = job.get("steps")
        if (not isinstance(steps, list) or len(steps) != 1 or not isinstance(steps[0], dict)
                or steps[0].get("step") != self.steps[0] or steps[0].get("step_index") != 0
                or type(job.get("current_step_index")) is not int
                or job["current_step_index"] != (1 if job.get("status") == "GOAL_CONFIRMED" else 0)
                or job.get("status") not in {"READY", "RUNNING", "ACTION_SUCCEEDED", "GOAL_CONFIRMED", "HOLD"}):
            raise ValueError("Fleet transport snapshot does not match the requested step")
        action, attempt = steps[0].get("action_id"), steps[0].get("attempt_id")
        if action is not None:
            identifier(action)
        if action != attempt or job["status"] == "GOAL_CONFIRMED" and (
                attempt is None or steps[0].get("status") != "GOAL_CONFIRMED"):
            raise ValueError("Fleet transport attempt is not consistent")
        return job


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
            # D-908 / API v1.329 proposal path: Harbor proposes once; a named operator admits in Fleet.
            connection.execute("CREATE TABLE IF NOT EXISTS transport_proposals (request_key TEXT PRIMARY KEY, "
                               "payload TEXT NOT NULL,digest TEXT NOT NULL,state TEXT NOT NULL, proposal_id TEXT, "
                               "mission_id TEXT,job_status TEXT,version INTEGER NOT NULL DEFAULT 0)")
            connection.execute("UPDATE transport_proposals SET state='UNCERTAIN',version=version+1 "
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
        return TripIntent(row["request_key"], tuple(value["candidates"]), value["destination"], tuple(value["via"]))

    def prepare(self, intent):
        if not isinstance(intent, TripIntent):
            raise ValueError("Expected an immutable transport intent")
        payload = _json({"candidates": intent.candidates, "destination": intent.destination, "via": intent.via})
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            old = connection.execute("SELECT digest FROM transport_intents WHERE request_key=?",
                                     (intent.request_key,)).fetchone()
            if old is not None and old["digest"] != intent.digest:
                raise ValueError("Transport request key conflicts with existing contents")
            connection.execute("INSERT OR IGNORE INTO transport_intents(request_key,payload,digest,state) "
                               "VALUES(?,?,?,'PLANNED')", (intent.request_key, payload, intent.digest))

    def _adopt(self, row, job):
        job = self._intent(row).check(job)
        if row["mission_id"] is not None and row["mission_id"] != job["mission_id"]:
            raise ValueError("Fleet transport identity changed")
        with closing(self._connect()) as connection, connection:
            result = connection.execute("UPDATE transport_intents SET state='ACKNOWLEDGED',mission_id=?,version=version+1 "
                "WHERE request_key=? AND version=?", (job["mission_id"], row["request_key"], row["version"]))
            if result.rowcount != 1:
                raise ValueError("Transport intent changed while the response was pending; reconcile again")
        return job

    def submit(self, key, client, *, operator_confirmed):
        if operator_confirmed is not True:
            raise ValueError("Explicit operator confirmation is required to start transport")
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
            result = client.trip(intent.candidates, intent.steps, request_key=key)
            job = client.trip_request(key)["job"]
            if result["job"]["mission_id"] != job["mission_id"]:
                raise ValueError("Transport submission and readback identify different executions")
            return self._adopt(row, job)
        except BaseException:
            with closing(self._connect()) as connection, connection:
                connection.execute("UPDATE transport_intents SET state='UNCERTAIN',version=version+1 "
                                   "WHERE request_key=? AND version=? AND state='SUBMITTING'", (key, row["version"]))
            raise

    def reconcile(self, key, client):
        row = self.read(key)
        if row["state"] == "SUBMITTING":
            raise ValueError("Submission is still pending")
        return self._adopt(row, client.trip_request(key)["job"])

    # --- D-908 proposal path (API v1.329). States: PLANNED, SUBMITTING, UNCERTAIN, PROPOSED (pending operator
    # admission), ADMITTED (job exists), GOAL_CONFIRMED (transport fact only, never cargo arrival).
    # This ledger never admits: the client has no admit method and admission is a named operator's act.

    def read_proposal(self, key):
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT * FROM transport_proposals WHERE request_key=?", (key,)).fetchone()
        return None if row is None else dict(row)

    def open_proposal_keys(self):
        with closing(self._connect()) as connection:
            return [r[0] for r in connection.execute(
                "SELECT request_key FROM transport_proposals WHERE state!='GOAL_CONFIRMED' ORDER BY rowid")]

    def prepare_proposal(self, intent):
        if not isinstance(intent, TripIntent):
            raise ValueError("Expected an immutable transport intent")
        payload = _json({"candidates": intent.candidates, "destination": intent.destination, "via": intent.via})
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            old = connection.execute("SELECT digest FROM transport_proposals WHERE request_key=?",
                                     (intent.request_key,)).fetchone()
            if old is not None and old["digest"] != intent.digest:
                raise ValueError("Transport request key conflicts with existing contents")
            connection.execute("INSERT OR IGNORE INTO transport_proposals(request_key,payload,digest,state) "
                               "VALUES(?,?,?,'PLANNED')", (intent.request_key, payload, intent.digest))

    def _set_proposal(self, row, state, **columns):
        names = "".join(f",{name}=?" for name in columns)
        with closing(self._connect()) as connection, connection:
            result = connection.execute(
                f"UPDATE transport_proposals SET state=?,version=version+1{names} WHERE request_key=? AND version=?",
                (state, *columns.values(), row["request_key"], row["version"]))
            if result.rowcount != 1:
                raise ValueError("Transport proposal changed while the response was pending; reconcile again")

    def _check_proposal(self, intent, proposal):
        if not isinstance(proposal, dict):
            raise ValueError("Fleet trip proposal does not match the saved intent")
        admitted = proposal.get("status") == "ADMITTED"
        if (proposal.get("request_key") != intent.request_key
                or proposal.get("request_digest") != intent.digest
                or proposal.get("candidate_robot_ids") != list(intent.candidates)
                or proposal.get("steps") != intent.steps or proposal.get("status") not in {"PROPOSED", "ADMITTED"}
                or admitted != all(proposal.get(k) is not None for k in ("approved_by", "mission_id", "dispatch_generation"))
                or admitted and proposal["approved_by"] == proposal.get("proposal_principal_id")):
            raise ValueError("Fleet trip proposal does not match the saved intent")
        identifier(proposal.get("proposal_id"))
        return proposal

    def propose(self, key, client):
        """Send the proposal exactly once. Any failure leaves UNCERTAIN; recover with reconcile_proposal(key)."""
        row = self.read_proposal(key)
        if row is None:
            raise ValueError("Unknown transport proposal")
        if row["state"] != "PLANNED":
            raise ValueError("Proposal was already sent or is uncertain; reconcile without resending")
        self._set_proposal(row, "SUBMITTING")
        row = {**row, "version": row["version"] + 1}
        intent = self._intent(row)
        try:
            sent = client.trip_propose(intent.candidates, intent.steps, request_key=key)["proposal"]
            self._check_proposal(intent, sent)
            return self._adopt_proposal(row, intent, client)
        except BaseException:
            with closing(self._connect()) as connection, connection:
                connection.execute("UPDATE transport_proposals SET state='UNCERTAIN',version=version+1 "
                                   "WHERE request_key=? AND version=? AND state='SUBMITTING'", (key, row["version"]))
            raise

    def reconcile_proposal(self, key, client):
        """Read the original key only: proposal first, then the admitted job through the v1.298 key lookup."""
        row = self.read_proposal(key)
        if row is None:
            raise ValueError("Unknown transport proposal")
        if row["state"] in ("PLANNED", "SUBMITTING"):
            raise ValueError("Proposal has not been sent; propose it or wait for the pending call")
        return self._adopt_proposal(row, self._intent(row), client)

    def _adopt_proposal(self, row, intent, client):
        proposal = self._check_proposal(intent, client.trip_proposal_request(row["request_key"])["proposal"])
        if row["proposal_id"] not in (None, proposal["proposal_id"]):
            raise ValueError("Fleet proposal identity changed")
        if proposal["status"] == "PROPOSED":
            if row["state"] in ("ADMITTED", "GOAL_CONFIRMED"):
                raise ValueError("Fleet proposal regressed after admission")
            self._set_proposal(row, "PROPOSED", proposal_id=proposal["proposal_id"])
        else:
            job = intent.check(client.trip_request(row["request_key"])["job"])
            if job["mission_id"] != proposal["mission_id"]:
                raise ValueError("Admitted proposal and trip job identify different executions")
            self._set_proposal(row, "GOAL_CONFIRMED" if job["status"] == "GOAL_CONFIRMED" else "ADMITTED",
                               proposal_id=proposal["proposal_id"], mission_id=job["mission_id"],
                               job_status=job["status"])
        return self.read_proposal(row["request_key"])
