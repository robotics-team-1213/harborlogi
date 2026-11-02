"""Durable web intent and a single leased ledger writer. No physical proof producer."""
import atexit
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import sqlite3
import threading
from urllib.parse import urlsplit

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from harbor.ownership import WriterLease
from harbor.ledger import ReadOnlyLedger, plan_payload
from harbor.process import (ArmTransferBinding, SimulationCellTemplate, SpatialPlacementBinding, anchor_address,
                            bind_plan, load_bindings)
from harbor.outbound import CargoMove, OutboundProposal
from harbor.rosy_http import RosyClient
from harbor.execution import read_arm_execution, validate_arm_execution, cancel_execution
from harbor.workflow import BusinessLedger
from harbor_web.command_models import ACTIVE_COMMAND_STATES
from harbor_web.models import Plan, Command, CommandFact, Audit, Observation
from harbor_web.services import _role, Conflict, queue_digest, constraints
from harbor_web.coordination import serialized

_registry = None
_guard = threading.RLock()


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _config():
    from harbor_web.fleet_connection import connection_settings
    from harbor_web.evidence import configured_sources
    connection = connection_settings()
    if connection['origin'] and not connection['allowed']:
        raise ValidationError('ORIGIN_NOT_ALLOWED')
    values = [os.environ.get('HARBOR_LEDGER_PATH', ''), connection['origin'],
        os.environ.get('HARBOR_FLEET_TOKEN', ''), connection['principal'], os.environ.get('HARBOR_BINDINGS_PATH', '')]
    if not all(values):
        raise ValidationError('EXECUTION_CONFIGURATION_REQUIRED')
    path, origin, token, principal, bindings = values
    RosyClient(origin, token)
    if len(principal) > 192 or not principal.strip() or any(ord(c) < 32 for c in principal):
        raise ValidationError('INVALID_FLEET_PRINCIPAL')
    evidence_sources = configured_sources()  # Raises on malformed or overlapping sets.
    return dict(path=Path(path).resolve(), origin=origin.rstrip('/'), token=token,
                principal=principal, bindings=bindings, evidence_sources=evidence_sources)


def _identity(config):
    return hashlib.sha256(str(config['path']).casefold().encode()).hexdigest()


def _bindings(config):
    with open(config['bindings'], encoding='utf-8') as source:
        return load_bindings(json.load(source))


def _matching_spatial(plan, installed):
    """The one installed spatial binding that authorizes exactly this placement, or None.

    Design 29: the recommendation tuple, footprint revision, and cargo must all
    match a single installed binding; anything else keeps the execution gate.
    """
    recommendation = plan.result.get('recommendation')
    if not isinstance(recommendation, dict) or plan.result.get('status') != 'READY':
        return None
    matches = [binding for binding in installed
               if isinstance(binding, SpatialPlacementBinding)
               and binding.matches(recommendation, plan.result.get('footprint_revision'), plan.order.cargo.code)]
    return matches[0] if len(matches) == 1 else None


def _rearrangement(plan, installed):
    """Temporary simulation binding of an R2 plan: (moves, completing move), or None.

    The ledger completes on one cargo at its final place, so one cargo must move
    exactly once; its last destination stands in for the outbound stand.
    """
    templates = [b for b in installed if isinstance(b, SimulationCellTemplate)]
    if len(templates) != 1 or plan.result.get('status') != 'READY' or not plan.result.get('moves'):
        return None
    moves = templates[0].moves(plan.result['moves'])
    once = [m for m in moves if sum(o.cargo_id == m.cargo_id for o in moves) == 1]
    return (moves, once[-1]) if once else None


def _spatial_from_path(plan, path):
    if not path:
        return None
    try:
        with open(path, encoding='utf-8') as source:
            installed = load_bindings(json.load(source))
        if plan.result.get('plan_kind') == 'rearrangement':
            return _rearrangement(plan, installed)
        return _matching_spatial(plan, installed)
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _spatial_binding(plan, config):
    return _spatial_from_path(plan, config['bindings'])


