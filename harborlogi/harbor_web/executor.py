"""Standing business executor: durable multi-step progression without views (design 24/27 P1).

The loop advances confirmed commands through the BusinessLedger stages: it submits the
next bound step, reconciles in-flight stages through the original request key (reusing
the operator reconcile operation), resumes stages held only by bookkeeping uncertainty,
and confirms/finishes goal-confirmed stages with evidence from a configured provider.
It never submits a new request key for an uncertain outcome, never admits or
self-approves, and leaves CANCEL_PENDING commands and operator decisions untouched.
Web requests stay read-only toward progression.

Production P1 runs without an evidence provider: stages reach goal-confirmed WAITING
and stay there until P2 connects authenticated sources. Start is explicit: set
HARBOR_EXECUTOR_ENABLED=1 in the serving process. When dev autoreload spawns a second
loop, the SQLite writer lease leaves the loser idling harmlessly.
"""
import json
import math
import os
import threading

from django.db import close_old_connections
from django.utils import timezone

from harbor.process import ArmTransferBinding
from harbor.transport import TransportLedger, TripIntent
from harbor_web import commands
from harbor_web.models import DepartureIntent
from harbor_web.trip_proposals import FRESH, destination, trip_key
from harbor_web.coordination import queue_lock

# DISPATCHING survives only a crash between the state save and the first proposal;
# RECONCILED is the steady progression state; UNKNOWN is reconciled by the original key.
ACTIVE_STATES = ('DISPATCHING', 'RECONCILED', 'UNKNOWN')
PROPOSE_STATES = ('DISPATCHING', 'RECONCILED')
MAX_BACKOFF_SECONDS = 300.0

_stop = threading.Event()
_thread = None
_start_lock = threading.Lock()
_failures = {}
_failures_guard = threading.Lock()


def _setting(name, default):
    try:
        value = float(os.environ.get(name, default))
    except ValueError:
        return default
    return value if math.isfinite(value) and value > 0 else default


def _eligible(command_id, now):
    with _failures_guard:
        _, until = _failures.get(command_id, (0, None))
        return until is None or now >= until


def _record(command_id, failed, interval):
    with _failures_guard:
        count, _ = _failures.get(command_id, (0, None))
        if not failed:
            if count:
                _failures.pop(command_id, None)
            return
        delay = min(interval * (2 ** count), MAX_BACKOFF_SECONDS)
        _failures[command_id] = (count + 1, timezone.now() + timezone.timedelta(seconds=delay))


def _propose_next(command, registry, key):
    """Submit the next bound stage: the durable step the user's confirmation commissioned."""
    with queue_lock, registry.lock(command.pk):
        command.refresh_from_db()
        if command.state not in PROPOSE_STATES:
            return command.state
        mission = registry.ledger.propose(key, registry.client)
        row = registry.ledger.read(key)
        request_key = f"{key}:{row['step_index']}"
        binding = ArmTransferBinding(**command.binding['moves'][row['step_index']])
        job = binding.check(registry.client.arm_request(request_key)['job'], request_key)
        if job.get('proposal_principal_id') != command.fleet_principal:
            raise ValueError('Principal mismatch')
        command.mission_id = mission
        commands._record_fact(command, job, None)
        return commands._save(command, command.actor, 'RECONCILED')


def _resume(command, registry, key, *, now):
    """Re-enter WAITING from a bookkeeping HOLD once the original request key reconciles."""
    with queue_lock, registry.lock(command.pk):
        command.refresh_from_db()
        if command.state != 'RECONCILED':
            return command.state
        job = registry.ledger.resume_arm(key, registry.client, now=now)
        if job.get('proposal_principal_id') != command.fleet_principal:
            raise ValueError('Principal mismatch')
        command.mission_id = job['mission_id']
        commands._record_fact(command, job, None)
        return commands._save(command, command.actor, 'RECONCILED')


def _advance(command, registry, *, now, evidence):
    """Progress one command by at most one business stage; keep every original identity."""
    from harbor_web.runtime import execution_block_reason
    key = commands._key(command)
    command.refresh_from_db()
    if command.state not in ACTIVE_STATES or execution_block_reason():
        return command.state
    try:
        commands._validate(command, registry.config)
        commands._prepare_ledger(command, registry)
        row = registry.ledger.read(key)
        payload = json.loads(row['payload'])
        if payload.get('execution_kind') != 'ARM_TRANSFER':
            return command.state  # Legacy cell stages stay with human reconciliation.
        if row['state'] == 'COMPLETED':
            return commands._save(command, command.actor, 'COMPLETED')
        if row['state'] == 'PLANNED':
            if command.state not in PROPOSE_STATES:
                return command.state  # An uncertain prior stage must reconcile first.
            return _propose_next(command, registry, key)
        if row['state'] in ('WAITING', 'HOLD'):
            command = commands._operation(command.actor, command.pk, command.version, 'reconcile')
            row = registry.ledger.read(key)
            if row['state'] == 'HOLD' and command.state == 'RECONCILED':
                command = _resume(command, registry, key, now=now)
                row = registry.ledger.read(key)
        if row['state'] == 'WAITING' and row['goal_confirmed'] and evidence is not None:
            step = evidence(command, row, payload, 'step')
            if step is not None:
                # A fresh clock at the confirmation site: refresh stamps reconciliation
                # with its own now, and confirmation must not run before it.
                with queue_lock, registry.lock(command.pk):
                    registry.ledger.confirm(key, step, now=timezone.now())
                row = registry.ledger.read(key)
        if row['state'] == 'AWAITING_FINAL_EVIDENCE' and evidence is not None:
            final = evidence(command, row, payload, 'final')
            if final is not None:
                with queue_lock, registry.lock(command.pk):
                    registry.ledger.finish(key, final, now=timezone.now())
                return commands._save(command, command.actor, 'COMPLETED')
        return command.state
    except Exception:
        # Uncertain outcome: hold the stage, keep the original request key, reconcile later.
        try:
            registry.ledger.hold(key)
        except Exception:
            pass
        command.refresh_from_db()
        if command.state in ACTIVE_STATES:
            return commands._save(command, command.actor, 'UNKNOWN', 'RECONCILIATION_REQUIRED')
        return command.state


