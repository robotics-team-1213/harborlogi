from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import sqlite3

from harbor.ledger import business_row, plan_payload as _payload
from harbor.hold_notify import (OUTBOX_SCHEMA, new_notification_id, record_hold, screen_url,
                                validated_screen_base)
from harbor.model import YARD_ARMS, aware_time, identifier, identifiers, revision
from harbor.process import ArmTransferBinding, ProcessBinding
from harbor.execution import read_arm_execution, prepare_execution, submit_execution, read_cell_execution


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)




def _header(evidence):
    for value in (evidence.evidence_id, evidence.mission_id, evidence.attempt_id, evidence.source_id):
        identifier(value)
    revision(evidence.revision)
    aware_time(evidence.observed_at)


@dataclass(frozen=True)
class PlacementEvidence:
    """Camera observation that a cargo lies at a cell location (내려놓기), separate from action results."""
    evidence_id: str
    mission_id: str
    attempt_id: str
    revision: int
    source_id: str
    observed_at: datetime
    cargo_id: str
    location: str
    attested_by: str = ""  # A named person's visual verdict (ADR 0023); empty means an automatic producer.

    def __post_init__(self):
        _header(self)
        identifier(self.cargo_id)
        identifier(self.location)
        if self.attested_by:
            identifier(self.attested_by)


@dataclass(frozen=True)
class ArmRetreat:
    """Retreat facts (퇴피) of one arm that acted, as Fleet reports them: zone release and clearance verdict.

    Fleet chooses which arm acts (ADR 0029 decision 6), so `arm` is an opaque participant label
    and Harbor stores no joint values.
    """
    arm: str
    zone_id: str
    claim_released: bool
    pose_observed_at: datetime
    pose_revision: int
    outside_zone: bool  # The platform's verdict; Harbor does not compute kinematics.

    def __post_init__(self):
        identifier(self.arm)
        identifier(self.zone_id)
        if type(self.claim_released) is not bool or type(self.outside_zone) is not bool:
            raise ValueError("Physical facts must be explicit booleans")
        aware_time(self.pose_observed_at)
        revision(self.pose_revision)


@dataclass(frozen=True)
class HumanArmRetreat:
    """A named person saw this yard arm waiting outside the zone (ADR 0023): a verdict, not a measurement."""
    arm: str
    attested_by: str
    outside_zone_seen: bool

    def __post_init__(self):
        if self.arm not in YARD_ARMS:
            raise ValueError("Retreat evidence covers yard arms A/B only")
        identifier(self.attested_by)
        if type(self.outside_zone_seen) is not bool:
            raise ValueError("Physical facts must be explicit booleans")


@dataclass(frozen=True)
class RetreatEvidence:
    """Platform-data retreat record for the arms that acted in a step; no camera is involved."""
    evidence_id: str
    mission_id: str
    attempt_id: str
    revision: int
    source_id: str
    observed_at: datetime
    arms: tuple[ArmRetreat | HumanArmRetreat, ...]

    def __post_init__(self):
        _header(self)
        if not isinstance(self.arms, tuple) or not all(isinstance(a, (ArmRetreat, HumanArmRetreat)) for a in self.arms):
            raise ValueError("Expected immutable arm retreat facts")
        identifiers(tuple(a.arm for a in self.arms))


@dataclass(frozen=True)
class MoveEvidence:
    """The two independent facts a step needs (ADR 0005 decision 6); each keeps its own source and time."""
    placement: PlacementEvidence
    retreat: RetreatEvidence


