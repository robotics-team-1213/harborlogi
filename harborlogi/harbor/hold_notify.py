"""Business HOLD notification outbox (ROSY D-997 Decision 6, Harbor row).

Each non-HOLD → HOLD transition of the business ledger records one occurrence
in the same SQLite transaction. Notification bodies carry only the opaque
notification id — never the business request key — and the screen link points
at the HTTPS-protected work screen. Delivery attempts hold per-(occurrence,
channel) state, send outside any SQLite write lock, and treat an unclear
submission (timeout, ambiguous failure, interrupted send) as UNKNOWN with no
automatic resend. The platform notification delivery service itself is still
a target of ROSY D-997, not an existing dependency.
"""
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import re
import secrets
from urllib.parse import urlsplit

from harbor.model import aware_time, identifier

CLASSIFICATION = "INTERVENTION_REQUIRED"
RETRY_INTERVAL = timedelta(seconds=60)

OUTBOX_SCHEMA = """
    CREATE TABLE IF NOT EXISTS hold_outbox (
        occurrence_id TEXT PRIMARY KEY, notification_id TEXT NOT NULL UNIQUE,
        request_key TEXT NOT NULL, hold_sequence INTEGER NOT NULL,
        occurred_at TEXT NOT NULL, expires_at TEXT NOT NULL, payload TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS hold_delivery (
        occurrence_id TEXT NOT NULL, channel TEXT NOT NULL,
        state TEXT NOT NULL, note TEXT, attempted_at TEXT,
        PRIMARY KEY(occurrence_id,channel));
"""


class DefiniteDeliveryFailure(Exception):
    """The channel refused the delivery before any ambiguity; a later retry is allowed."""


class UnknownDeliveryFailure(Exception):
    """The submission outcome is unclear (timeout/5xx-style); never blindly resend (D-997)."""


def occurrence_id(key, sequence):
    return f"{key}#hold-{sequence}"


def new_notification_id():
    """Opaque public id for one HOLD occurrence; the raw business key never leaves the ledger.
    The 'n' prefix keeps the id off numeric screen routes (/work/<command pk>/)."""
    return 'n' + secrets.token_hex(16)


def is_notification_id(value):
    return isinstance(value, str) and re.fullmatch(r'n[0-9a-f]{32}', value) is not None


def validated_screen_base(value):
    """HTTPS-only base for the protected (login) work screen; the link must not point at a
    local-only surface, so plain HTTP — loopback included — is rejected."""
    if value is None:
        return None
    parsed = urlsplit(value)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path not in ('', '/')
            or any(ord(c) <= 32 for c in value) or '\\' in value):
        raise ValueError("Expected an HTTPS work screen base without credentials or query")
    parsed.port  # Reject malformed ports before emitting any link.
    return value.rstrip('/')


def screen_url(base, notification_id):
    """Tokenless link; the protected screen resolves the business through this notification id."""
    return None if base is None else f"{base}/work/{notification_id}"


def record_hold(c, key, sequence, *, notification_id, now, expires_at, screen_url):
    """Write one HOLD occurrence inside the caller's business transaction (D-997 atomicity)."""
    aware_time(now)
    aware_time(expires_at)
    occurrence = occurrence_id(key, sequence)
    # Only the D-997 fields: classification, the opaque occurrence id, times, and the
    # tokenless link. Business correlation happens on the protected screen, not here.
    payload = json.dumps({"classification": CLASSIFICATION, "notification_id": notification_id,
                          "occurred_at": now.isoformat(), "expires_at": expires_at.isoformat(),
                          "screen_url": screen_url},
                         sort_keys=True, separators=(",", ":"), allow_nan=False)
    c.execute("INSERT INTO hold_outbox(occurrence_id,notification_id,request_key,hold_sequence,"
              "occurred_at,expires_at,payload) VALUES(?,?,?,?,?,?,?)",
              (occurrence, notification_id, key, sequence, now.isoformat(), expires_at.isoformat(), payload))
    return occurrence


def _undeliverable(c, row, now):
    if now > datetime.fromisoformat(row["expires_at"]):
        return "Occurrence expired"
    business = c.execute("SELECT state,hold_sequence FROM business WHERE request_key=?",
                         (row["request_key"],)).fetchone()
    if business is None or business["state"] != "HOLD" or business["hold_sequence"] != row["hold_sequence"]:
        return "Business left this HOLD occurrence"
    return None


def pre_send_check(c, occurrence, *, channel, channels, now):
    """D-997 pre-send validity: same business, same occurrence still HOLD, channel in receive scope."""
    aware_time(now)
    if channel not in channels:
        raise ValueError("Channel is outside the approved receive scope")
    row = c.execute("SELECT * FROM hold_outbox WHERE occurrence_id=?", (occurrence,)).fetchone()
    if row is None:
        raise ValueError("Unknown HOLD occurrence")
    reason = _undeliverable(c, row, now)
    if reason is not None:
        raise ValueError(reason)
    return dict(row)


def _retry_ready(attempted_at, now):
    return attempted_at is None or now - datetime.fromisoformat(attempted_at) >= RETRY_INTERVAL


