from django.test import SimpleTestCase
from harbor.ownership import WriterLease
import tempfile
from pathlib import Path
import os
import json
from unittest.mock import patch
from harbor_web.tests import test_services as fixtures
from django.test import TestCase
from django.test import TransactionTestCase
from harbor_web import commands, services
from harbor_web.models import Command, CommandFact
from harbor_web.services import Conflict
from django.core.exceptions import PermissionDenied, ValidationError
from django.contrib.auth.models import User


class LeaseTests(SimpleTestCase):
    def test_second_writer_refused_and_release(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'ledger.sqlite3'
            with WriterLease(path):
                with self.assertRaises(RuntimeError):
                    with WriterLease(path):
                        pass
            with WriterLease(path):
                pass


class CommandTests(TestCase):
    def test_same_source_with_changed_installation_invalidates_original_plan(self):
        from harbor_web import services
        from django.utils import timezone
        plan = self.plan()
        observed = plan.observation
        self.assertTrue(commands.capability(plan)['enabled'])
        condition = services.register_condition(self.admin, name='Changed map revision',
            calibration_ref='different-installation-reference', payload=observed.condition.payload)
        services.record_observation(self.admin, source_id=observed.source_id, source_revision='changed-map',
            observed_at=timezone.now(), provenance=observed.provenance, payload=observed.payload, condition=condition)
        self.assertEqual(commands.capability(plan)['reason'], 'OBSERVATION_REVISION_CHANGED')

    def test_new_source_revision_blocks_old_fresh_plan(self):
        from harbor_web import services
        from django.utils import timezone
        plan = self.plan()
        observed = plan.observation
        self.assertTrue(commands.capability(plan)['enabled'])
        services.record_observation(observed.created_by, source_id=observed.source_id,
            source_revision='newer-revision', observed_at=timezone.now(), provenance=observed.provenance,
            payload=observed.payload, condition=observed.condition)
        self.assertEqual(commands.capability(plan)['reason'], 'OBSERVATION_REVISION_CHANGED')

    def test_restore_gate_reason_survives_capability_and_submission(self):
        plan = self.plan()
        command = commands.prepare_command(self.operator, plan_id=plan.pk, expected_version=1)
        with patch('harbor_web.runtime.execution_block_reason', return_value='RESTORE_RECONCILIATION_REQUIRED'):
            self.assertEqual(commands.capability(plan)['reason'], 'RESTORE_RECONCILIATION_REQUIRED')
            result = commands.submit_command(self.operator, command_id=command.pk, expected_version=command.version)
            self.assertEqual(result.state, 'BLOCKED')
            self.assertEqual(result.reason, 'RESTORE_RECONCILIATION_REQUIRED')

    cargo = fixtures.ServicesTests.cargo
    condition = fixtures.ServicesTests.condition
    observation = fixtures.ServicesTests.observation
    order = fixtures.ServicesTests.order

    def setUp(self):
        fixtures.ServicesTests.setUp(self)
        from django.conf import settings
        from harbor_web.runtime_models import DeploymentState
        DeploymentState.objects.get_or_create(pk=1, defaults={'mode': settings.MODE})
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(commands.registry_close)
        root = Path(self.directory.name)
        bindings = root / 'bindings.json'
        bindings.write_text(json.dumps([dict(cargo_id='C1', source='bay', destination='out', workcell_id='cell',
            instance_id='instance', object_ref='cargo', source_ref='bay-ref', target_ref='out-ref')]))
        self.environment = patch.dict(os.environ, dict(HARBOR_LEDGER_PATH=str(root / 'ledger.db'),
            HARBOR_FLEET_ORIGIN='http://127.0.0.1:9999', HARBOR_FLEET_TOKEN='test',
            HARBOR_FLEET_PRINCIPAL='operator-1', HARBOR_BINDINGS_PATH=str(bindings), HARBOR_WEB_ALLOW_SIMULATION='1'))
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def plan(self, provenance='SYNTHETIC'):
        from harbor_web import services
        order = self.order()
        observation = self.observation(provenance=provenance)
        return services.make_plan(self.operator, order_id=order.pk, expected_version=1, observation_id=observation.pk)

    def test_declared_observation_cannot_enable_execution(self):
        self.assertFalse(commands.capability(self.plan('DECLARED'))['enabled'])

    def test_completed_order_refuses_a_new_command(self):
        plan = self.plan()
        command = commands.prepare_command(self.operator, plan_id=plan.pk, expected_version=1)
        Command.objects.filter(pk=command.pk).update(state='COMPLETED')
        again = services.make_plan(self.operator, order_id=plan.order_id, expected_version=1,
                                   observation_id=plan.observation_id, now=self.now)
        self.assertEqual(commands.capability(again)['reason'], 'ORDER_ALREADY_COMPLETED')
        with self.assertRaises(ValidationError):
            commands.prepare_command(self.operator, plan_id=again.pk, expected_version=1)
        self.assertEqual(Command.objects.filter(order_id=plan.order_id).count(), 1)

    def test_prepare_idempotent_and_no_ledger_business_yet(self):
        plan = self.plan()
        command = commands.prepare_command(self.operator, plan_id=plan.pk, expected_version=1)
        self.assertEqual(commands.acquire_registry().client.expected_role, 'service')
        self.assertEqual(commands.prepare_command(self.operator, plan_id=plan.pk, expected_version=1).pk, command.pk)
        self.assertIsNone(commands.command_detail(command)['ledger'])
        with self.assertRaises(Conflict):
            commands.prepare_command(self.operator, plan_id=plan.pk, expected_version=2)

    def test_plan_confirmation_submits_once_and_repeated_post_opens_original(self):
        plan = self.plan()
        registry = commands.acquire_registry()
        self.client.force_login(self.operator)
        with patch.object(registry.client, 'propose_arm', side_effect=TimeoutError) as post:
            response = self.client.post(f'/plans/{plan.pk}/', {'expected_version': 1})
            command = Command.objects.get(plan=plan)
            self.assertRedirects(response, f'/work/{command.pk}/', fetch_redirect_response=False)
            self.assertEqual(command.state, 'UNKNOWN')
            self.assertEqual(post.call_count, 1)
            response = self.client.post(f'/plans/{plan.pk}/', {'expected_version': 1})
            self.assertRedirects(response, f'/work/{command.pk}/', fetch_redirect_response=False)
            self.assertEqual(post.call_count, 1)

    def test_role_explicit_even_superuser(self):
        plan = self.plan()
        root = User.objects.create_superuser('command-root')
        with self.assertRaises(PermissionDenied):
            commands.prepare_command(root, plan_id=plan.pk, expected_version=1)

    def test_failed_dispatch_is_never_posted_again(self):
        command = commands.prepare_command(self.operator, plan_id=self.plan().pk, expected_version=1)
        registry = commands.acquire_registry()
        with patch.object(registry.client, 'propose_arm', side_effect=TimeoutError) as post:
            command = commands.submit_command(self.operator, command_id=command.pk, expected_version=1)
            self.assertEqual(command.state, 'UNKNOWN')
            with self.assertRaises(Conflict):
                commands.submit_command(self.operator, command_id=command.pk, expected_version=command.version)
            self.assertEqual(post.call_count, 1)
        self.assertEqual(commands.command_detail(command)['ledger']['state'], 'HOLD')

    def test_identity_rotation_refused(self):
        command = commands.prepare_command(self.operator, plan_id=self.plan().pk, expected_version=1)
        with patch.dict(os.environ, HARBOR_FLEET_PRINCIPAL='other'):
            with self.assertRaises(Conflict):
                commands.submit_command(self.operator, command_id=command.pk, expected_version=1)

    def test_interruption_preserves_dispatch_marker(self):
        command = commands.prepare_command(self.operator, plan_id=self.plan().pk, expected_version=1)
        registry = commands.acquire_registry()
        with patch.object(registry.client, 'propose_arm', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                commands.submit_command(self.operator, command_id=command.pk, expected_version=1)
        command.refresh_from_db()
        self.assertEqual(command.state, 'DISPATCHING')
        self.assertEqual(commands.command_detail(command)['ledger']['state'], 'HOLD')

    def job(self, command):
        import hashlib
        from harbor.process import ArmTransferBinding
        binding = ArmTransferBinding(**command.binding['moves'][0])
        request_key = commands._key(command) + ':0'
        transfer = {k: v for k, v in binding.proposal_request(request_key).items() if k != 'request_key'}
        return dict(action_kind='ARM_TRANSFER', request_key=request_key, transfer=transfer,
            request_digest=hashlib.sha256(json.dumps(transfer, sort_keys=True, separators=(',', ':')).encode()).hexdigest(),
            mission_id='mission-1', status='READY', proposal_principal_id='operator-1')

    def test_recovery_get_only_and_cancel_ack_keeps_business_hold(self):
        command = commands.prepare_command(self.operator, plan_id=self.plan().pk, expected_version=1)
        registry = commands.acquire_registry()
        with patch.object(registry.client, 'propose_arm', side_effect=TimeoutError):
            command = commands.submit_command(self.operator, command_id=command.pk, expected_version=1)
        job = self.job(command)
        with patch.object(registry.client, 'arm_request', return_value={'job': job}), patch.object(registry.client, 'propose_arm') as post:
            command = commands.reconcile_command(self.operator, command_id=command.pk, expected_version=command.version)
            self.assertEqual(command.mission_id, 'mission-1')
            self.assertEqual(command.reason, 'BUSINESS_HOLD')
            post.assert_not_called()
        commands.registry_close()
        self.assertEqual(commands.command_detail(command)['job']['mission_id'], 'mission-1')
        registry = commands.acquire_registry()
        with patch.object(registry.client, 'cancel_arm', return_value={'ack': True}) as cancel:
            command = commands.cancel_command(self.operator, command_id=command.pk, expected_version=command.version, reason='Operator stop')
            cancel.assert_called_once_with('mission-1', reason='Operator stop')
        self.assertEqual(command.state, 'CANCEL_PENDING')
        self.assertEqual(commands.command_detail(command)['ledger']['state'], 'HOLD')

    def test_saved_binding_cannot_be_overwritten(self):
        from django.core.exceptions import ValidationError
        from django.db import IntegrityError, transaction
        command = commands.prepare_command(self.operator, plan_id=self.plan().pk, expected_version=1)
        with self.assertRaises(ValidationError):
            Command.objects.filter(pk=command.pk).update(binding={})
        command.binding = {}
        with self.assertRaises(ValidationError):
            command.save()
        with self.assertRaises(IntegrityError), transaction.atomic():
            Command(pk=command.pk, plan=command.plan, order=command.order, actor=self.operator, plan_version=1,
                ledger_identity='different', fleet_origin='different', fleet_principal='different', body_digest='a'*64, binding={}).save()

    def test_validated_receipt_persisted_without_business_completion(self):
        command = commands.prepare_command(self.operator, plan_id=self.plan().pk, expected_version=1)
        registry = commands.acquire_registry()
        commands._prepare_ledger(command, registry)
        registry.ledger.hold(commands._key(command))
        job = self.job(command)
        job.update(status='GOAL_CONFIRMED', resource_policy_digest='a'*64, authority_epoch=1, dispatch_generation=2)
        receipt = dict(mission_id='mission-1', workcell_id='cell', instance_id='instance', object_ref='cargo',
            source='bay-ref', target='out-ref', policy_digest='a'*64, authority_epoch=1, dispatch_generation=2,
            physical_status='AT_TARGET_AND_CLEAR', validated_at_ns=3, request_digest=job['request_digest'],
            evidence_digest='b'*64, receipt_id='receipt-1', action_id='action-1', attempt_id='attempt-1')
        with patch.object(registry.client, 'arm_request', return_value={'job': job}), patch.object(registry.client, 'physical_receipt', return_value={'receipt': receipt}):
            command = commands.reconcile_command(self.operator, command_id=command.pk, expected_version=1)
        commands.registry_close()
        detail = commands.command_detail(command)
        self.assertEqual(detail['receipt']['receipt_id'], 'receipt-1')
        self.assertEqual(detail['ledger']['state'], 'HOLD')
        self.assertEqual(detail['ledger']['goal_confirmed'], 0)
        CommandFact.objects.create(created_by=self.operator, command=command, command_key=command.key,
            body_digest=command.body_digest, fleet_origin=command.fleet_origin, fleet_principal=command.fleet_principal,
            job=job, receipt=dict(receipt, request_digest='c'*64), read_at=self.now + __import__('datetime').timedelta(seconds=5))
        self.assertEqual(commands.command_detail(command)['receipt']['request_digest'], 'c'*64)
        bad = dict(job, mission_id='wrong')
        CommandFact.objects.create(created_by=self.operator, command=command, command_key=command.key,
            body_digest=command.body_digest, fleet_origin=command.fleet_origin, fleet_principal=command.fleet_principal,
            job=bad, receipt=receipt, read_at=self.now + __import__('datetime').timedelta(seconds=10))
        self.assertIsNone(commands.command_detail(command)['job'])

    def test_reconciliation_uses_one_job_and_receipt_for_ledger_and_audit(self):
        command = commands.prepare_command(self.operator, plan_id=self.plan().pk, expected_version=1)
        registry = commands.acquire_registry()
        job = self.job(command)
        with patch.object(registry.client, 'propose_arm', return_value={'job': job}), patch.object(
                registry.client, 'arm_request', return_value={'job': job}):
            command = commands.submit_command(self.operator, command_id=command.pk, expected_version=1)
        job.update(status='GOAL_CONFIRMED', resource_policy_digest='a'*64, authority_epoch=1, dispatch_generation=2)
        receipt = dict(mission_id='mission-1', workcell_id='cell', instance_id='instance', object_ref='cargo',
            source='bay-ref', target='out-ref', policy_digest='a'*64, authority_epoch=1, dispatch_generation=2,
            physical_status='AT_TARGET_AND_CLEAR', validated_at_ns=3, request_digest='c'*64,
            evidence_digest='b'*64, receipt_id='receipt-1', action_id='action-1', attempt_id='attempt-1')
        with patch.object(registry.client, 'arm_request', return_value={'job': job}) as read, patch.object(
                registry.client, 'physical_receipt', return_value={'receipt': receipt}) as physical, patch.object(
                registry.client, 'arm_job', side_effect=AssertionError('A second job read must not occur')):
            command = commands.reconcile_command(self.operator, command_id=command.pk, expected_version=command.version)
        self.assertEqual(command.state, 'RECONCILED')
        read.assert_called_once_with(commands._key(command) + ':0')
        physical.assert_called_once_with('mission-1')
        row = registry.ledger.read(commands._key(command))
        fact = CommandFact.objects.filter(command=command).latest('pk')
        self.assertEqual(row['attempt_id'], fact.receipt['attempt_id'])
        self.assertEqual(row['mission_id'], fact.job['mission_id'])
        self.assertEqual(row['goal_confirmed'], 1)

    def test_executor_reconciliation_cannot_resume_a_pending_cancellation(self):
        command = commands.prepare_command(self.operator, plan_id=self.plan().pk, expected_version=1)
        registry = commands.acquire_registry()
        job = self.job(command)
        with patch.object(registry.client, 'propose_arm', return_value={'job': job}), patch.object(
                registry.client, 'arm_request', return_value={'job': job}):
            command = commands.submit_command(self.operator, command_id=command.pk, expected_version=1)
        with patch.object(registry.client, 'cancel_arm', return_value={'ack': True}):
            command = commands.cancel_command(self.operator, command_id=command.pk,
                expected_version=command.version, reason='Operator stop')
        with patch.object(registry.client, 'arm_request', return_value={'job': job}):
            command = commands.reconcile_command(self.operator, command_id=command.pk,
                expected_version=command.version, resume=True)
        self.assertEqual(command.state, 'CANCEL_PENDING')
        self.assertEqual(registry.ledger.read(commands._key(command))['state'], 'HOLD')

    def test_binding_profile_rotation_blocks_dispatch(self):
        command = commands.prepare_command(self.operator, plan_id=self.plan().pk, expected_version=1)
        path = Path(os.environ['HARBOR_BINDINGS_PATH'])
        bindings = json.loads(path.read_text())
        bindings[0]['target_ref'] = 'different-target'
        path.write_text(json.dumps(bindings))
        with patch.object(commands.acquire_registry().client, 'propose_arm') as post:
            result = commands.submit_command(self.operator, command_id=command.pk, expected_version=1)
        post.assert_not_called()
        self.assertEqual(result.state, 'BLOCKED')
        self.assertEqual(result.binding, command.binding)

    def _assert_replanning_keeps_lineage(self, state):
        from harbor_web import services
        plan = self.plan()
        command = commands.prepare_command(self.operator, plan_id=plan.pk, expected_version=1)
        registry = commands.acquire_registry()
        with patch.object(registry.client, 'propose_arm', side_effect=TimeoutError) as post:
            command = commands.submit_command(self.operator, command_id=command.pk, expected_version=1)
            if state == 'CANCEL_PENDING':
                command = commands.cancel_command(self.operator, command_id=command.pk, expected_version=command.version, reason='stop')
            second = services.make_plan(self.operator, order_id=plan.order_id, expected_version=1, observation_id=plan.observation_id)
            self.assertEqual(commands.capability(second)['reason'], 'EXISTING_ORDER_EXECUTION_REQUIRES_RECONCILIATION')
            with self.assertRaises(Conflict):
                commands.prepare_command(self.operator, plan_id=second.pk, expected_version=1)
            self.assertEqual(post.call_count, 1)
        self.assertEqual(commands.prepare_command(self.operator, plan_id=plan.pk, expected_version=1).pk, command.pk)

    def test_replanning_after_unknown_keeps_original_key(self):
        self._assert_replanning_keeps_lineage('UNKNOWN')

    def test_replanning_after_cancel_keeps_original_key(self):
        self._assert_replanning_keeps_lineage('CANCEL_PENDING')

    def test_new_order_occurrence_can_prepare_its_own_command(self):
        from harbor_web import services
        plan = self.plan()
        first = commands.prepare_command(self.operator, plan_id=plan.pk, expected_version=1)
        services.update_order(self.operator, order_id=plan.order_id, expected_version=1,
            priority_group=2, sequence=1, tie_policy='FIXED')
        self.assertEqual(commands.prepare_command(self.operator, plan_id=plan.pk, expected_version=1).pk, first.pk)
        new_order = services.create_order(self.operator, cargo=plan.order.cargo, kind='OUTBOUND',
            priority_group=1, sequence=1, tie_policy='FIXED')
        new_plan = services.make_plan(self.operator, order_id=new_order.pk, expected_version=1, observation_id=plan.observation_id)
        second = commands.prepare_command(self.operator, plan_id=new_plan.pk, expected_version=1)
        self.assertNotEqual(first.key, second.key)
        self.assertNotEqual(first.order_id, second.order_id)

    def test_order_lineage_database_unique(self):
        from harbor_web import services
        from django.db import IntegrityError, transaction
        plan = self.plan()
        command = commands.prepare_command(self.operator, plan_id=plan.pk, expected_version=1)
        second = services.make_plan(self.operator, order_id=plan.order_id, expected_version=1, observation_id=plan.observation_id)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Command.objects.create(order=plan.order, plan=second, actor=self.operator, plan_version=1,
                ledger_identity=command.ledger_identity, fleet_origin=command.fleet_origin,
                fleet_principal=command.fleet_principal, body_digest=command.body_digest, binding=command.binding)

    def test_cancel_interruption_repaired_after_registry_restart(self):
        command = commands.prepare_command(self.operator, plan_id=self.plan().pk, expected_version=1)
        registry = commands.acquire_registry()
        job = self.job(command)
        with patch.object(registry.client, 'propose_arm', return_value={'job': job}), patch.object(registry.client, 'arm_request', return_value={'job': job}):
            command = commands.submit_command(self.operator, command_id=command.pk, expected_version=1)
        with patch.object(registry.ledger, 'hold', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                commands.cancel_command(self.operator, command_id=command.pk, expected_version=command.version, reason='stop')
        command.refresh_from_db()
        self.assertEqual(command.state, 'CANCEL_PENDING')
        commands.registry_close()
        registry = commands.acquire_registry()
        self.assertEqual(registry.ledger.read(commands._key(command))['state'], 'HOLD')
        with patch.object(registry.client, 'arm_request', return_value={'job': job}), patch.object(registry.client, 'cancel_arm') as post:
            command = commands.reconcile_command(self.operator, command_id=command.pk, expected_version=command.version)
        post.assert_not_called()
        self.assertEqual(command.state, 'CANCEL_PENDING')
        self.assertEqual(commands.command_detail(command)['ledger']['state'], 'HOLD')

    def test_receipt_grant_digest_can_differ_from_transfer_body_digest(self):
        from harbor.process import ArmTransferBinding
        command = commands.prepare_command(self.operator, plan_id=self.plan().pk, expected_version=1)
        binding = ArmTransferBinding(**command.binding['moves'][0])
        job = self.job(command)
        job.update(status='GOAL_CONFIRMED', resource_policy_digest='a'*64, authority_epoch=1, dispatch_generation=2)
        receipt = dict(mission_id='mission-1', workcell_id='cell', instance_id='instance', object_ref='cargo',
            source='bay-ref', target='out-ref', policy_digest='a'*64, authority_epoch=1, dispatch_generation=2,
            physical_status='AT_TARGET_AND_CLEAR', validated_at_ns=3, request_digest='c'*64,
            evidence_digest='b'*64, receipt_id='receipt-1', action_id='action-1', attempt_id='attempt-1')
        self.assertNotEqual(receipt['request_digest'], job['request_digest'])
        self.assertEqual(binding.check_receipt(job, receipt), receipt)
        with self.assertRaises(ValueError):
            binding.check_receipt(job, dict(receipt, dispatch_generation=3))
        with self.assertRaises(ValueError):
            binding.check_receipt(job, dict(receipt, target='other-scope'))


class DispatchCoordinationTests(TransactionTestCase):
    setUp = CommandTests.setUp
    cargo = CommandTests.cargo
    condition = CommandTests.condition
    observation = CommandTests.observation
    order = CommandTests.order
    plan = CommandTests.plan

    def test_order_edit_waits_for_pending_dispatch(self):
        from threading import Event, Thread
        from django.db import connections
        from harbor_web import services
        from harbor_web.models import Order
        plan = self.plan()
        command = commands.prepare_command(self.operator, plan_id=plan.pk, expected_version=1)
        registry = commands.acquire_registry()
        entered, release, edit_started, edited = Event(), Event(), Event(), Event()
        errors = []

        def post(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise AssertionError('Test did not release dispatch')
            raise TimeoutError

        def dispatch():
            try:
                commands.submit_command(self.operator, command_id=command.pk, expected_version=1)
            except BaseException as error:
                errors.append(error)
            finally:
                connections.close_all()

        def edit():
            try:
                edit_started.set()
                services.update_order(self.operator, order_id=plan.order_id, expected_version=1,
                    priority_group=2, sequence=1, tie_policy='ANY', note='new instruction')
                edited.set()
            except BaseException as error:
                errors.append(error)
            finally:
                connections.close_all()

        with patch.object(registry.client, 'propose_arm', side_effect=post):
            submission = Thread(target=dispatch)
            modification = Thread(target=edit)
            submission.start()
            try:
                self.assertTrue(entered.wait(5))
                modification.start()
                self.assertTrue(edit_started.wait(5))
                self.assertFalse(edited.wait(.2), 'Queue edit ran while dispatch outcome was pending')
            finally:
                release.set()
                submission.join(5)
                if modification.ident:
                    modification.join(5)
        self.assertFalse(submission.is_alive())
        self.assertFalse(modification.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(edited.is_set())
        self.assertEqual(Order.objects.get(pk=plan.order_id).version, 2)
        command.refresh_from_db()
        self.assertEqual(command.state, 'UNKNOWN')


class CommandLineageMigrationTests(TransactionTestCase):
    setUp = CommandTests.setUp
    cargo = CommandTests.cargo
    condition = CommandTests.condition
    observation = CommandTests.observation
    order = CommandTests.order
    plan = CommandTests.plan

    def test_duplicate_backfill_refuses_without_choosing_or_deleting_intent(self):
        from django.db import connection
        from django.db.migrations.executor import MigrationExecutor
        from harbor_web import services
        plan = self.plan()
        second = services.make_plan(self.operator, order_id=plan.order_id, expected_version=1, observation_id=plan.observation_id)
        old_target = [('harbor_web', '0007_deploymentstate_relocation')]
        new_target = [('harbor_web', '0008_command_order')]
        executor = MigrationExecutor(connection)
        executor.migrate(old_target)
        old_apps = executor.loader.project_state(old_target).apps
        OldCommand = old_apps.get_model('harbor_web', 'Command')
        fields = dict(actor_id=self.operator.pk, plan_version=1, ledger_identity='ledger', fleet_origin='http://localhost',
            fleet_principal='operator', body_digest='a'*64, binding={}, state='UNKNOWN')
        first = OldCommand.objects.create(plan_id=plan.pk, **fields)
        duplicate = OldCommand.objects.create(plan_id=second.pk, **fields)
        try:
            with self.assertRaisesRegex(RuntimeError, 'Operator reconciliation'):
                MigrationExecutor(connection).migrate(new_target)
            self.assertEqual(set(OldCommand.objects.values_list('key', flat=True)), {first.key, duplicate.key})
        finally:
            # Test cleanup only; production migration never chooses a surviving execution.
            OldCommand.objects.filter(pk=duplicate.pk).delete()
            cleanup = MigrationExecutor(connection)
            cleanup.migrate(cleanup.loader.graph.leaf_nodes())
        self.assertEqual(Command.objects.get(pk=first.pk).order_id, plan.order_id)