class BusinessLedger:
    def __init__(self, path, *, placement_sources, retreat_sources, freshness=timedelta(seconds=5),
                 human_placement_sources=(), human_retreat_sources=(), human_freshness=timedelta(minutes=5),
                 notify_expiry=timedelta(hours=1), screen_base=None, clock=None):
        identifiers(placement_sources)
        identifiers(retreat_sources)
        identifiers(human_placement_sources)
        identifiers(human_retreat_sources)
        every = [placement_sources, retreat_sources, human_placement_sources, human_retreat_sources]
        if (not placement_sources or not retreat_sources
                or sum(len(set(group)) for group in every) != len(set().union(*every))
                or not isinstance(freshness, timedelta) or freshness <= timedelta(0)
                or not isinstance(human_freshness, timedelta) or human_freshness <= timedelta(0)
                or not isinstance(notify_expiry, timedelta) or notify_expiry <= timedelta(0)):
            raise ValueError("Separate configured placement and retreat sources and positive freshness are required")
        self.path, self.freshness, self.human_freshness = str(path), freshness, human_freshness
        self.placement_sources, self.retreat_sources = placement_sources, retreat_sources
        self.human_placement_sources, self.human_retreat_sources = human_placement_sources, human_retreat_sources
        self.notify_expiry = notify_expiry
        self._screen_base = validated_screen_base(screen_base)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._reads = {}
        with closing(self._connect()) as c, c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS business (
                    request_key TEXT PRIMARY KEY, digest TEXT NOT NULL, payload TEXT NOT NULL,
                    state TEXT NOT NULL, step_index INTEGER NOT NULL DEFAULT 0,
                    row_version INTEGER NOT NULL DEFAULT 0,
                    mission_id TEXT, attempt_id TEXT, goal_confirmed INTEGER NOT NULL DEFAULT 0,
                    outbound_mission_id TEXT, outbound_attempt_id TEXT,
                    hold_sequence INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS evidence (
                    source_id TEXT NOT NULL, evidence_id TEXT NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY(source_id,evidence_id));
            """ + OUTBOX_SCHEMA)
            c.execute("BEGIN IMMEDIATE")
            columns = {r[1] for r in c.execute("PRAGMA table_info(business)")}
            if "row_version" not in columns:
                c.execute("ALTER TABLE business ADD COLUMN row_version INTEGER NOT NULL DEFAULT 0")
            if "hold_sequence" not in columns:
                c.execute("ALTER TABLE business ADD COLUMN hold_sequence INTEGER NOT NULL DEFAULT 0")
            for old in ("mission_id", "attempt_id"):  # ADR 0005 names; rows keep their values.
                if "target_" + old in columns:
                    c.execute(f"ALTER TABLE business RENAME COLUMN target_{old} TO outbound_{old}")
            # Restart recovery is itself a HOLD occurrence (D-997); one outbox row per request.
            for row in c.execute("SELECT request_key FROM business WHERE state='SUBMITTING'").fetchall():
                c.execute("UPDATE business SET state='HOLD',row_version=row_version+1 "
                          "WHERE request_key=? AND state='SUBMITTING'", (row[0],))
                self._notify_hold(c, row[0], "SUBMITTING", self._clock())

    def _notify_hold(self, c, key, previous_state, now):
        """Record one HOLD occurrence in the caller's transaction (D-997; never on HOLD → HOLD)."""
        if previous_state == "HOLD":
            return
        sequence = self._row(c, key)["hold_sequence"] + 1
        c.execute("UPDATE business SET hold_sequence=? WHERE request_key=?", (sequence, key))
        notification = new_notification_id()
        record_hold(c, key, sequence, notification_id=notification, now=now,
                    expires_at=now + self.notify_expiry,
                    screen_url=screen_url(self._screen_base, notification))

    def _connect(self):
        c = sqlite3.connect(self.path, timeout=10)
        c.row_factory = sqlite3.Row
        return c

    @staticmethod
    def _row(c, key):
        return business_row(c, key)

    def read(self, key):
        with closing(self._connect()) as c:
            return self._row(c, key)

    def _check_current(self, c, key, original):
        current = self._row(c, key)
        if (current["row_version"] != original["row_version"] or current["state"] != original["state"]
                or current["step_index"] != original["step_index"] or current["digest"] != original["digest"]):
            raise ValueError("Business stage changed while the Fleet request was pending")

    def prepare(self, key, plan_revision, moves, *, outbound_id, outbound_stand, move_arms):
        identifier(key)
        if len(key) > 128 or any(ord(c) < 32 for c in key):
            raise ValueError("Invalid business request key")
        revision(plan_revision)
        identifier(outbound_id)
        identifier(outbound_stand)
        if (not isinstance(moves, tuple) or not moves
                or not all(isinstance(m, (ProcessBinding, ArmTransferBinding)) for m in moves)
                or len({type(m) for m in moves}) != 1):
            raise ValueError("Expected a nonempty immutable bound plan")
        if sum(m.cargo_id == outbound_id for m in moves) != 1 or not any(
                m.cargo_id == outbound_id and m.destination == outbound_stand for m in moves):
            raise ValueError("Expected exactly one outbound move to the outbound stand")
        # Primary then alternates per step, kept for display; Fleet picks the arm that acts (ADR 0029).
        if (not isinstance(move_arms, tuple) or len(move_arms) != len(moves)
                or not all(isinstance(a, tuple) and a and set(a) <= set(YARD_ARMS) and len(set(a)) == len(a)
                           for a in move_arms)):
            raise ValueError("Expected nonempty A/B arm roles for every move")
        mode = {"execution_kind": "ARM_TRANSFER"} if isinstance(moves[0], ArmTransferBinding) else {}
        payload = _json({**mode, "revision": plan_revision, "moves": [asdict(m) for m in moves],
                         "outbound_id": outbound_id, "outbound_stand": outbound_stand,
                         "move_arms": [list(a) for a in move_arms]})
        digest = hashlib.sha256(payload.encode()).hexdigest()
        with closing(self._connect()) as c, c:
            c.execute("BEGIN IMMEDIATE")
            old = c.execute("SELECT digest FROM business WHERE request_key=?", (key,)).fetchone()
            if old is not None and old[0] != digest:
                raise ValueError("Business request key conflicts with existing contents")
            c.execute("INSERT OR IGNORE INTO business(request_key,digest,payload,state) VALUES(?,?,?,'PLANNED')",
                      (key, digest, payload))

    def propose(self, key, client):
        return self.submit(key, client).mission_id

    def submit(self, key, client, *, principal=None):
        row = self.read(key)
        if row["state"] != "PLANNED":
            raise ValueError("Current stage cannot submit a proposal")
        payload = _payload(row)
        neutral = payload.get("execution_kind") == "ARM_TRANSFER"
        binding = (ArmTransferBinding if neutral else ProcessBinding)(**payload["moves"][row["step_index"]])
        prepare_execution(client, binding)
        with closing(self._connect()) as c, c:
            c.execute("BEGIN IMMEDIATE")
            self._check_current(c, key, row)
            c.execute("UPDATE business SET state='SUBMITTING',row_version=row_version+1 WHERE request_key=?", (key,))
        intent_version = row["row_version"] + 1
        request_key = f"{key}:{row['step_index']}"
        try:
            execution = submit_execution(client, binding, request_key, principal=principal)
            mission_id = execution.mission_id
        except BaseException:
            with closing(self._connect()) as c, c:
                entered = c.execute("UPDATE business SET state='HOLD',row_version=row_version+1 "
                                    "WHERE request_key=? AND state='SUBMITTING' AND row_version=?",
                                    (key, intent_version)).rowcount
                if entered:
                    self._notify_hold(c, key, "SUBMITTING", self._clock())
            raise
        with closing(self._connect()) as c, c:
            c.execute("BEGIN IMMEDIATE")
            current = self._row(c, key)
            if current["state"] == "SUBMITTING" and current["row_version"] == intent_version:
                state = "WAITING"
            elif (current["state"] == "HOLD" and current["step_index"] == row["step_index"]
                  and current["digest"] == row["digest"] and current["mission_id"] is None):
                state = "HOLD"
            else:
                raise ValueError("Late proposal cannot overwrite a different business stage")
            c.execute("UPDATE business SET state=?,mission_id=?,attempt_id=NULL,goal_confirmed=0,row_version=row_version+1 "
                      "WHERE request_key=?", (state, mission_id, key))
        if state == "HOLD":
            raise ValueError("Late proposal response retained for reconciliation; restart HOLD remains")
        return execution

    def hold(self, key):
        with closing(self._connect()) as c, c:
            c.execute('BEGIN IMMEDIATE')
            row = self._row(c, key)
            c.execute("UPDATE business SET state='HOLD',row_version=row_version+1 WHERE request_key=?", (key,))
            self._notify_hold(c, key, row["state"], self._clock())

    def resume_arm(self, key, client, *, now):
        """Re-enter WAITING from a bookkeeping HOLD after reconciling the original request key.

        A stage is held by uncertainty (lost response, transient read failure), not by an
        operator decision. Resume re-reads the original request key, adopts the mission it
        still names, and only then re-enters WAITING with the job's current status. It never
        posts a new request and leaves cancellation bookkeeping to hold().
        """
        row = self.read(key)
        if row['state'] != 'HOLD':
            raise ValueError('Only held physical transfers can be resumed')
        return self._reconcile_arm(key, client, row, now=now, resume=True).job

    def recover_arm(self, key, client, *, now):
        row = self.read(key)
        if row['state'] != 'HOLD':
            raise ValueError('Only held physical transfers can be recovered')
        return self._reconcile_arm(key, client, row, now=now).job

    def reconcile_arm(self, key, client, *, now, resume=False, principal=None):
        """Apply one validated read; a bookkeeping HOLD only resumes when requested."""
        return self._reconcile_arm(key, client, self.read(key), now=now, resume=resume, principal=principal)

    def _reconcile_arm(self, key, client, row, *, now, resume=False, principal=None):
        aware_time(now)
        payload = _payload(row)
        if row['state'] not in ('WAITING', 'HOLD') or payload.get('execution_kind') != 'ARM_TRANSFER':
            raise ValueError('No current physical transfer to reconcile')
        if hashlib.sha256(_json(payload).encode()).hexdigest() != row['digest']:
            raise ValueError('Saved business body changed')
        binding = ArmTransferBinding(**payload['moves'][row['step_index']])
        request_key = f"{key}:{row['step_index']}"
        execution = read_arm_execution(client, binding, request_key,
                                       mission_id=row['mission_id'], principal=principal)
        progressing = row['state'] == 'WAITING' or resume
        state = 'WAITING' if progressing and not execution.held else 'HOLD'
        attempt = execution.attempt_id if progressing else row['attempt_id']
        confirmed = execution.goal_confirmed if progressing else row['goal_confirmed']
        with closing(self._connect()) as c, c:
            c.execute('BEGIN IMMEDIATE')
            self._check_current(c, key, row)
            c.execute('UPDATE business SET mission_id=?,attempt_id=?,goal_confirmed=?,state=?,'
                      'row_version=row_version+1 WHERE request_key=?',
                      (execution.mission_id, attempt, int(confirmed), state, key))
            if state == 'HOLD':
                self._notify_hold(c, key, row['state'], now)
        if progressing:
            self._reads[key] = (execution.mission_id, attempt, now, row['row_version'] + 1)
        return execution

    def refresh(self, key, client, *, now):
        aware_time(now)
        row = self.read(key)
        if row["state"] != "WAITING":
            raise ValueError("No current execution to reconcile")
        payload = _payload(row)
        if payload.get("execution_kind") == "ARM_TRANSFER":
            return self._reconcile_arm(key, client, row, now=now)
        execution = read_cell_execution(client, row['mission_id'], f"{key}:{row['step_index']}")
        attempt, confirmed = execution.attempt_id, execution.goal_confirmed
        state = "HOLD" if execution.held else "WAITING"
        with closing(self._connect()) as c, c:
            c.execute("BEGIN IMMEDIATE")
            self._check_current(c, key, row)
            c.execute("UPDATE business SET attempt_id=?,goal_confirmed=?,state=?,row_version=row_version+1 WHERE request_key=?",
                      (attempt, int(confirmed), state, key))
            if state == "HOLD":
                self._notify_hold(c, key, row["state"], now)
        self._reads[key] = (row["mission_id"], attempt, now, row["row_version"] + 1)

    def _validate(self, row, evidence, now, *, mission_id, attempt_id, cargo_id, location):
        aware_time(now)
        payload = _payload(row)
        if not isinstance(evidence, MoveEvidence):
            raise ValueError("Placement and retreat evidence are both required")
        placement, retreat = evidence.placement, evidence.retreat

        # A named person's verdict (ADR 0023) rides separate source ids and its own freshness window;
        # it is never mixed with automatic facts, and nothing here reads Fleet state.
        human = isinstance(placement, PlacementEvidence) and bool(placement.attested_by)
        window = self.human_freshness if human else self.freshness

        def bound(e, kind, sources):
            if (not isinstance(e, kind) or e.source_id not in sources
                    or e.revision != payload["revision"] or e.mission_id != mission_id
                    or e.attempt_id != attempt_id
                    or not timedelta(0) <= now - e.observed_at <= window):
                raise ValueError("Physical evidence does not match the current business execution")

        bound(placement, PlacementEvidence, self.human_placement_sources if human else self.placement_sources)
        if placement.cargo_id != cargo_id or placement.location != location:
            raise ValueError("Placement evidence does not show the cargo at its destination")
        bound(retreat, RetreatEvidence, self.human_retreat_sources if human else self.retreat_sources)
        # Only the arms that acted must show retreat; Fleet owns fallback switching (ADR 0029, D3).
        if not retreat.arms:
            raise ValueError("Retreat evidence names no arm that acted")
        for fact in retreat.arms:
            if human:
                ok = (isinstance(fact, HumanArmRetreat) and fact.outside_zone_seen
                      and fact.attested_by == placement.attested_by)
            else:
                ok = (isinstance(fact, ArmRetreat) and fact.claim_released and fact.outside_zone
                      and timedelta(0) <= now - fact.pose_observed_at <= self.freshness)
            if not ok:
                raise ValueError("Retreat evidence does not show every related arm out of the space")

    @staticmethod
    def _save_evidence(c, evidence):
        for part in (evidence.placement, evidence.retreat):
            payload = json.dumps(asdict(part), sort_keys=True, separators=(",", ":"), allow_nan=False,
                                 default=datetime.isoformat)
            old = c.execute("SELECT payload FROM evidence WHERE source_id=? AND evidence_id=?",
                            (part.source_id, part.evidence_id)).fetchone()
            if old is not None:
                raise ValueError("Observation identity was already consumed")
            c.execute("INSERT INTO evidence VALUES(?,?,?)", (part.source_id, part.evidence_id, payload))

    def confirm(self, key, evidence, *, now):
        with closing(self._connect()) as c, c:
            c.execute("BEGIN IMMEDIATE")
            row = self._row(c, key)
            payload = _payload(row)
            if row["state"] != "WAITING" or not row["goal_confirmed"]:
                raise ValueError("Fleet goal confirmation is required")
            read = self._reads.get(key)
            if (read is None or read[:2] != (row["mission_id"], row["attempt_id"])
                    or read[3] != row["row_version"]
                    or not timedelta(0) <= now - read[2] <= self.freshness):
                raise ValueError("Fresh Fleet reconciliation is required")
            move = payload["moves"][row["step_index"]]
            self._validate(row, evidence, now, mission_id=row["mission_id"], attempt_id=row["attempt_id"],
                           cargo_id=move["cargo_id"], location=move["destination"])
            self._save_evidence(c, evidence)
            if move["cargo_id"] == payload["outbound_id"]:
                c.execute("UPDATE business SET outbound_mission_id=mission_id,outbound_attempt_id=attempt_id "
                          "WHERE request_key=?", (key,))
            index = row["step_index"] + 1
            state = "AWAITING_FINAL_EVIDENCE" if index == len(payload["moves"]) else "PLANNED"
            c.execute("UPDATE business SET state=?,step_index=?,mission_id=NULL,attempt_id=NULL,goal_confirmed=0,row_version=row_version+1 "
                      "WHERE request_key=?", (state, index, key))
        if self._reads.get(key) == read:
            self._reads.pop(key, None)

    def finish(self, key, evidence, *, now):
        with closing(self._connect()) as c, c:
            c.execute("BEGIN IMMEDIATE")
            row = self._row(c, key)
            payload = _payload(row)
            if row["state"] != "AWAITING_FINAL_EVIDENCE":
                raise ValueError("Outbound placement and all restored moves are required")
            self._validate(row, evidence, now, mission_id=row["outbound_mission_id"],
                           attempt_id=row["outbound_attempt_id"], cargo_id=payload["outbound_id"],
                           location=payload["outbound_stand"])
            self._save_evidence(c, evidence)
            c.execute("UPDATE business SET state='COMPLETED',row_version=row_version+1 WHERE request_key=?", (key,))
