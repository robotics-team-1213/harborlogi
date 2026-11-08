"""P1 executor acceptance: durable multi-step progression without any web request."""
from hashlib import sha256
from itertools import count
import json
import os
from pathlib import Path
import tempfile
from unittest.mock import patch
from urllib.parse import unquote

from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from harbor.rosy_http import RosyClient
from harbor.workflow import ArmRetreat, MoveEvidence, PlacementEvidence, RetreatEvidence
from harbor_web import commands, executor, services
from harbor_web.models import Command
from harbor_web.tests import test_services as fixtures

PLACEMENT_SOURCE, RETREAT_SOURCE = 'installed-camera', 'fleet-retreat'
PRINCIPAL = 'operator-1'


class Fleet:
    """In-memory ARM fleet: posts are idempotent-keyed, statuses are staged per job."""

    def __init__(self):
        self.calls, self.jobs = [], {}
        self.status = 'PROPOSED'
        self.fail_receipt_once = False

    def _job(self, job):
        return {'job': dict(job, status=self.status)}

    def __call__(self, method, path, body):
        if method == 'GET' and path == '/api/fleet/session':
            # The client verifies the configured application identity before every
            # operation (ADR0015); fakes must answer that check like the real Fleet.
            # Identity probes stay out of `calls`, which is the ARM operation log.
            return {'principal_id': PRINCIPAL, 'role': 'service'}
        self.calls.append((method, path, body))
        if method == 'POST' and path == '/api/fleet/arm-transfers/jobs':
            request_key = body['request_key']
            if request_key not in self.jobs:
                transfer = {k: v for k, v in body.items() if k != 'request_key'}
                self.jobs[request_key] = dict(mission_id=f'physical-{len(self.jobs) + 1}',
                    action_kind='ARM_TRANSFER', proposal_principal_id=PRINCIPAL, request_key=request_key,
                    request_digest=sha256(json.dumps(transfer, sort_keys=True, separators=(',', ':')).encode()).hexdigest(),
                    transfer=transfer, resource_policy_digest='c' * 64, authority_epoch=2, dispatch_generation=3)
            return self._job(self.jobs[request_key])
        if path.startswith('/api/fleet/arm-transfers/jobs?request_key='):
            return self._job(self.jobs[unquote(path.split('request_key=', 1)[1])])
        mission = unquote(path[len('/api/fleet/arm-transfers/jobs/'):].split('/', 1)[0].split('?', 1)[0])
        job = next(job for job in self.jobs.values() if job['mission_id'] == mission)
        if path.endswith('/physical-receipt'):
            if self.fail_receipt_once:
                self.fail_receipt_once = False
                raise TimeoutError('receipt endpoint unreachable')
            transfer = job['transfer']
            suffix = mission.rsplit('-', 1)[1]
            return {'receipt': dict(receipt_id=f'receipt-{suffix}', mission_id=mission,
                action_id=f'action-{suffix}', attempt_id=f'attempt-{suffix}', request_digest='e' * 64,
                workcell_id=transfer['workcell_id'], instance_id=transfer['instance_id'],
                policy_digest=job['resource_policy_digest'], evidence_digest='d' * 64,
                object_ref=transfer['object_ref'], source=transfer['source'], target=transfer['target'],
                authority_epoch=job['authority_epoch'], dispatch_generation=job['dispatch_generation'],
                validated_at_ns=2_000_000_000, physical_status='AT_TARGET_AND_CLEAR')}
        return self._job(job)

    def posted_keys(self):
        return [body['request_key'] for method, path, body in self.calls
                if method == 'POST' and path == '/api/fleet/arm-transfers/jobs']


def evidence_provider(now):
    """Synthetic MoveEvidence bound to the row being confirmed (P1 tests only).

    Identities are derived from (mission, attempt, phase) so no observation is ever
    consumed twice, across ticks and across the final outbound evidence.
    """
    def build(command, row, payload, phase):
        if phase == 'step':
            move = payload['moves'][row['step_index']]
            arms = tuple(payload['move_arms'][row['step_index']])
            mission, attempt = row['mission_id'], row['attempt_id']
            cargo, location = move['cargo_id'], move['destination']
        elif phase == 'final':
            arms = tuple(sorted({arm for step in payload['move_arms'] for arm in step}))
            mission, attempt = row['outbound_mission_id'], row['outbound_attempt_id']
            cargo, location = payload['outbound_id'], payload['outbound_stand']
        else:
            raise ValueError(phase)
        if mission is None or attempt is None:
            return None
        tag = f'{mission}-{attempt}-{phase}'
        placement = PlacementEvidence(f'{tag}-p', mission, attempt, payload['revision'],
                                      PLACEMENT_SOURCE, now, cargo, location)
        retreat = RetreatEvidence(f'{tag}-r', mission, attempt, payload['revision'],
                                  RETREAT_SOURCE, now, tuple(ArmRetreat(arm, f'zone-{arm}', True,
                                  now, payload['revision'], True) for arm in arms))
        return MoveEvidence(placement, retreat)
    return build


