"""Design 32 R2 execution layer: standing authorization and the automatic check."""
from unittest.mock import patch

from django.contrib.auth.models import Group, User
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase
from django.utils import timezone

from harbor_web import executor, rearrangement, services
from harbor_web.models import Audit, CargoFootprint, Command, Order, Plan, RearrangeAuthorization
from harbor_web.tests import test_multicell_services as multicell


class RearrangementExecutionTests(TestCase):
    cargo = multicell.MulticellServicesTests.cargo
    observe = multicell.MulticellServicesTests.observe
    payload = multicell.MulticellServicesTests.payload
    base_setup = multicell.MulticellServicesTests.setUp

    def condition(self):
        cells = [dict(bay_id=f'Y{r}{c}', row=r, column=c) for r in range(1, 4) for c in range(1, 4)]
        return services.register_condition(self.admin, name='Grid', calibration_ref='declared', payload={
            'outbound_stand': 'out', 'temporary_bays': ['temp'], 'conveyor': 'belt',
            'allowed_moves': [['Y11', 'out']], 'max_observation_age_seconds': 60,
            'arm_reach': [['A', c['bay_id']] for c in cells] + [['A', 'temp'], ['A', 'out']],
            'yard_layout': dict(rows=3, columns=3, levels=3, cells=cells),
            'spatial_areas': [dict(area_id=a, rows=3, columns=3, levels=3) for a in ['temp', 'out']],
            'spatial_moves': [['yard', 'out'], ['yard', 'temp'], ['temp', 'yard'], ['yard', 'yard']]})

    def setUp(self):
        self.base_setup()
        self.early, self.late = self.cargo('early', 1), self.cargo('late', 1)
        self.condition_row = self.condition()
        self.finish_outbound(self.early, 1)
        self.finish_outbound(self.late, 2)
        self.observation = self.observe(self.condition_row, self.inverted())

    def place(self, cargo, column, level):
        return dict(cargo_id=cargo.code, area_id='yard', row=1, column=column, level=level, orientation=0,
                    footprint_revision=CargoFootprint.objects.get(cargo=cargo).pk)

    def inverted(self, holdings=()):
        # 'early' leaves first but 'late' sits on it.
        return dict(schema='grid-placement-v1', observed_areas=['yard', 'temp', 'out'], holdings=list(holdings),
                    placements=[self.place(self.early, 1, 1), self.place(self.late, 1, 2)])

    def finish_outbound(self, cargo, group):
        """A completed outbound order still ranks the cargo, but is no longer pending."""
        order = services.create_order(self.operator, cargo=cargo, kind='OUTBOUND', priority_group=group,
                                      sequence=1, tie_policy='FIXED')
        plan = services.make_plan(self.operator, order_id=order.pk, expected_version=1,
                                  observation_id=self.observe(self.condition_row, self.payload(cargo), f'seed{group}').pk,
                                  now=self.now)
        Command.objects.create(order=order, plan=plan, actor=self.operator, plan_version=1, state='COMPLETED',
            ledger_identity='t', fleet_origin='t', fleet_principal='t', body_digest='a' * 64, binding={})

    def authorize(self, **values):
        return rearrangement.enable_authorization(self.operator, now=self.now, **values)

    def holds(self):
        return list(Audit.objects.filter(action=rearrangement.HOLD_ACTION).values_list('detail__reason', flat=True))

    def run_ready(self):
        with patch('harbor_web.commands.capability', return_value=dict(enabled=True, reason='')), \
                patch('harbor_web.commands.prepare_command') as prepare, \
                patch('harbor_web.commands.submit_command', return_value='submitted') as submit:
            prepare.return_value.pk, prepare.return_value.version = 7, 1
            return rearrangement.auto_check(self.now), prepare, submit

    def assert_no_rearrangement(self):
        self.assertFalse(Order.objects.filter(kind='REARRANGE').exists())
        self.assertFalse(Command.objects.filter(order__kind='REARRANGE').exists())

    def test_ready_creates_one_rearrange_plan_with_the_record(self):
        auth = self.authorize()
        result, prepare, submit = self.run_ready()
        self.assertEqual(result, 'submitted')
        self.assertEqual(prepare.call_count, 1)
        self.assertEqual(submit.call_count, 1)
        plan = Plan.objects.get(order__kind='REARRANGE')
        self.assertEqual(Order.objects.filter(kind='REARRANGE').count(), 1)
        self.assertIsNone(plan.order.cargo_id)
        self.assertEqual(plan.result['status'], 'READY')
        self.assertEqual(plan.result['plan_kind'], 'rearrangement')
        self.assertEqual(plan.result['required_completion'], ['rearranged_at_targets', 'arms_retreated'])
        record = plan.result['rearrangement']
        self.assertEqual((record['authorization_id'], record['automatic']), (auth.pk, True))
        self.assertEqual((record['blocked_before'], record['blocked_after']), (1, 0))
        self.assertGreaterEqual(record['gain'], 1)
        self.assertEqual(record['move_count'], len(plan.result['moves']))
        self.assertTrue(all(move['phase_label'] == '재배치' for move in plan.result['moves']))
        # The same observation and queue are not planned twice.
        self.assertEqual(self.run_ready()[0], 'ALREADY_PLANNED')
        self.assertEqual(Plan.objects.filter(order__kind='REARRANGE').count(), 1)

    def test_blocked_execution_gate_leaves_plan_but_no_command(self):
        self.authorize()
        reason = rearrangement.auto_check(self.now)
        self.assertEqual(Plan.objects.filter(order__kind='REARRANGE').count(), 1)
        self.assertFalse(Command.objects.filter(order__kind='REARRANGE').exists())
        self.assertEqual(self.holds(), [reason])
        self.assertNotEqual(reason, '')

    def test_no_authorization_or_expired_or_disabled_starts_nothing(self):
        self.assertEqual(rearrangement.auto_check(self.now), 'AUTHORIZATION_OFF_OR_EXPIRED')
        self.assert_no_rearrangement()
        auth = self.authorize()
        later = auth.valid_until + timezone.timedelta(seconds=1)
        self.assertEqual(rearrangement.auto_check(later), 'AUTHORIZATION_OFF_OR_EXPIRED')
        rearrangement.disable_authorization(self.operator, now=self.now)
        self.assertEqual(rearrangement.auto_check(self.now), 'AUTHORIZATION_OFF_OR_EXPIRED')
        self.assert_no_rearrangement()
        self.assertEqual(set(self.holds()), {'AUTHORIZATION_OFF_OR_EXPIRED'})

    def test_daily_limit(self):
        self.authorize(daily_limit=1)
        self.run_ready()
        plan = Plan.objects.get(order__kind='REARRANGE')
        Command.objects.create(order=plan.order, plan=plan, actor=self.operator, plan_version=1, state='COMPLETED',
            ledger_identity='t', fleet_origin='t', fleet_principal='t', body_digest='b' * 64, binding={})
        self.observe(self.condition_row, self.inverted(), 'next')
        self.assertEqual(rearrangement.auto_check(self.now), 'DAILY_LIMIT_REACHED')
        self.assertEqual(Order.objects.filter(kind='REARRANGE').count(), 1)

    def test_pending_outbound_holds(self):
        self.authorize()
        services.create_order(self.operator, cargo=self.cargo('next', 1), kind='OUTBOUND', priority_group=3,
                              sequence=1, tie_policy='FIXED')
        self.assertEqual(rearrangement.auto_check(self.now), 'OUTBOUND_PENDING')
        self.assert_no_rearrangement()
        self.assertEqual(self.holds(), ['OUTBOUND_PENDING'])

    def test_other_active_command_holds(self):
        self.authorize()
        order = services.create_order(self.operator, cargo=self.cargo('busy', 1), kind='INBOUND', priority_group=1,
                                      sequence=1, tie_policy='FIXED')
        plan = services.make_plan(self.operator, order_id=order.pk, expected_version=1,
                                  observation_id=self.observation.pk, now=self.now)
        for state in ('UNKNOWN', 'RECONCILED'):
            command = Command.objects.create(order=order, plan=plan, actor=self.operator, plan_version=1, state=state,
                ledger_identity='t', fleet_origin='t', fleet_principal='t', body_digest='c' * 64, binding={})
            self.assertEqual(rearrangement.auto_check(self.now), 'OTHER_COMMAND_ACTIVE')
            Command.objects.filter(pk=command.pk).update(state='COMPLETED')
            command.refresh_from_db()
            plan = services.make_plan(self.operator, order_id=order.pk, expected_version=1,
                                      observation_id=self.observation.pk, now=self.now)
        self.assert_no_rearrangement()

    def test_stale_observation_holds(self):
        self.authorize()
        later = self.now + timezone.timedelta(seconds=120)
        self.assertEqual(rearrangement.auto_check(later), 'OBSERVATION_NOT_CURRENT')
        self.assert_no_rearrangement()

    def test_revision_change_before_execution_holds(self):
        self.authorize()
        real = rearrangement.rearrange_proposal

        def proposal_then_newer_observation(observation, options, *, now):
            result = real(observation, options, now=now)
            self.observe(self.condition_row, self.inverted(), 'newer')
            return result

        with patch('harbor_web.rearrangement.rearrange_proposal', proposal_then_newer_observation):
            self.assertEqual(rearrangement.auto_check(self.now), 'OBSERVATION_REVISION_CHANGED')
        self.assert_no_rearrangement()

    def test_holding_holds(self):
        self.authorize()
        held = self.cargo('held', 1)
        self.observe(self.condition_row, self.inverted([dict(cargo_id=held.code, holder_id='A')]), 'holding')
        self.assertEqual(rearrangement.auto_check(self.now), 'PHYSICAL_HOLDING_REQUIRES_RECONCILIATION')
        self.assert_no_rearrangement()

    def test_calculation_hold_is_recorded_once(self):
        self.authorize()
        self.observe(self.condition_row, dict(self.inverted(), placements=[self.place(self.early, 1, 1)]), 'sorted')
        for _ in range(2):
            self.assertEqual(rearrangement.auto_check(self.now), 'REARRANGE_NOT_WORTH')
        self.assertEqual(self.holds(), ['REARRANGE_NOT_WORTH'])
        self.assert_no_rearrangement()

    def test_authorization_history_is_append_only_and_operator_only(self):
        first = self.authorize(k=2)
        second = self.authorize(k=3)
        first.refresh_from_db()
        self.assertIsNotNone(first.disabled_at)
        self.assertEqual(first.disabled_by, self.operator)
        self.assertEqual((rearrangement.current_authorization(self.now).pk, second.k), (second.pk, 3))
        first.k = 5
        with self.assertRaises(ValidationError):
            first.save()
        with self.assertRaises(ValidationError):
            second.delete()
        for bad in (dict(k=0), dict(max_moves=13), dict(daily_limit=0)):
            with self.assertRaises(ValidationError):
                self.authorize(**bad)
        with self.assertRaises(PermissionDenied):
            rearrangement.enable_authorization(self.admin)
        with self.assertRaises(PermissionDenied):
            rearrangement.disable_authorization(self.admin)
        rearrangement.disable_authorization(self.operator, now=self.now)
        self.assertIsNone(rearrangement.current_authorization(self.now))
        self.assertEqual(RearrangeAuthorization.objects.count(), 2)
        self.assertEqual(Audit.objects.filter(action='rearrange.authorization.enable').count(), 2)

    def test_rearrange_orders_do_not_change_the_outbound_queue_digest(self):
        before = services.queue_digest()
        rearrangement.request_rearrangement(self.operator, observation_id=self.observation.pk, now=self.now)
        self.assertEqual(services.queue_digest(), before)

    def test_manual_request_is_not_automatic_and_has_no_command(self):
        plan = rearrangement.request_rearrangement(self.operator, observation_id=self.observation.pk, now=self.now)
        self.assertFalse(plan.result['rearrangement']['automatic'])
        self.assertIsNone(plan.result['rearrangement']['authorization_id'])
        self.assertFalse(Command.objects.exists() and Command.objects.filter(order__kind='REARRANGE').exists())
        with self.assertRaises(PermissionDenied):
            rearrangement.request_rearrangement(self.admin, observation_id=self.observation.pk)

    def test_idle_executor_runs_the_check_but_a_busy_one_does_not(self):
        with patch('harbor_web.runtime.execution_block_reason', return_value=''), \
                patch('harbor_web.rearrangement.auto_check') as check:
            executor.tick(registry=object(), now=self.now)
            self.assertEqual(check.call_count, 1)
            order = Order.objects.filter(kind='OUTBOUND').first()
            plan = Plan.objects.filter(order=order).first()
            Command.objects.filter(order=order).update(state='RECONCILED')
            with patch('harbor_web.commands.advance_command', return_value='RECONCILED'):
                executor.tick(registry=object(), now=self.now)
            self.assertEqual(check.call_count, 1)

    def test_page_shows_state_and_toggles(self):
        self.client.force_login(self.operator)
        self.assertContains(self.client.get('/rearrangement/'), '꺼짐 또는 만료')
        response = self.client.post('/rearrangement/', dict(action='enable', k=2, max_moves=6, hours=8, daily_limit=20))
        self.assertRedirects(response, '/rearrangement/', fetch_redirect_response=False)
        self.assertContains(self.client.get('/rearrangement/'), '켜짐')
        self.client.post('/rearrangement/', dict(action='disable'))
        self.assertContains(self.client.get('/rearrangement/'), '꺼짐 또는 만료')
        self.client.force_login(self.admin)
        self.assertEqual(self.client.post('/rearrangement/', dict(action='disable')).status_code, 403)

    def test_page_marks_automatic_plans_and_detail_page_renders(self):
        self.authorize()
        self.run_ready()
        plan = Plan.objects.get(order__kind='REARRANGE')
        self.client.force_login(self.operator)
        self.assertContains(self.client.get('/rearrangement/'), f'자동 실행 · 허용 #{plan.result["rearrangement"]["authorization_id"]}')
        self.assertEqual(self.client.get(f'/plans/{plan.pk}/').status_code, 200)
        Command.objects.create(order=plan.order, plan=plan, actor=self.operator, plan_version=1, state='UNKNOWN',
            ledger_identity='t', fleet_origin='t', fleet_principal='t', body_digest='d' * 64, binding={})
        self.assertContains(self.client.get('/work/'), f'/work/{plan.command.pk}/')