def _bound(plan, config):
    bindings = _bindings(config)
    if plan.result.get('plan_kind') == 'rearrangement':
        bound = _rearrangement(plan, bindings)
        if bound is None:
            raise ValueError('No simulation cell template binds this rearrangement')
        return bound[0]
    if plan.result.get('model') == 'grid-placement-v1':
        spatial = _matching_spatial(plan, bindings)
        if spatial is None:
            raise ValueError('No installed spatial binding matches this placement')
        return (spatial.move(),)
    proposal = OutboundProposal(plan.result['status'], plan.result['reason'], plan.result['source_revision'],
                               tuple(CargoMove(**move) for move in plan.result['moves']))
    return bind_plan(proposal, tuple(b for b in bindings
                                     if not isinstance(b, (SpatialPlacementBinding, SimulationCellTemplate))))


@serialized
def capability(plan):
    reason, origin = '', None
    try:
        if Command.objects.filter(order_id=plan.order_id, state__in=ACTIVE_COMMAND_STATES).exclude(plan=plan).exists():
            raise ValidationError('EXISTING_ORDER_EXECUTION_REQUIRES_RECONCILIATION')
        from harbor_web.runtime import execution_block_reason
        blocked = execution_block_reason()
        if blocked:
            raise ValidationError(blocked)
        if plan.result.get('model') == 'grid-placement-v1':
            # Answerable from the installed bindings alone, before execution configuration.
            if _spatial_from_path(plan, os.environ.get('HARBOR_BINDINGS_PATH', '')) is None:
                raise ValidationError('SPATIAL_EXECUTION_BINDINGS_REQUIRED')
        else:
            from harbor_web.models import CargoFootprint
            source_codes = {plan.order.cargo.code}
            for bay in plan.observation.payload.get('bays', []):
                source_codes.update(bay['cargo'])
            source_codes.update(h['cargo_id'] for h in plan.observation.payload.get('holdings', []))
            if CargoFootprint.objects.filter(cargo__code__in=source_codes, length_cells__gt=1).exists():
                raise ValidationError('SPATIAL_OBSERVATION_REQUIRED')
        config = _config()
        origin = config['origin']
        if plan.result.get('status') != 'READY':
            raise ValidationError('PLAN_NOT_READY')
        if plan.order.version != plan.order_version or plan.queue_digest != queue_digest():
            raise ValidationError('PLAN_QUEUE_CHANGED')
        if Observation.objects.filter(source_id=plan.observation.source_id, pk__gt=plan.observation_id).exists():
            raise ValidationError('OBSERVATION_REVISION_CHANGED')
        age = timezone.now() - plan.observation.observed_at
        if not timezone.timedelta(0) <= age <= constraints(plan.condition.payload).max_observation_age:
            raise ValidationError('OBSERVATION_NOT_FRESH')
        host = urlsplit(origin).hostname
        try:
            loopback = host == 'localhost' or ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = False
        if not (settings.MODE in {'development', 'test'} and os.environ.get('HARBOR_WEB_ALLOW_SIMULATION') == '1'
                and loopback and plan.observation.provenance == 'SYNTHETIC'):
            raise ValidationError('AUTHENTICATED_OBSERVATION_AND_EVIDENCE_PRODUCER_REQUIRED')
        _bound(plan, config)
    except ValidationError as error:
        safe = {'SPATIAL_EXECUTION_BINDINGS_REQUIRED', 'SPATIAL_OBSERVATION_REQUIRED',
                'EXISTING_ORDER_EXECUTION_REQUIRES_RECONCILIATION', 'RESTORE_RECONCILIATION_REQUIRED', 'DEPLOYMENT_PROFILE_REQUIRED',
                'EXECUTION_CONFIGURATION_REQUIRED', 'ORIGIN_NOT_ALLOWED', 'INVALID_FLEET_PRINCIPAL', 'PLAN_NOT_READY', 'PLAN_QUEUE_CHANGED',
                'OBSERVATION_NOT_FRESH', 'OBSERVATION_REVISION_CHANGED', 'AUTHENTICATED_OBSERVATION_AND_EVIDENCE_PRODUCER_REQUIRED'}
        reason = error.messages[0] if error.messages and error.messages[0] in safe else 'EXECUTION_INPUTS_UNAVAILABLE'
    except (ValueError, KeyError, TypeError, OSError):
        reason = 'EXECUTION_INPUTS_UNAVAILABLE'
    return dict(enabled=not reason, reason=reason, fleet_url=origin,
                requirements=['Fresh authenticated observation', 'Verified installed bindings', 'Independent physical evidence'])