class ExecutorTests(TestCase):
    cargo = fixtures.ServicesTests.cargo
    condition = fixtures.ServicesTests.condition
    order = fixtures.ServicesTests.order

    def setUp(self):
        fixtures.ServicesTests.setUp(self)
        from harbor_web.runtime_models import DeploymentState
        from django.conf import settings
        DeploymentState.objects.get_or_create(pk=1, defaults={'mode': settings.MODE})
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(commands.registry_close)
        self.fleet = Fleet()
        root = Path(self.directory.name)
        bindings = [dict(cargo_id='C2', source='bay', destination='temp', workcell_id='cell',
                         instance_id='instance', object_ref='object-c2', source_ref='bay-ref', target_ref='temp-ref'),
                    dict(cargo_id='C1', source='bay', destination='out', workcell_id='cell',
                         instance_id='instance', object_ref='object-c1', source_ref='bay-ref', target_ref='out-ref'),
                    dict(cargo_id='C2', source='temp', destination='bay', workcell_id='cell',
                         instance_id='instance', object_ref='object-c2', source_ref='temp-ref', target_ref='bay-ref')]
        (root / 'bindings.json').write_text(json.dumps(bindings))
        self.environment = patch.dict(os.environ, dict(HARBOR_LEDGER_PATH=str(root / 'ledger.db'),
            HARBOR_FLEET_ORIGIN='http://127.0.0.1:9999', HARBOR_FLEET_TOKEN='test',
            HARBOR_FLEET_PRINCIPAL=PRINCIPAL, HARBOR_BINDINGS_PATH=str(root / 'bindings.json'),
            HARBOR_WEB_ALLOW_SIMULATION='1'))
        self.environment.start()
        self.addCleanup(self.environment.stop)
        registry = commands.acquire_registry()
        registry.client = RosyClient(transport=self.fleet)
        registry.client.expected_principal = PRINCIPAL
        registry.ledger.placement_sources = (PLACEMENT_SOURCE,)
        registry.ledger.retreat_sources = (RETREAT_SOURCE,)
        self.registry = registry

    def plan(self):
        services.register_cargo(self.operator, code='C2', name='Rehandle', weight_kg='1', volume_m3='1', destination='yard')
        cargo = services.register_cargo(self.operator, code='C1', name='Outbound', weight_kg='1', volume_m3='1', destination='yard')
        observation = fixtures.ServicesTests.observation(self, payload={
            'bays': [{'bay_id': 'bay', 'cargo': ['C1', 'C2'], 'capacity': 2},
                     {'bay_id': 'temp', 'cargo': [], 'capacity': 2},
                     {'bay_id': 'out', 'cargo': [], 'capacity': 1}], 'holdings': []},
            provenance='SYNTHETIC')
        order = services.create_order(self.operator, cargo=cargo, kind='OUTBOUND', priority_group=1,
                                      sequence=1, tie_policy='FIXED')
        return services.make_plan(self.operator, order_id=order.pk, expected_version=1, observation_id=observation.pk)

    def confirmed_command(self):
        plan = self.plan()
        command = commands.prepare_command(self.operator, plan_id=plan.pk, expected_version=1)
        return commands.submit_command(self.operator, command_id=command.pk, expected_version=command.version)

    def run_ticks(self, command, *, evidence=True, limit=12):
        for _ in range(limit):
            provider = evidence_provider(timezone.now()) if evidence else None
            result = executor.tick(registry=self.registry, evidence=provider, now=timezone.now())
            command.refresh_from_db()
            if command.state == 'COMPLETED':
                return command, result
        return command, None

    def test_three_step_business_progresses_without_any_web_request(self):
        command = self.confirmed_command()
        key = commands._key(command)
        self.assertEqual(self.fleet.posted_keys(), [f'{key}:0'])
        self.fleet.status = 'GOAL_CONFIRMED'
        finished, _ = self.run_ticks(command)
        self.assertEqual(finished.state, 'COMPLETED')
        row = self.registry.ledger.read(key)
        self.assertEqual(row['state'], 'COMPLETED')
        self.assertEqual(row['step_index'], 3)
        self.assertEqual(sorted(self.fleet.posted_keys()), sorted([f'{key}:0', f'{key}:1', f'{key}:2']))
        self.assertFalse(any('admit' in path or 'observations' in path or 'compile' in path
                             for _, path, _ in self.fleet.calls))

    def test_initial_and_next_submit_use_one_bound_readback_and_save_the_fact(self):
        from harbor_web.models import CommandFact
        command = self.confirmed_command()
        self.assertEqual(len(self.fleet.calls), 2)
        fact = CommandFact.objects.get(command=command)
        self.assertEqual(fact.job['mission_id'], command.mission_id)
        self.assertNotIn('payload', commands.command_detail(command)['ledger'])
        self.fleet.status = 'GOAL_CONFIRMED'
        executor.tick(registry=self.registry, evidence=evidence_provider(timezone.now()))
        self.fleet.calls.clear()
        executor.tick(registry=self.registry)
        command.refresh_from_db()
        # A completed receipt is read once as part of this submission snapshot.
        self.assertEqual(len(self.fleet.calls), 3)
        fact = CommandFact.objects.filter(command=command).latest('pk')
        self.assertEqual(fact.job['mission_id'], command.mission_id)
        self.assertEqual(fact.receipt['mission_id'], command.mission_id)

    def test_submission_audit_names_the_submitting_operator(self):
        from django.contrib.auth.models import User, Group
        from harbor_web.models import Audit
        plan = self.plan()
        command = commands.prepare_command(self.operator, plan_id=plan.pk, expected_version=1)
        submitter = User.objects.create_user('second-operator')
        submitter.groups.add(Group.objects.get(name='operator'))
        command = commands.submit_command(submitter, command_id=command.pk, expected_version=command.version)
        self.assertEqual(command.state, 'RECONCILED')
        audit = Audit.objects.get(action='command.reconciled', object_id=str(command.pk))
        self.assertEqual(audit.actor, submitter)

    def test_fleet_reads_alone_never_confirm_a_stage(self):
        command = self.confirmed_command()
        self.fleet.status = 'GOAL_CONFIRMED'
        executor.tick(registry=self.registry, evidence=None, now=timezone.now())
        key = commands._key(command)
        row = self.registry.ledger.read(key)
        self.assertEqual(row['state'], 'WAITING')
        self.assertEqual(row['goal_confirmed'], 1)
        self.assertEqual(row['step_index'], 0)
        from contextlib import closing
        with closing(self.registry.ledger._connect()) as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM evidence').fetchone()[0], 0)
        command.refresh_from_db()
        self.assertEqual(command.state, 'RECONCILED')
        self.assertEqual(self.fleet.posted_keys(), [f'{key}:0'])

    def test_repeated_ticks_without_status_change_are_read_only(self):
        command = self.confirmed_command()
        self.fleet.status = 'ACTION_SUCCEEDED'
        for _ in range(2):
            executor.tick(registry=self.registry, evidence=evidence_provider(timezone.now()), now=timezone.now())
        key = commands._key(command)
        self.assertEqual(self.fleet.posted_keys(), [f'{key}:0'])
        self.assertEqual(self.registry.ledger.read(key)['state'], 'WAITING')
        self.assertTrue(all(method == 'GET' for method, _, _ in self.fleet.calls[1:]))

    def test_transient_failure_holds_then_recovers_on_the_original_key(self):
        command = self.confirmed_command()
        key = commands._key(command)
        self.fleet.status = 'GOAL_CONFIRMED'
        self.fleet.fail_receipt_once = True
        executor.tick(registry=self.registry, evidence=evidence_provider(timezone.now()), now=timezone.now())
        command.refresh_from_db()
        self.assertEqual(command.state, 'UNKNOWN')
        self.assertEqual(self.registry.ledger.read(key)['state'], 'HOLD')
        posted_after_failure = list(self.fleet.posted_keys())
        finished, _ = self.run_ticks(command)
        self.assertEqual(finished.state, 'COMPLETED')
        self.assertEqual(self.fleet.posted_keys()[:len(posted_after_failure)], posted_after_failure)
        self.assertEqual(len(self.fleet.posted_keys()), len(set(self.fleet.posted_keys())),
                         'No request key is ever posted twice')

    def test_cancel_pending_commands_are_never_resumed(self):
        command = self.confirmed_command()
        commands.cancel_command(self.operator, command_id=command.pk, expected_version=command.version, reason='stop')
        before = list(self.fleet.calls)
        self.fleet.status = 'GOAL_CONFIRMED'
        result = executor.tick(registry=self.registry, evidence=evidence_provider(timezone.now()), now=timezone.now())
        self.assertEqual(result['results'], {})  # CANCEL_PENDING is outside the executor's active set.
        self.assertEqual(self.fleet.calls, before)
        self.assertEqual(self.registry.ledger.read(commands._key(command))['state'], 'HOLD')

    def test_blocked_runtime_stops_the_whole_tick(self):
        self.confirmed_command()
        with patch('harbor_web.runtime.execution_block_reason', return_value='RESTORE_RECONCILIATION_REQUIRED'):
            result = executor.tick(registry=self.registry, now=timezone.now())
        self.assertFalse(result['ran'])
        self.assertEqual(result['reason'], 'RESTORE_RECONCILIATION_REQUIRED')


class ExecutorThreadTests(SimpleTestCase):
    def test_start_is_idempotent_and_stop_ends_the_loop(self):
        with patch.dict(os.environ, {'HARBOR_EXECUTOR_INTERVAL': '60'}):
            try:
                first = executor.start()
                self.assertIs(executor.start(), first)
                self.assertTrue(first.is_alive())
            finally:
                executor.stop()
                first.join(timeout=2)
        self.assertFalse(first.is_alive())
