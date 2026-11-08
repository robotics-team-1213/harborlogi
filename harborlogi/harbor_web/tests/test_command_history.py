"""P3 acceptance: command history, terminal cancellation, and replan entry (design 28)."""
from django.core.exceptions import PermissionDenied
from django.test import TestCase
from django.utils import timezone

from harbor_web import commands, executor, services
from harbor_web.command_models import Command
from harbor_web.models import Audit, CommandFact
from harbor_web.services import Conflict
from harbor_web.tests import test_executor


class CommandHistoryTests(TestCase):
    cargo = test_executor.ExecutorTests.cargo
    condition = test_executor.ExecutorTests.condition
    order = test_executor.ExecutorTests.order

    def setUp(self):
        test_executor.ExecutorTests.setUp(self)

    def replan_plan(self, plan):
        """Bump the order version and make a fresh plan on a newer observation."""
        from harbor_web.models import Order
        services.update_order(self.operator, order_id=plan.order_id, expected_version=plan.order.version,
                              priority_group=plan.order.priority_group, sequence=plan.order.sequence,
                              tie_policy=plan.order.tie_policy, note=plan.order.note)
        order = Order.objects.get(pk=plan.order_id)
        observed = plan.observation
        return services.make_plan(self.operator, order_id=order.pk, expected_version=order.version,
                                  observation_id=services.record_observation(self.admin,
                                      source_id=observed.source_id, source_revision='replan-revision',
                                      observed_at=timezone.now(), provenance='SYNTHETIC', payload=observed.payload,
                                      condition=observed.condition).pk)

    def confirmed(self):
        return test_executor.ExecutorTests.confirmed_command(self)

    def plan(self):
        return test_executor.ExecutorTests.plan(self)

    def test_active_command_blocks_other_plans_for_the_order(self):
        command = self.confirmed()
        self.fleet.status = 'GOAL_CONFIRMED'
        executor.tick(registry=self.registry, evidence=test_executor.evidence_provider(timezone.now()),
                      now=timezone.now())  # Confirm step 0: partial completion.
        second = self.replan_plan(command.plan)
        with self.assertRaises(Conflict):
            commands.prepare_command(self.operator, plan_id=second.pk, expected_version=second.order_version)
        self.assertEqual(Command.objects.filter(order=command.order).count(), 1)

    def test_close_cancelled_then_replan_starts_a_new_command_with_history(self):
        command = self.confirmed()
        cancelled = commands.cancel_command(self.operator, command_id=command.pk,
                                            expected_version=command.version, reason='stop this work')
        self.assertEqual(cancelled.state, 'CANCEL_PENDING')
        with self.assertRaises(Conflict):
            commands.close_cancelled(self.operator, command_id=cancelled.pk, expected_version=999)
        closed = commands.close_cancelled(self.operator, command_id=cancelled.pk, expected_version=cancelled.version)
        self.assertEqual(closed.state, 'CANCELLED')
        original = (str(command.key), command.body_digest, command.mission_id)
        second = self.replan_plan(command.plan)
        fresh = commands.prepare_command(self.operator, plan_id=second.pk, expected_version=second.order_version)
        self.assertEqual(fresh.supersedes_id, command.pk)
        self.assertEqual(Command.objects.filter(order=command.order).count(), 2)
        command.refresh_from_db()
        self.assertEqual((str(command.key), command.body_digest, command.mission_id), original)
        self.assertNotEqual(fresh.key, command.key)
        self.assertEqual(commands._key(fresh), 'harbor:' + str(fresh.key))

    def test_close_cancelled_requires_operator_and_pending_cancellation(self):
        command = self.confirmed()
        with self.assertRaises(PermissionDenied):
            commands.close_cancelled(self.field, command_id=command.pk, expected_version=command.version)
        with self.assertRaises(Conflict):
            commands.close_cancelled(self.operator, command_id=command.pk, expected_version=command.version)
        commands.cancel_command(self.operator, command_id=command.pk, expected_version=command.version, reason='halt')
        command.refresh_from_db()
        closed = commands.close_cancelled(self.operator, command_id=command.pk, expected_version=command.version)
        self.assertEqual(closed.state, 'CANCELLED')
        with self.assertRaises(Conflict):
            commands.close_cancelled(self.operator, command_id=command.pk, expected_version=closed.version)

    def test_executor_ignores_terminal_history(self):
        command = self.confirmed()
        commands.cancel_command(self.operator, command_id=command.pk, expected_version=command.version, reason='halt')
        command.refresh_from_db()
        closed = commands.close_cancelled(self.operator, command_id=command.pk, expected_version=command.version)
        self.fleet.status = 'GOAL_CONFIRMED'
        result = executor.tick(registry=self.registry, evidence=test_executor.evidence_provider(timezone.now()),
                               now=timezone.now())
        self.assertNotIn(closed.pk, result['results'])

    def test_work_page_closes_a_pending_cancellation_for_operators_only(self):
        command = self.confirmed()
        commands.cancel_command(self.operator, command_id=command.pk, expected_version=command.version, reason='halt')
        command.refresh_from_db()
        self.client.force_login(self.field)
        response = self.client.post(f'/work/{command.pk}/', {'action': 'close', 'expected_version': command.version})
        self.assertEqual(response.status_code, 403)
        command.refresh_from_db()
        self.assertEqual(command.state, 'CANCEL_PENDING')
        self.client.force_login(self.operator)
        response = self.client.post(f'/work/{command.pk}/', {'action': 'close', 'expected_version': command.version})
        self.assertEqual(response.status_code, 302)
        command.refresh_from_db()
        self.assertEqual(command.state, 'CANCELLED')

    def test_closed_command_rejects_execution_operations_without_side_effects(self):
        command = self.confirmed()
        for state in ('CANCELLED', 'COMPLETED', 'BLOCKED'):
            for operation in ('submit', 'reconcile', 'resume', 'cancel'):
                with self.subTest(state=state, operation=operation):
                    Command.objects.filter(pk=command.pk).update(state=state)
                    command.refresh_from_db()
                    before = (command.version, command.mission_id, command.reason)
                    row = self.registry.ledger.read(commands._key(command))
                    calls = list(self.fleet.calls)
                    counts = (Audit.objects.count(), CommandFact.objects.count())
                    args = dict(command_id=command.pk, expected_version=command.version)
                    with self.assertRaises(Conflict):
                        if operation == 'cancel':
                            commands.cancel_command(self.operator, reason='closed work', **args)
                        elif operation == 'submit':
                            commands.submit_command(self.operator, **args)
                        else:
                            commands.reconcile_command(self.operator, resume=operation == 'resume', **args)
                    command.refresh_from_db()
                    self.assertEqual(command.state, state)
                    self.assertEqual((command.version, command.mission_id, command.reason), before)
                    self.assertEqual(self.registry.ledger.read(commands._key(command)), row)
                    self.assertEqual(self.fleet.calls, calls)
                    self.assertEqual((Audit.objects.count(), CommandFact.objects.count()), counts)

    def test_cancelled_work_cannot_be_reopened_by_a_post(self):
        command = self.confirmed()
        cancelled = commands.cancel_command(self.operator, command_id=command.pk,
            expected_version=command.version, reason='Operator stop')
        closed = commands.close_cancelled(self.operator, command_id=command.pk,
            expected_version=cancelled.version)
        self.client.force_login(self.operator)
        before = list(self.fleet.calls)
        for action in ('submit', 'reconcile', 'cancel'):
            with self.subTest(action=action):
                response = self.client.post(f'/work/{closed.pk}/', dict(action=action,
                    expected_version=closed.version, reason='Already closed'))
                self.assertEqual(response.status_code, 409)
        closed.refresh_from_db()
        self.assertEqual(closed.state, 'CANCELLED')
        self.assertEqual(self.registry.ledger.read(commands._key(closed))['state'], 'HOLD')
        self.assertEqual(self.fleet.calls, before)

    def test_final_stage_and_completed_history_have_no_current_job(self):
        command = self.confirmed()
        self.fleet.status = 'GOAL_CONFIRMED'

        def stage_evidence(command, row, payload, phase):
            if phase == 'step':
                return test_executor.evidence_provider(timezone.now())(command, row, payload, phase)
            return None

        for _ in range(8):
            executor.tick(registry=self.registry, evidence=stage_evidence, now=timezone.now())
            if self.registry.ledger.read(commands._key(command))['state'] == 'AWAITING_FINAL_EVIDENCE':
                break
        self.client.force_login(self.operator)
        for state in ('AWAITING_FINAL_EVIDENCE', 'COMPLETED'):
            if state == 'COMPLETED':
                executor.tick(registry=self.registry, evidence=test_executor.evidence_provider(timezone.now()),
                    now=timezone.now())
            command.refresh_from_db()
            before = self.registry.ledger.read(commands._key(command))
            self.assertEqual(before['state'], state)
            self.assertEqual(before['step_index'], len(command.binding['moves']))
            self.assertIsNone(before['mission_id'])
            calls = list(self.fleet.calls)
            detail = commands.command_detail(command)
            self.assertEqual(detail['ledger']['state'], state)
            self.assertIsNone(detail['job'])
            self.assertIsNone(detail['receipt'])
            for url in (f'/work/{command.pk}/', f'/requests/{command.order_id}/'):
                self.assertEqual(self.client.get(url).status_code, 200)
            self.assertEqual(self.registry.ledger.read(commands._key(command)), before)
            self.assertEqual(self.fleet.calls, calls)
        self.assertEqual(command.state, 'COMPLETED')
        self.assertTrue(CommandFact.objects.filter(command=command).exists())