class Registry:
    def __init__(self, config):
        self.config = config
        self.lease = WriterLease(config['path'])
        self.lease.__enter__()
        try:
            from harbor_web.evidence import ledger_sources
            # Unconfigured deployments keep placeholder sources that cannot authorize
            # anything: no real submission ever carries them.
            placement_sources, retreat_sources = ledger_sources()
            self.ledger = BusinessLedger(config['path'], placement_sources=placement_sources,
                                         retreat_sources=retreat_sources)
            self.client = RosyClient(config['origin'], config['token'])
            self.client.expected_principal = config['principal']
            self.client.expected_role = 'service'
            self.locks = {}
        except BaseException:
            self.lease.__exit__()
            raise

    def lock(self, key):
        with _guard:
            return self.locks.setdefault(str(key), threading.RLock())


def acquire_registry():
    global _registry
    with _guard:
        config = _config()
        if _registry is None:
            _registry = Registry(config)
        if _registry.config != config:
            raise Conflict('Execution identity changed; restart the owner.')
        try:
            _repair_pending_cancellations(_registry)
        except BaseException:
            _registry.lease.__exit__()
            _registry = None
            raise
        return _registry


def registry_close():
    global _registry
    with _guard:
        if _registry:
            _registry.lease.__exit__()
            _registry = None


atexit.register(registry_close)


def _key(command):
    return 'harbor:' + str(command.key)


def _validate(command, config):
    from harbor_web.runtime import ledger_identity_matches
    if (command.order_id != command.plan.order_id or not ledger_identity_matches(command.ledger_identity, _identity(config)) or command.fleet_origin != config['origin']
            or command.fleet_principal != config['principal'] or command.body_digest != _digest(command.binding)):
        raise Conflict('Saved command identity differs from configured execution owner.')


@serialized
def prepare_command(actor, *, plan_id, expected_version):
    _role(actor, 'operator')
    registry = acquire_registry()
    with registry.lock('plan:' + str(plan_id)):
        plan = Plan.objects.select_related('order', 'order__cargo', 'observation', 'condition').get(pk=plan_id)
        # Design 28: only an ACTIVE command occupies the order; terminal commands are history.
        existing = Command.objects.filter(order=plan.order, state__in=ACTIVE_COMMAND_STATES).first()
        if existing:
            _validate(existing, registry.config)
            if existing.plan_id != plan.pk:
                raise Conflict('Existing order execution requires reconciliation.')
            if existing.plan_version != expected_version:
                raise Conflict('Plan version changed.')
            return existing
        if plan.order_version != expected_version or plan.order.version != expected_version:
            raise Conflict('Plan version changed.')
        if not capability(plan)['enabled']:
            Audit.objects.create(actor=actor, action='command.blocked', object_type='Plan', object_id=str(plan.pk))
            raise ValidationError('Execution inputs unavailable.')
        if plan.result.get('plan_kind') == 'rearrangement':
            moves, completing = _rearrangement(plan, _bindings(registry.config))
            body = dict(moves=[m.__dict__ for m in moves], revision=plan.result['source_revision'],
                        outbound_id=completing.cargo_id, outbound_stand=completing.destination,
                        move_arms=[[m.yard_arm] for m in moves])
        elif plan.result.get('model') == 'grid-placement-v1':
            spatial = _spatial_binding(plan, registry.config)
            body = dict(moves=[spatial.move().__dict__], revision=plan.result['source_revision'],
                        outbound_id=plan.order.cargo.code, outbound_stand=spatial.anchor(),
                        move_arms=[[spatial.yard_arm]])
        else:
            bindings = _bound(plan, registry.config)
            body = dict(moves=[b.__dict__ for b in bindings], revision=plan.result['source_revision'],
                        outbound_id=plan.order.cargo.code,
                        outbound_stand=constraints(plan.condition.payload).outbound_stand,
                        move_arms=[[m['primary_arm'], *m.get('alternate_arms', [])] for m in plan.result['moves']])
        with transaction.atomic():
            command = Command.objects.create(plan=plan, order=plan.order, actor=actor, plan_version=expected_version,
                ledger_identity=_identity(registry.config), fleet_origin=registry.config['origin'],
                fleet_principal=registry.config['principal'], binding=body, body_digest=_digest(body),
                supersedes=Command.objects.filter(order=plan.order).order_by('-pk').first())
            Audit.objects.create(actor=actor, action='command.prepare', object_type='Command', object_id=str(command.pk))
    return command


