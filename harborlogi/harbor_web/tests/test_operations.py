from datetime import timedelta
from unittest.mock import patch

from django.template.loader import render_to_string
from django.test import TestCase

from harbor_web import rearrangement, services
from harbor_web.models import Audit, Command, Plan
from harbor_web.operations import operations_context
from harbor_web.tests import test_multicell_services as spatial_fixtures
from harbor_web.tests import test_rearrangement_execution as rearrangement_fixtures
from harbor_web.tests import test_services as fixtures
from harbor_web.tests import test_workspace as workspace_fixtures


class OperationsTests(TestCase):
    setUp = fixtures.ServicesTests.setUp
    cargo = fixtures.ServicesTests.cargo
    condition = fixtures.ServicesTests.condition
    observation = fixtures.ServicesTests.observation
    order = fixtures.ServicesTests.order
    buried_plan = workspace_fixtures.WorkspaceTests.buried_plan

    def test_missing_observation_is_unknown_but_registered_orders_are_global(self):
        self.order(kind='INBOUND')
        context = operations_context(None)
        self.assertIsNone(context['inventory']['cargo_count'])
        self.assertIsNone(context['workload']['move_count'])
        self.assertEqual(context['orders']['inbound'], 1)
        self.assertEqual(context['orders']['outbound'], 0)
        self.assertEqual(context['actual']['completed'], 0)
        html = render_to_string('operations_summary.html', dict(operations=context))
        self.assertIn('현황 없음', html)
        self.assertIn('미집계', html)

    def test_latest_plan_once_and_rehandle_counts_cargo_not_round_trip_moves(self):
        original = self.buried_plan()
        for _ in range(5):
            latest = services.make_plan(self.operator, order_id=original.order_id,
                expected_version=1, observation_id=original.observation_id)
        before = (Audit.objects.count(), Plan.objects.count(), Command.objects.count())
        with patch.object(Plan, 'from_db', wraps=Plan.from_db) as loaded_plan:
            context = operations_context(original.observation, now=self.now)
        self.assertEqual(loaded_plan.call_count, 1)
        self.assertEqual(before, (Audit.objects.count(), Plan.objects.count(), Command.objects.count()))
        self.assertEqual(context['inventory']['cargo_count'], 2)
        self.assertEqual(context['inventory']['capacity'], 6)
        self.assertFalse(context['inventory']['spatial'])
        workload = context['workload']
        self.assertEqual([r['plan'].pk for r in workload['plans']], [latest.pk])
        self.assertEqual(workload['move_count'], 3)
        self.assertEqual(workload['rehandled_cargo_count'], 1)
        self.assertEqual(workload['phase_counts'], {'재취급': 1, '반출': 1, '복귀': 1, '입고': 0})
        self.assertIn('적재 자리', render_to_string('operations_summary.html', dict(operations=context)))

    def test_stale_superseded_observations_exclude_ready_moves(self):
        plan = self.buried_plan()
        stale = operations_context(plan.observation, now=self.now + timedelta(seconds=61))
        self.assertTrue(stale['freshness']['stale'])
        self.assertEqual(stale['workload']['move_count'], 0)
        self.assertEqual(stale['workload']['excluded_count'], 1)
        services.record_observation(self.admin, source_id=plan.observation.source_id,
            source_revision='new', observed_at=self.now, provenance='DECLARED',
            condition=plan.condition, payload=plan.observation.payload)
        self.assertTrue(operations_context(plan.observation, now=self.now)['freshness']['superseded'])
        self.assertEqual(operations_context(plan.observation, now=self.now)['workload']['move_count'], 0)

    def test_changed_order_revision_excludes_previous_ready_metrics(self):
        plan = self.buried_plan()
        services.update_order(self.operator, order_id=plan.order_id, expected_version=1,
            priority_group=2, sequence=1, tie_policy='FIXED', note='Changed')
        context = operations_context(plan.observation, now=self.now)
        self.assertTrue(context['workload']['plans'][0]['stale'])
        self.assertEqual(context['workload']['move_count'], 0)
        self.assertEqual(context['workload']['rehandled_cargo_count'], 0)

    def test_other_source_current_plan_does_not_fall_back_to_old_selected_plan(self):
        plan = self.buried_plan()
        other = self.observation(source_id='other', source_revision='other', condition=plan.condition,
                                 payload=plan.observation.payload)
        services.make_plan(self.operator, order_id=plan.order_id, expected_version=1, observation_id=other.pk)
        context = operations_context(plan.observation, now=self.now)
        self.assertEqual(context['workload']['plans'], [])
        self.assertEqual(context['orders']['outbound'], 1)
        self.assertFalse(context['freshness']['superseded'])

    def test_handoff_keeps_original_plan_separate_from_newer_proposal(self):
        original = self.buried_plan()
        Command.objects.create(order=original.order, plan=original, actor=self.operator,
            plan_version=1, ledger_identity='test', fleet_origin='http://127.0.0.1:9999',
            fleet_principal='test', body_digest='a' * 64, binding={}, state='UNKNOWN')
        services.make_plan(self.operator, order_id=original.order_id, expected_version=1,
                           observation_id=original.observation_id)
        context = operations_context(original.observation, now=self.now)
        self.assertEqual(context['handoffs'][0]['plan'].pk, original.pk)
        self.assertEqual(context['handoffs'][0]['label'], '결과 불명')
        self.assertEqual(context['workload']['plans'], [])
        self.assertEqual(context['workload']['move_count'], 0)
        self.assertEqual(context['actual']['completed'], 0)

    def test_planned_rehandle_ratio_and_arms_stay_apart_from_actuals(self):
        plan = self.buried_plan()
        context = operations_context(plan.observation, now=self.now)
        workload = context['workload']
        self.assertEqual((workload['outbound_rehandle_moves'], workload['outbound_plan_count']), (1, 1))
        self.assertEqual(workload['rehandle_per_outbound'], 1.0)
        self.assertEqual(workload['outbound_rehandle_plans'], 1)
        arms = {m['primary_arm'] for m in plan.result['moves']}
        self.assertEqual(workload['plans'][0]['primary_arms'], sorted(arms))
        self.assertNotIn('S', workload['plans'][0]['primary_arms'] + workload['plans'][0]['alternate_arms'])
        self.assertEqual(context['actual']['completed'], 0)
        self.assertEqual(context['attention'], [])
        self.assertEqual(context['inventory']['occupancy_percent'], round(2 * 100 / 6))
        html = render_to_string('operations_summary.html', dict(operations=context))
        for text in ('확인이 필요한 것 0건', f'야드 현황 #{plan.observation.pk} 기준', '재취급이 필요한 반출',
                     '1<small> / 계획된 반출 1건</small>', '지시 전 계획 · 실적 아님', '계획한 재취급 이동 1회', '공급암 S',
                     '계획한 팔(주·대안)', '미집계', '>점유율<'):
            self.assertIn(text, html)
        for text in ('반출 1건당', '관측 점유율', '선택 관측'):
            self.assertNotIn(text, html)

    def test_stale_observation_is_flagged_and_has_no_ratio(self):
        plan = self.buried_plan()
        context = operations_context(plan.observation, now=self.now + timedelta(seconds=61))
        self.assertEqual([a['label'] for a in context['attention']], ['관측 유효 시간 밖'])
        self.assertIsNone(context['workload']['rehandle_per_outbound'])
        html = render_to_string('operations_summary.html', dict(operations=context))
        self.assertIn('유효한 반출 계획 없음', html)
        self.assertIn('야드 현황 다시 확인', html)
        services.record_observation(self.admin, source_id=plan.observation.source_id,
            source_revision='new', observed_at=self.now, provenance='DECLARED',
            condition=plan.condition, payload=plan.observation.payload)
        superseded = operations_context(plan.observation, now=self.now)
        self.assertEqual([a['label'] for a in superseded['attention']], ['같은 출처에 새 관측 있음'])

    def test_missing_observation_is_flagged_not_reported_valid(self):
        context = operations_context(None)
        self.assertEqual([a['label'] for a in context['attention']], ['관측 없음'])
        html = render_to_string('operations_summary.html', dict(operations=context))
        self.assertIn('확인이 필요한 것 1건', html)
        self.assertNotIn('관측 유효', html)

    def test_unknown_handoff_asks_for_original_reconciliation_only(self):
        plan = self.buried_plan()
        command = Command.objects.create(order=plan.order, plan=plan, actor=self.operator,
            plan_version=1, ledger_identity='test', fleet_origin='http://127.0.0.1:9999',
            fleet_principal='test', body_digest='a' * 64, binding={}, state='UNKNOWN')
        context = operations_context(plan.observation, now=self.now)
        self.assertEqual(context['attention'], [dict(label=f'전체 인계 · 결과 불명 · {plan.order.cargo.code}', count=1,
                                                     action='원래 작업 대조', href=f'/work/{command.pk}/')])
        for state, flagged in [('DISPATCHING', True), ('CANCEL_PENDING', True), ('BLOCKED', True),
                               ('PREPARED', False), ('RECONCILED', False)]:
            Command.objects.filter(pk=command.pk).update(state=state)
            self.assertEqual(bool(operations_context(plan.observation, now=self.now)['attention']), flagged, state)
        Command.objects.filter(pk=command.pk).update(state='UNKNOWN')
        html = render_to_string('operations_summary.html', dict(operations=context))
        self.assertIn('확인이 필요한 것 1건', html)
        self.assertNotIn('재전송', html)

    def test_hold_has_no_partial_workload_and_holdings_are_separate(self):
        order = self.order()
        self.cargo('C2')
        observation = self.observation(payload=dict(bays=[dict(bay_id='bay', cargo=['C1'], capacity=2),
            dict(bay_id='temp', cargo=[], capacity=2), dict(bay_id='out', cargo=[], capacity=1)],
            holdings=[dict(cargo_id='C2', holder_id='A')]))
        services.make_plan(self.operator, order_id=order.pk, expected_version=1, observation_id=observation.pk)
        context = operations_context(observation, now=self.now)
        self.assertEqual(context['inventory']['cargo_count'], 1)
        self.assertEqual(context['inventory']['holding_count'], 1)
        self.assertEqual(context['workload']['hold_count'], 1)
        self.assertEqual(context['workload']['move_count'], 0)

    def handoff(self, plan, state, binding):
        return Command.objects.create(order=plan.order, plan=plan, actor=self.operator, plan_version=1,
            ledger_identity='test', fleet_origin='http://127.0.0.1:9999', fleet_principal='test',
            body_digest='a' * 64, binding=binding, state=state)

    def test_only_completed_handoffs_count_executed_rehandles(self):
        plan = self.buried_plan()
        outbound = plan.order.cargo.code
        binding = dict(outbound_id=outbound, moves=[dict(cargo_id=m['cargo_id']) for m in plan.result['moves']])
        command = self.handoff(plan, 'UNKNOWN', binding)
        self.assertEqual(operations_context(plan.observation, now=self.now)['actual']['completed'], 0)
        Command.objects.filter(pk=command.pk).update(state='COMPLETED')
        inbound = self.order(self.cargo('C9'), kind='INBOUND')
        self.handoff(services.make_plan(self.operator, order_id=inbound.pk, expected_version=1,
                                        observation_id=plan.observation_id), 'CANCELLED', {})
        actual = operations_context(plan.observation, now=self.now)['actual']
        # Buried outbound: one cargo moved off before the outbound move, then returned.
        self.assertEqual(actual, dict(completed=1, human_attested=0, inbound=0, outbound=1, rearrange=0,
                                      outbound_rehandle_moves=1, unmatched=[],
                                      throughput=dict(window_hours=24, count=0, span_minutes=0, per_hour=None)))
        html = render_to_string('operations_summary.html',
                                dict(operations=operations_context(plan.observation, now=self.now)))
        for text in ('실제 완료량·처리율', '1<small> 건 완료', '실행된 재취급 이동 1회', '처리율 미집계'):
            self.assertIn(text, html)
        # A newer completion of the same order replaces it; it bound only the outbound move.
        newer = services.make_plan(self.operator, order_id=plan.order_id, expected_version=1,
                                   observation_id=plan.observation_id)
        self.handoff(newer, 'COMPLETED', dict(outbound_id=outbound, moves=[dict(cargo_id=outbound)]))
        actual = operations_context(plan.observation, now=self.now)['actual']
        self.assertEqual((actual['completed'], actual['outbound'], actual['outbound_rehandle_moves']), (1, 1, 0))

    def test_overview_puts_finished_work_last_and_links_cancelled_work_to_review(self):
        plan = self.buried_plan()
        other = self.order(self.cargo('C8'), sequence=5)
        other_plan = services.make_plan(self.operator, order_id=other.pk, expected_version=1, observation_id=plan.observation_id)
        self.handoff(plan, 'COMPLETED', {})
        self.handoff(other_plan, 'CANCELLED', {})
        self.client.force_login(self.operator)
        rows = self.client.get('/').context['rows']
        self.assertEqual([r['order'].pk for r in rows][-1], plan.order_id)
        finished, cancelled = rows[-1], next(r for r in rows if r['order'].pk == other.pk)
        self.assertEqual((finished['status_label'], finished['action']), ('업무 완료', '완료 기록 보기'))
        self.assertTrue(finished['href'].startswith(f'/requests/{plan.order_id}/timeline/'))
        self.assertEqual((cancelled['status_label'], cancelled['action']), ('취소됨', '최신 기준으로 검토'))
        self.assertTrue(cancelled['href'].startswith(f'/requests/{other.pk}/'))

    def completed_at(self, *ages):
        """One completed order per age, each saved through the real path so its audit row has that time."""
        from harbor_web.commands import _save
        plan = self.buried_plan()
        for index, age in enumerate(ages):
            order = plan.order if index == 0 else self.order(self.cargo(f'T{index}'), sequence=10 + index)
            source = plan if index == 0 else services.make_plan(
                self.operator, order_id=order.pk, expected_version=1, observation_id=plan.observation_id)
            command = self.handoff(source, 'RECONCILED', dict(outbound_id=order.cargo.code, moves=[]))
            with patch('django.utils.timezone.now', return_value=self.now - age):
                _save(command, self.operator, 'COMPLETED')
        return operations_context(plan.observation, now=self.now)['actual']['throughput']

    def test_throughput_spans_first_to_last_completion_inside_the_window(self):
        throughput = self.completed_at(timedelta(hours=30), timedelta(minutes=40), timedelta(minutes=1))
        self.assertEqual(throughput, dict(window_hours=24, count=2, span_minutes=39, per_hour=round(1 / (39 / 60), 1)))

    def test_window_edges_and_a_single_completion(self):
        edge = self.completed_at(timedelta(hours=24), timedelta(0))
        self.assertEqual((edge['count'], edge['span_minutes'], edge['per_hour']), (2, 1440, round(1 / 24, 1)))

    def test_single_completion_has_count_without_rate(self):
        throughput = self.completed_at(timedelta(minutes=30))
        self.assertEqual((throughput['count'], throughput['per_hour']), (1, None))

    def test_short_span_is_not_turned_into_an_hourly_rate(self):
        throughput = self.completed_at(timedelta(minutes=5), timedelta(minutes=1))
        self.assertEqual((throughput['count'], throughput['span_minutes'], throughput['per_hour']), (2, 4, None))


