"""Standing business executor: durable multi-step progression without views (design 24/27 P1).

The loop selects active commands and invokes commands.advance_command. The command
service owns submission, reconciliation and business evidence transitions; this module
owns scheduling, bounded passes and backoff.
It never submits a new request key for an uncertain outcome, never admits or
self-approves, and leaves CANCEL_PENDING commands and operator decisions untouched.
Web requests stay read-only toward progression.

Production P1 runs without an evidence provider: stages reach goal-confirmed WAITING
and stay there until P2 connects authenticated sources. Start is explicit: set
HARBOR_EXECUTOR_ENABLED=1 in the serving process. When dev autoreload spawns a second
loop, the SQLite writer lease leaves the loser idling harmlessly.
"""
import math
import os
import threading

from django.db import close_old_connections
from django.utils import timezone

from harbor_web import commands

# DISPATCHING survives only a crash between the state save and the first proposal;
# RECONCILED is the steady progression state; UNKNOWN is reconciled by the original key.
ACTIVE_STATES = ('DISPATCHING', 'RECONCILED', 'UNKNOWN')
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


def _rearrange_when_idle(now):
    """Design 32 R2: the idle executor asks whether a standing authorization allows a rearrangement."""
    from harbor_web import rearrangement
    try:
        rearrangement.auto_check(now)
    except Exception:
        pass  # A failed check leaves no Command; the next idle tick retries.


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
            results[command.pk] = commands.advance_command(command, registry, evidence=evidence)
            _record(command.pk, False, interval)
        except Exception as error:
            results[command.pk] = f'TICK_ERROR: {type(error).__name__}'
            _record(command.pk, True, interval)
    if not active:
        _rearrange_when_idle(now)
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