def _prepare_ledger(command, registry):
    body = command.binding
    registry.ledger.prepare(_key(command), body['revision'], tuple(ArmTransferBinding(**m) for m in body['moves']),
        outbound_id=body['outbound_id'], outbound_stand=body['outbound_stand'],
        move_arms=tuple(tuple(a) for a in body['move_arms']))
    row = registry.ledger.read(_key(command))
    _check_ledger_binding(command, row)


def _check_ledger_binding(command, row):
    expected = dict(command.binding, execution_kind='ARM_TRANSFER')
    if plan_payload(row) != expected or row['digest'] != _digest(expected):
        raise Conflict('Ledger binding differs from saved command.')


def _repair_pending_cancellations(registry):
    for command in Command.objects.filter(state='CANCEL_PENDING'):
        with registry.lock(command.pk):
            command.refresh_from_db()
            if command.state != 'CANCEL_PENDING':
                continue
            _validate(command, registry.config)
            _prepare_ledger(command, registry)
            if registry.ledger.read(_key(command))['state'] != 'HOLD':
                registry.ledger.hold(_key(command))


def _save(command, actor, state, reason=''):
    command.state, command.reason = state, reason
    command.version += 1
    with transaction.atomic():
        command.save(update_fields=['state', 'reason', 'version', 'mission_id', 'updated_at'])
        Audit.objects.create(actor=actor, action='command.' + state.lower(), object_type='Command', object_id=str(command.pk),
                             detail={'reason': reason, 'version': command.version})
    return command


def _record_fact(command, job, receipt):
    job_fields = ('action_kind', 'request_key', 'transfer', 'request_digest', 'mission_id', 'status',
                  'proposal_principal_id', 'resource_policy_digest', 'authority_epoch', 'dispatch_generation')
    receipt_fields = ('mission_id', 'workcell_id', 'instance_id', 'object_ref', 'source', 'target', 'policy_digest',
        'authority_epoch', 'dispatch_generation', 'physical_status', 'validated_at_ns', 'request_digest',
        'evidence_digest', 'receipt_id', 'action_id', 'attempt_id')
    CommandFact.objects.create(created_by=command.actor, command=command, command_key=command.key, body_digest=command.body_digest,
        fleet_origin=command.fleet_origin, fleet_principal=command.fleet_principal,
        job={k: job[k] for k in job_fields if k in job},
        receipt={k: receipt[k] for k in receipt_fields if k in receipt} if receipt else None, read_at=timezone.now())