class RearrangementOperationsTests(TestCase):
    """A REARRANGE order has no cargo (design 32); the summary must still render it."""
    setUp = rearrangement_fixtures.RearrangementExecutionTests.setUp
    base_setup = rearrangement_fixtures.RearrangementExecutionTests.base_setup
    cargo = rearrangement_fixtures.RearrangementExecutionTests.cargo
    observe = rearrangement_fixtures.RearrangementExecutionTests.observe
    payload = rearrangement_fixtures.RearrangementExecutionTests.payload
    condition = rearrangement_fixtures.RearrangementExecutionTests.condition
    place = rearrangement_fixtures.RearrangementExecutionTests.place
    inverted = rearrangement_fixtures.RearrangementExecutionTests.inverted
    finish_outbound = rearrangement_fixtures.RearrangementExecutionTests.finish_outbound

    def test_rearrange_plan_handoff_and_completion_render(self):
        plan = rearrangement.request_rearrangement(self.operator, observation_id=self.observation.pk, now=self.now)
        context = operations_context(self.observation, now=self.now)
        self.assertEqual([r['plan'].pk for r in context['workload']['plans']], [plan.pk])
        self.assertEqual(context['workload']['outbound_plan_count'], 0)
        command = Command.objects.create(order=plan.order, plan=plan, actor=self.operator, plan_version=1,
            ledger_identity='t', fleet_origin='t', fleet_principal='t', body_digest='a' * 64, binding={},
            state='UNKNOWN')
        context = operations_context(self.observation, now=self.now)
        self.assertIn(f'전체 인계 · 결과 불명 · 재배치 요청 #{plan.order_id}', [a['label'] for a in context['attention']])
        self.assertIn(f'재배치 요청 #{plan.order_id}', render_to_string('operations_summary.html', dict(operations=context)))
        Command.objects.filter(pk=command.pk).update(state='COMPLETED')
        context = operations_context(self.observation, now=self.now)
        actual = context['actual']
        # setUp finished two outbound orders whose bindings carry no moves: unknown, never zero.
        self.assertEqual((actual['completed'], actual['outbound'], actual['rearrange'], actual['outbound_rehandle_moves']),
                         (3, 2, 1, None))
        flagged = [a for a in context['attention'] if a['label'].startswith('완료 인계 · 반출 이동 대조 불가')]
        self.assertEqual(sorted(a['href'] for a in flagged), sorted(f'/work/{c.pk}/' for c in actual['unmatched']))
        self.assertEqual(len(flagged), 2)
        self.assertIn('실행된 재취급 이동 미집계(대조 불가 2건)',
                      render_to_string('operations_summary.html', dict(operations=context)))