class HoldNotifier:
    """Delivery-side gate; without approved channels and a client nothing is sent (D-997 default).

    One attempt is: a short transaction marking SENDING, the network call with no
    SQLite lock held, then a short transaction recording the outcome. The delivery
    loop calls settle_interrupted() once at start to close rows an interrupted
    send left in SENDING; reads and test helpers never write.
    """

    def __init__(self, ledger, *, channels=(), client=None):
        self.ledger, self.channels, self.client = ledger, tuple(channels), client
        for channel in self.channels:
            identifier(channel)
        if len(set(self.channels)) != len(self.channels):
            raise ValueError("Duplicate receiving channel")
        if self.client is not None and not self.channels:
            raise ValueError("A delivery client needs at least one approved receiving channel")
        self._settled = False

    def settle_interrupted(self):
        """Call once when the delivery loop starts: a leftover SENDING row means the send's
        outcome is unknown (interrupted process). Reads and test helpers never settle."""
        with closing(self.ledger._connect()) as c, c:
            c.execute("BEGIN IMMEDIATE")
            c.execute("UPDATE hold_delivery SET state='UNKNOWN',note=? WHERE state='SENDING'",
                      ("Interrupted send left the submission unclear",))
        self._settled = True

    def interrupted(self):
        """In-flight rows another start left behind; visible only, never auto-resent."""
        with closing(self.ledger._connect()) as c:
            return c.execute("SELECT COUNT(*) FROM hold_delivery WHERE state='SENDING'").fetchone()[0]

    def outbox(self):
        with closing(self.ledger._connect()) as c:
            return [dict(row) for row in c.execute(
                "SELECT * FROM hold_outbox ORDER BY occurred_at, occurrence_id")]

    def deliveries(self):
        with closing(self.ledger._connect()) as c:
            return [dict(row) for row in c.execute(
                "SELECT * FROM hold_delivery ORDER BY occurrence_id, channel")]

    def deliver_pending(self, *, now=None):
        """Send each waiting (occurrence, channel) once; unclear submissions never auto-resend."""
        now = datetime.now(timezone.utc) if now is None else now
        aware_time(now)
        if not self.channels or self.client is None:
            return []  # External sending is off by default; the local HOLD and outbox stay untouched.
        if not self._settled:
            # Forgetting the one-time settle would leave interrupted sends pending forever.
            raise RuntimeError("Call settle_interrupted() once before the first delivery pass")
        sent = []
        for occurrence in self._waiting(now):
            for channel in self.channels:
                if self._attempt(occurrence, channel, now):
                    sent.append(occurrence)
        return sent

    def _waiting(self, now):
        """Occurrences with at least one configured channel that has no row yet or a
        retry-ready PENDING row; a final row on one channel never hides another
        channel's missing attempt."""
        with closing(self.ledger._connect()) as c:
            states = {(r[0], r[1]): (r[2], r[3]) for r in c.execute(
                "SELECT occurrence_id, channel, state, attempted_at FROM hold_delivery")}
            waiting = []
            for row in c.execute("SELECT occurrence_id FROM hold_outbox "
                                 "ORDER BY occurred_at, occurrence_id"):
                for channel in self.channels:
                    state, attempted_at = states.get((row[0], channel), (None, None))
                    if state is None or (state == 'PENDING' and _retry_ready(attempted_at, now)):
                        waiting.append(row[0])
                        break
            return waiting

    def _attempt(self, occurrence, channel, now):
        """Run one attempt; True only when this call itself completed a send."""
        with closing(self.ledger._connect()) as c, c:
            c.execute("BEGIN IMMEDIATE")
            settled = c.execute("SELECT state,attempted_at FROM hold_delivery "
                                "WHERE occurrence_id=? AND channel=?", (occurrence, channel)).fetchone()
            if settled is not None and settled["state"] != "PENDING":
                return False  # SENT/UNKNOWN/REJECTED are final and stay untouched.
            if settled is not None and not _retry_ready(settled["attempted_at"], now):
                return False  # Retry spacing: leave the PENDING row for a later pass.
            try:
                pre_send_check(c, occurrence, channel=channel, channels=self.channels, now=now)
            except ValueError as reason:
                # The row is absent or PENDING here, so the settle cannot overwrite a final state.
                c.execute("INSERT INTO hold_delivery VALUES(?,?,?,?,?) ON CONFLICT(occurrence_id,channel) "
                          "DO UPDATE SET state=excluded.state,note=excluded.note",
                          (occurrence, channel, "REJECTED", str(reason), now.isoformat()))
                return False
            c.execute("INSERT INTO hold_delivery VALUES(?,?,?,?,?) ON CONFLICT(occurrence_id,channel) "
                      "DO UPDATE SET state='SENDING',note=NULL,attempted_at=excluded.attempted_at",
                      (occurrence, channel, "SENDING", None, now.isoformat()))
            body = json.loads(c.execute("SELECT payload FROM hold_outbox WHERE occurrence_id=?",
                                        (occurrence,)).fetchone()[0])
        try:
            self.client(channel, body)  # Outside any SQLite transaction: no write lock while sending.
        except DefiniteDeliveryFailure as failure:
            state, note = "PENDING", str(failure) or "definite delivery failure"
        except BaseException as failure:  # Timeouts and unclassified errors are unclear, never resent.
            state, note = "UNKNOWN", f"{type(failure).__name__}: {failure}"[:200]
        else:
            state, note = "SENT", None
        with closing(self.ledger._connect()) as c, c:
            moved = c.execute("UPDATE hold_delivery SET state=?,note=? WHERE occurrence_id=? AND channel=? "
                              "AND state='SENDING'", (state, note, occurrence, channel)).rowcount
        # With moved==0 another notifier start settled the in-flight row; whatever it stored,
        # this call did not complete its own recorded send.
        return bool(moved and state == "SENT")