@serialized
def _operation(actor, command_id, expected_version, action, reason='', *, resume=False):
    registry = acquire_registry()
    with registry.lock(command_id):
        _role(actor, 'operator')
        command = Command.objects.select_related('plan__order', 'plan__observation', 'plan__condition').get(pk=command_id)
        if command.version != expected_version:
            raise Conflict('Command changed; reload.')
        restore_read = (command.state == 'BLOCKED' and action == 'reconcile'
                        and command.reason == 'RESTORE_RECONCILIATION_REQUIRED')
        if command.state not in ACTIVE_COMMAND_STATES and not restore_read:
            raise Conflict('Closed commands are history; prepare a new command.')
        _validate(command, registry.config)
        key = _key(command)
        if restore_read:
            try:
                row = registry.ledger.read(key)
                _check_ledger_binding(command, row)
                binding = ArmTransferBinding(**command.binding['moves'][row['step_index']])
                execution = read_arm_execution(registry.client, binding, f"{key}:{row['step_index']}",
                    mission_id=row['mission_id'], principal=command.fleet_principal)
            except (ValueError, KeyError, IndexError, TypeError, OSError, sqlite3.Error) as error:
                raise Conflict('Restored execution could not be reconciled.') from error
            _record_fact(command, execution.job, execution.receipt)
            return command
        if action == 'submit':
            if command.state != 'PREPARED':
                raise Conflict('Explicit reconciliation required; submission cannot be retried.')
            gate = capability(command.plan)
            if not gate['enabled']:
                return _save(command, actor, 'BLOCKED', gate['reason'])
            current_bindings = [binding.__dict__ for binding in _bound(command.plan, registry.config)]
            if current_bindings != command.binding['moves']:
                return _save(command, actor, 'BLOCKED', 'INSTALLED_BINDINGS_CHANGED')
            _prepare_ledger(command, registry)
            _save(command, actor, 'DISPATCHING')
            try:
                return _submit_stage(command, registry, actor)
            except Exception:
                registry.ledger.hold(key)
                return _save(command, actor, 'UNKNOWN', 'RECONCILIATION_REQUIRED')
        _prepare_ledger(command, registry)
        if action == 'cancel':
            if not isinstance(reason, str) or not reason.strip() or len(reason) > 192 or any(ord(c) < 32 for c in reason):
                raise ValidationError('A short cancellation reason is required.')
            _save(command, actor, 'CANCEL_PENDING', 'CANCELLATION_REQUESTED')
            registry.ledger.hold(key)
            if command.mission_id:
                try:
                    cancel_execution(registry.client, command.mission_id, reason)
                except Exception:
                    return _save(command, actor, 'CANCEL_PENDING', 'CANCELLATION_OUTCOME_UNKNOWN')
            return command
        try:
            row = registry.ledger.read(key)
            can_resume = resume and command.state in ('DISPATCHING', 'RECONCILED', 'UNKNOWN')
            execution = registry.ledger.reconcile_arm(key, registry.client, now=timezone.now(),
                resume=can_resume, principal=command.fleet_principal)
            command.mission_id = execution.mission_id
            _record_fact(command, execution.job, execution.receipt)
            state = 'CANCEL_PENDING' if command.state == 'CANCEL_PENDING' else 'RECONCILED'
            held = row['state'] == 'HOLD' and (not can_resume or execution.held)
            return _save(command, actor, state, 'BUSINESS_HOLD' if held else '')
        except Exception:
            registry.ledger.hold(key)
            return _save(command, actor, 'CANCEL_PENDING' if command.state == 'CANCEL_PENDING' else 'UNKNOWN', 'RECONCILIATION_REQUIRED')


def submit_command(actor, *, command_id, expected_version):
    return _operation(actor, command_id, expected_version, 'submit')


def reconcile_command(actor, *, command_id, expected_version, resume=False):
    return _operation(actor, command_id, expected_version, 'reconcile', resume=resume)


def cancel_command(actor, *, command_id, expected_version, reason):
    return _operation(actor, command_id, expected_version, 'cancel', reason)


@serialized
def close_cancelled(actor, *, command_id, expected_version):
    """Close a pending cancellation as CANCELLED after the operator confirmed the stop.

    The Fleet ARM contract has no cancelled terminal status and cancel/HOLD retain
    occupancy (API reference v1.312), so this transition is a human decision recorded
    with its actor — never an automatic readback. Design 28.
    """
    registry = acquire_registry()
    with registry.lock(command_id):
        _role(actor, 'operator')
        command = Command.objects.get(pk=command_id)
        if command.version != expected_version:
            raise Conflict('Command changed; reload.')
        _validate(command, registry.config)
        if command.state != 'CANCEL_PENDING':
            raise Conflict('Only a pending cancellation can be closed.')
        registry.ledger.hold(_key(command))
        return _save(command, actor, 'CANCELLED', 'CANCELLATION_CONFIRMED_BY_OPERATOR')