def _advance_trips(registry, *, now, interval):
    """D-908: one neutral trip proposal per fresh departure intent, then reconcile by the original key.

    Never admits (the client has no admit call), never mints a second key, holds on any uncertainty.
    """
    place = destination()
    if not place:
        return {}
    ledger = getattr(registry, 'transport', None)
    if ledger is None:
        ledger = registry.transport = TransportLedger(registry.config['path'])
    results, seen = {}, set()

    def run(key, action):
        seen.add(key)
        if not _eligible('trip:' + key, now):
            results[key] = 'BACKOFF'
            return
        try:
            row = action()
            results[key] = row['state']
            _record('trip:' + key, False, interval)
        except Exception as error:
            results[key] = f'HOLD: {type(error).__name__}'
            _record('trip:' + key, True, interval)

    fresh = DepartureIntent.objects.filter(confirmation__observed_at__gte=now - FRESH,
                                           confirmation__observed_at__lte=now).order_by('pk')
    for departure in fresh:
        key = trip_key(departure)
        row = ledger.read_proposal(key)
        if row is None or row['state'] == 'PLANNED':
            def send(key=key, departure=departure):
                ledger.prepare_proposal(TripIntent(key, (departure.vehicle_id,), place))
                return ledger.propose(key, registry.client)
            run(key, send)
    for key in ledger.open_proposal_keys():
        if key not in seen and ledger.read_proposal(key)['state'] != 'PLANNED':
            run(key, lambda key=key: ledger.reconcile_proposal(key, registry.client))
    return results


def tick(*, evidence=None, now=None, registry=None):
    """One bounded pass over active commands. Returns a per-command outcome summary."""
    from harbor_web.runtime import execution_block_reason
    blocked = execution_block_reason()
    if blocked:
        return dict(ran=False, reason=blocked, results={})
    now = now or timezone.now()
    if registry is None:
        try:
            registry = commands.acquire_registry()
        except Exception:
            return dict(ran=False, reason='EXECUTION_CONFIGURATION_REQUIRED', results={})
    batch = max(1, int(_setting('HARBOR_EXECUTOR_BATCH', 4)))
    interval = _setting('HARBOR_EXECUTOR_INTERVAL', 5.0)
    results = {}
    active = list(commands.Command.objects.filter(state__in=ACTIVE_STATES).order_by('pk')[:batch])
    for command in active:
        if not _eligible(command.pk, now):
            results[command.pk] = 'BACKOFF'
            continue
        try:
            results[command.pk] = _advance(command, registry, now=now, evidence=evidence)
            _record(command.pk, False, interval)
        except Exception as error:
            results[command.pk] = f'TICK_ERROR: {type(error).__name__}'
            _record(command.pk, True, interval)
    try:
        results.update({'trip:' + key: value for key, value in _advance_trips(registry, now=now, interval=interval).items()})
    except Exception as error:
        results['trip'] = f'TICK_ERROR: {type(error).__name__}'
    return dict(ran=True, reason='', results=results)


def _loop_evidence():
    """The production provider (P2): intake-backed when the deployment opts in."""
    if os.environ.get('HARBOR_EXECUTOR_EVIDENCE') != 'intake':
        return None
    try:
        from harbor_web.evidence import intake_provider
        return intake_provider()
    except Exception:
        return None


def _run(interval):
    while not _stop.wait(interval):
        try:
            close_old_connections()
            tick(evidence=_loop_evidence())
        except Exception:
            pass  # Never die; the next interval retries.


def start():
    """Start the single background loop (explicit opt-in, idempotent)."""
    global _thread
    with _start_lock:
        if _thread is not None and _thread.is_alive():
            return _thread
        interval = _setting('HARBOR_EXECUTOR_INTERVAL', 5.0)
        _stop.clear()
        _thread = threading.Thread(target=_run, args=(interval,), name='harbor-executor', daemon=True)
        _thread.start()
        return _thread


def stop():
    _stop.set()