class SpatialOperationsTests(TestCase):
    setUp = spatial_fixtures.MulticellServicesTests.setUp
    cargo = spatial_fixtures.MulticellServicesTests.cargo
    condition = spatial_fixtures.MulticellServicesTests.condition
    payload = spatial_fixtures.MulticellServicesTests.payload
    observe = spatial_fixtures.MulticellServicesTests.observe

    def test_multicell_footprint_counts_distinct_cargo_and_occupied_cells(self):
        cargo = self.cargo(length=3)
        held = self.cargo('held', length=1)
        temporary = self.cargo('temporary', length=2)
        outbound = self.cargo('outbound', length=1)
        from harbor_web.models import CargoFootprint
        payload = self.payload(cargo)
        for item, area in [(temporary, 'temp'), (outbound, 'out')]:
            payload['placements'].append(dict(cargo_id=item.code, area_id=area, row=1,
                column=1, level=1, orientation=0, footprint_revision=CargoFootprint.objects.get(cargo=item).pk))
        payload['holdings'] = [dict(cargo_id=held.code, holder_id='A')]
        observation = self.observe(self.condition(), payload)
        inventory = operations_context(observation, now=self.now)['inventory']
        self.assertEqual(inventory['cargo_count'], 3)
        self.assertEqual(inventory['occupied'], 6)
        self.assertEqual(inventory['capacity'], 81)
        self.assertEqual((inventory['storage_count'], inventory['temporary_count'], inventory['outbound_count']), (1, 1, 1))
        self.assertEqual(inventory['holding_count'], 1)
