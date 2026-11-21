from datetime import datetime, timedelta, timezone as dt_timezone
from unittest.mock import patch

from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.urls import resolve
from django.utils import timezone

from harbor_web.models import Audit, Command, Order, Plan
from harbor_web.tests import test_services as fixtures


class WorkHubTests(TestCase):
    setUp = fixtures.ServicesTests.setUp
    cargo = fixtures.ServicesTests.cargo
    condition = fixtures.ServicesTests.condition

    def order(self, code=None, *, kind='OUTBOUND', group=1, sequence=1, policy='FIXED'):
        return Order.objects.create(created_by=self.operator, cargo=self.cargo(code) if code else None,
            kind=kind, priority_group=group, sequence=sequence, tie_policy=policy)

    def plan(self, order, *, result=None, version=1):
        from harbor_web.models import Observation
        condition = self.condition()
        observation = Observation.objects.create(created_by=self.admin, condition=condition,
            source_id='test', source_revision=str(Plan.objects.count()), observed_at=self.now,
            provenance='SYNTHETIC', payload={})
        return Plan.objects.create(created_by=self.operator, order=order, order_version=version,
            condition=condition, observation=observation, queue_digest='test',
            result=result or {'status': 'READY'})

    def command(self, order, state, *, plan=None):
        return Command.objects.create(order=order, plan=plan or self.plan(order), actor=self.operator,
            plan_version=1, ledger_identity='test', fleet_origin='test', fleet_principal='test',
            body_digest='a' * 64, binding={}, state=state)

    def completion(self, command, at):
        with patch('django.utils.timezone.now', return_value=at):
            Audit.objects.create(actor=self.operator, action='command.completed',
                object_type='Command', object_id=str(command.pk))

    def get_hub(self):
        self.client.force_login(self.operator)
        return self.client.get('/work/')

    def test_empty_hub_has_four_sections_and_no_estimates_or_actions(self):
        response = self.get_hub()
        for text in ('<h1>작업</h1>', '오늘 시간표', '반출 순서', '입고 자리', '자동 재배치',
                     '등록된 작업이 없습니다', '상태는 ROSY 실행 결과로 바뀜', '연동 전'):
            self.assertContains(response, text)
        body = response.content.decode().split('<main', 1)[1]
        for text in ('type="submit"', 'draggable=', '예정 시각', '희망 시각', '진행률', 'READY', 'HOLD', '#번호'):
            self.assertNotIn(text, body)
        self.assertRegex(body, r'<details[^>]*>\s*<summary>자동 재배치')

    def test_today_uses_first_completion_per_order_and_local_midnight(self):
        now = datetime(2026, 11, 21, 3, tzinfo=dt_timezone.utc)
        midnight = now.replace(hour=0) - timedelta(hours=9)
        old = self.command(self.order('OLD'), 'COMPLETED')
        today = self.command(self.order('TODAY', sequence=2), 'COMPLETED')
        unknown = self.command(self.order('NO-DATE', sequence=3), 'COMPLETED')
        future = self.command(self.order('FUTURE', sequence=4), 'COMPLETED')
        self.completion(old, midnight - timedelta(microseconds=1))
        self.completion(old, now)
        self.completion(today, midnight)
        retry = self.command(old.order, 'COMPLETED')
        self.completion(retry, now)
        self.completion(future, now + timedelta(seconds=1))
        Command.objects.filter(pk=today.pk).update(updated_at=now + timedelta(days=1))
        with timezone.override('Asia/Seoul'), patch('django.utils.timezone.now', return_value=now):
            response = self.get_hub()
        rows = response.context['timeline']
        self.assertEqual([(r['order'].pk, r['recorded_at']) for r in rows], [(today.order_id, midnight)])
        self.assertContains(response, '00:00')
        self.assertNotContains(response, 'NO-DATE')
        self.assertEqual(unknown.state, 'COMPLETED')

    def test_pending_order_and_outbound_chips_follow_queue_and_never_claim_any_is_optimal(self):
        later = self.order('LATER', group=3)
        any_order = self.order('ANY', group=2, policy='ANY')
        urgent = self.order('URGENT')
        running = self.order('RUNNING', group=3, sequence=2)
        self.command(running, 'RECONCILED')
        incoming = self.order('IN', kind='INBOUND')
        rearrange = self.order(kind='REARRANGE')
        done = self.command(self.order('DONE', group=3, sequence=3), 'COMPLETED')
        response = self.get_hub()
        rows = response.context['timeline']
        self.assertEqual([r['order'].pk for r in rows],
            [running.pk, urgent.pk, incoming.pk, rearrange.pk, any_order.pk, later.pk])
        self.assertTrue(all(r['recorded_at'] is None for r in rows))
        self.assertEqual([r['order'].pk for r in response.context['outbound']],
            [urgent.pk, any_order.pk, later.pk, running.pk])
        self.assertNotIn(done.order_id, [r['order'].pk for r in rows])
        self.assertContains(response, '같은 우선순위 안 순서 미지정')
        self.assertNotContains(response, '알고리즘이 정한 순서')

    def test_latest_state_and_existing_detail_links(self):
        order = self.order('RETRY')
        self.command(order, 'CANCELLED')
        active = self.command(order, 'DISPATCHING')
        response = self.get_hub()
        row = response.context['timeline'][0]
        self.assertEqual(row['state'], '진행 중')
        self.assertEqual(row['href'], f'/work/{active.pk}/')
        self.assertContains(response, f'/work/{active.pk}/')
        self.assertEqual(resolve(f'/work/{active.pk}/').url_name, 'work_detail')
        self.assertEqual(resolve('/work/n' + '0' * 32 + '/').url_name, 'notification_work')

    def test_completion_waits_for_next_work_without_a_plan_but_not_while_running(self):
        done = self.command(self.order('DONE'), 'COMPLETED')
        self.completion(done, timezone.now())
        next_order = self.order('NEXT', sequence=2)
        response = self.get_hub()
        self.assertEqual(response.context['timeline'][0]['state'], '완료 · 다음 작업 대기 중')
        self.command(next_order, 'DISPATCHING')
        self.assertEqual(self.get_hub().context['timeline'][0]['state'], '완료')

    def test_query_count_does_not_grow_with_work_or_plan_history(self):
        from harbor_web.work_hub import hub_context
        first = self.order('FIRST')
        self.plan(first)
        with self.assertNumQueries(4):
            hub_context()
        for number in range(2, 8):
            order = self.order(f'C{number}', sequence=number)
            self.plan(order)
            self.plan(order)
            self.command(order, 'PREPARED')
        with self.assertNumQueries(4):
            context = hub_context()
        self.assertEqual(len(context['timeline']), 7)

    def test_rearrangement_summary_uses_current_saved_permission(self):
        from harbor_web.models import RearrangeAuthorization
        current = RearrangeAuthorization.objects.create(created_by=self.operator, k=2, max_moves=4,
            daily_limit=7, valid_until=self.now + timedelta(hours=1))
        RearrangeAuthorization.objects.create(created_by=self.operator, k=2, max_moves=9,
            daily_limit=99, valid_until=self.now - timedelta(seconds=1))
        response = self.get_hub()
        self.assertEqual(response.context['authorization'], current)
        self.assertContains(response, '이동 최대 4개 · 하루 최대 7건')
        self.assertNotContains(response, '자동 실행 끄기')

    def test_old_completed_work_does_not_load_plan_payloads_even_with_a_repeated_audit_today(self):
        from harbor_web.work_hub import hub_context
        old = self.command(self.order('OLD'), 'COMPLETED')
        self.completion(old, self.now - timedelta(days=1))
        self.completion(old, self.now)
        visible_plan = self.plan(self.order('NEXT', sequence=2))
        with patch.object(Plan, 'from_db', wraps=Plan.from_db) as loaded:
            context = hub_context()
        loaded_ids = {call.args[2][call.args[1].index('id')] for call in loaded.call_args_list}
        self.assertEqual(loaded_ids, {visible_plan.pk})
        self.assertEqual([row['order'].pk for row in context['timeline']], [visible_plan.order_id])

    def test_inbound_only_shows_latest_current_recommendation_and_labels_it_as_proposed(self):
        incoming = self.order('IN', kind='INBOUND')
        result = {'status': 'READY', 'plan_kind': 'inbound-placement', 'recommendation': {
            'cargo_id': 'IN', 'area_id': 'yard', 'row': 2, 'column': 1, 'level': 3, 'orientation': 0}}
        self.plan(incoming, result=result)
        latest = self.plan(incoming, result={**result, 'recommendation': {
            **result['recommendation'], 'row': 1}})
        response = self.get_hub()
        self.assertEqual(response.context['inbound'][0]['place'], '1행 3층')
        self.assertContains(response, '추천 자리 · 실제 적재는 실행 후 반영')
        self.assertContains(response, f'/plans/{latest.pk}/')
        self.plan(incoming, result={'status': 'HOLD'})
        response = self.get_hub()
        self.assertEqual(response.context['inbound'][0]['place'], None)
        self.assertContains(response, '자리 결정 대기')
        self.plan(incoming, result=result, version=2)
        self.assertEqual(self.get_hub().context['inbound'][0]['place'], None)

    def test_access_and_read_only_get_and_post(self):
        order = self.order('C1')
        self.command(order, 'UNKNOWN')
        before = (Order.objects.count(), Plan.objects.count(), Command.objects.count(), Audit.objects.count())
        viewer = User.objects.create_user('viewer')
        viewer.groups.add(Group.objects.get_or_create(name='viewer')[0])
        with patch('harbor.rosy_http.RosyClient._http', side_effect=AssertionError('GET contacted Fleet')):
            for user in (self.operator, self.admin, viewer):
                self.client.force_login(user)
                self.assertEqual(self.client.get('/work/').status_code, 200)
                self.assertEqual(self.client.post('/work/', {}).status_code, 405)
        self.client.force_login(self.field)
        self.assertEqual(self.client.get('/work/').status_code, 403)
        self.client.logout()
        self.assertEqual(self.client.get('/work/').status_code, 302)
        self.assertEqual(before, (Order.objects.count(), Plan.objects.count(), Command.objects.count(), Audit.objects.count()))