def command_detail(command):
    try:
        config = _config()
        _validate(command, config)
        try:
            row = ReadOnlyLedger(config['path']).read(_key(command))
            _check_ledger_binding(command, row)
        except KeyError:
            row = None
        fact = CommandFact.objects.filter(command=command, command_key=command.key, body_digest=command.body_digest,
            fleet_origin=command.fleet_origin, fleet_principal=command.fleet_principal).order_by('-read_at', '-pk').first()
        job, receipt = None, None
        if fact and row and row['mission_id']:
            binding = ArmTransferBinding(**command.binding['moves'][row['step_index']])
            execution = validate_arm_execution(binding, f"{_key(command)}:{row['step_index']}",
                fact.job, fact.receipt, mission_id=row['mission_id'], principal=command.fleet_principal)
            job, receipt = execution.job, execution.receipt
        summary = {field: row[field] for field in ('state', 'step_index', 'row_version', 'mission_id', 'goal_confirmed')} if row else None
        return dict(ledger=summary, job=job, receipt=receipt, reason=command.reason,
                    read_at=fact.read_at if job else None)
    except (ValidationError, Conflict, ValueError, sqlite3.Error):
        return dict(ledger=None, job=None, receipt=None, reason='LEDGER_UNAVAILABLE', read_at=None)


def _submit_stage(command, registry, actor):
    execution = registry.ledger.submit(_key(command), registry.client, principal=command.fleet_principal)
    command.mission_id = execution.mission_id
    _record_fact(command, execution.job, execution.receipt)
    return _save(command, actor, 'RECONCILED')


PROPOSE_STATES = ('DISPATCHING', 'RECONCILED')
EXECUTING_STATES = ('DISPATCHING', 'RECONCILED', 'UNKNOWN')


@serialized
def advance_command(command, registry, *, evidence=None):
    """Progress one command by at most one business stage; keep every original identity."""
    with registry.lock(command.pk):
        from harbor_web.runtime import execution_block_reason
        key = _key(command)
        command.refresh_from_db()
        if command.state not in EXECUTING_STATES or execution_block_reason():
            return command.state
        try:
            _validate(command, registry.config)
            _prepare_ledger(command, registry)
            row = registry.ledger.read(key)
            payload = plan_payload(row)
            if payload.get('execution_kind') != 'ARM_TRANSFER':
                return command.state  # Legacy cell stages stay with human reconciliation.
            if row['state'] == 'COMPLETED':
                return _save(command, command.actor, 'COMPLETED').state
            if row['state'] == 'PLANNED':
                if command.state not in PROPOSE_STATES:
                    return command.state  # An uncertain prior stage must reconcile first.
                if row['step_index'] and command.order.kind == 'REARRANGE':
                    from harbor_web.rearrangement import outbound_pending
                    if outbound_pending():
                        return command.state  # Outbound first (design 32): no further R2 move starts.
                return _submit_stage(command, registry, command.actor).state
            if row['state'] in ('WAITING', 'HOLD'):
                command = reconcile_command(command.actor, command_id=command.pk,
                                            expected_version=command.version, resume=True)
                row = registry.ledger.read(key)
            if row['state'] == 'WAITING' and row['goal_confirmed'] and evidence is not None:
                step = evidence(command, row, payload, 'step')
                if step is not None:
                    # A fresh clock at the confirmation site: refresh stamps reconciliation
                    # with its own now, and confirmation must not run before it.
                    registry.ledger.confirm(key, step, now=timezone.now())
                    row = registry.ledger.read(key)
            if row['state'] == 'AWAITING_FINAL_EVIDENCE' and evidence is not None:
                final = evidence(command, row, payload, 'final')
                if final is not None:
                    registry.ledger.finish(key, final, now=timezone.now())
                    return _save(command, command.actor, 'COMPLETED').state
            return command.state
        except Exception:
            # Uncertain outcome: hold the stage, keep the original request key, reconcile later.
            try:
                registry.ledger.hold(key)
            except Exception:
                pass
            command.refresh_from_db()
            if command.state in EXECUTING_STATES:
                return _save(command, command.actor, 'UNKNOWN', 'RECONCILIATION_REQUIRED').state
            return command.state
