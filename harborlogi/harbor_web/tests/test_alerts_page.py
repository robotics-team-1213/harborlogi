from contextlib import closing
from datetime import timedelta
import os
from pathlib import Path
import re
import sqlite3
import tempfile
from unittest.mock import patch

from django.contrib.auth.models import Group, User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from harbor.departure import DepartureLedger, record_release
from harbor_web import access, services
from harbor_web.alert_pages import release_overdue_minutes
from harbor_web.models import Audit, Command, DepartureIntent, FieldEpisode, Observation, Order, Plan
from harbor_web.tests import test_services as fixtures


class AlertsPageTests(TestCase):
    setUp = fixtures.ServicesTests.setUp
    cargo = fixtures.ServicesTests.cargo
    condition = fixtures.ServicesTests.condition
    observation = fixtures.ServicesTests.observation
    order = fixtures.ServicesTests.order

    def open(self, user=None, query=''):
        self.client.force_login(user or self.operator)
        return self.client.get('/alerts/' + query)

    def job(self, state=None):
        number = Order.objects.count() + 1
        order = self.order(self.cargo(f'C{number}'), sequence=number)
        observation = self.observation(code=order.cargo.code, source_revision=f'r{number}')
        plan = services.make_plan(self.operator, order_id=order.pk, expected_version=1,
                                  observation_id=observation.pk)
        if state is None:
            return plan
        return Command.objects.create(order=order, plan=plan, actor=self.operator, plan_version=1,
            ledger_identity='test', fleet_origin='test', fleet_principal='test',
            body_digest='a' * 64, binding={}, state=state)

    def test_registered_page_and_role_access(self):
        self.assertEqual(reverse('alerts'), '/alerts/')
        self.assertEqual(access.page('alerts').roles, access.EVERYONE)
        for role in access.EVERYONE:
            user = User.objects.create_user(f'alerts-{role}')
            user.groups.add(Group.objects.get_or_create(name=role)[0])
            self.assertEqual(self.open(user).status_code, 200, role)
        self.assertEqual(self.open(User.objects.create_user('nobody')).status_code, 403)
        inactive = User.objects.create_user('inactive', is_active=False)
        inactive.groups.add(Group.objects.get(name='viewer'))
        self.assertEqual(self.open(inactive).status_code, 302)
        self.client.logout()
        self.assertRedirects(self.client.get('/alerts/'), '/accounts/login/?next=/alerts/',
                             fetch_redirect_response=False)

    def test_missing_yard_is_an_alert_not_an_empty_success(self):
        response = self.open()
        self.assertContains(response, '야드 현황이 없습니다')
        self.assertContains(response, 'href="/inventory/"')
        self.assertNotContains(response, '처리할 것 없음 · 자동 운전 중')
        self.assertEqual(response.context['alert_total'], 1)

    def test_empty_with_fresh_yard(self):
        self.cargo()
        self.observation()
        response = self.open()
        self.assertContains(response, '처리할 것 없음 · 자동 운전 중')
        self.assertEqual(response.context['alerts'], [])
        self.assertEqual(response.context['alert_total'], 0)

    def test_problem_commands_once_and_other_states_not_problems(self):
        commands = [self.job(state) for state, _ in Command._meta.get_field('state').choices]
        response = self.open()
        rows = response.context['alerts']
        # An old completion with no outbound binding is separately flagged by operations_context.
        expected = {'UNKNOWN', 'BLOCKED', 'CANCEL_PENDING', 'COMPLETED'}
        for command in commands:
            href = f'/work/{command.pk}/'
            self.assertEqual(sum(row['href'] == href for row in rows), int(command.state in expected), command.state)
        for row in rows:
            if row['href']:
                self.assertEqual(self.client.get(row['href']).status_code, 200, row)

    def test_hold_plan_attention_keeps_count_and_opens_existing_destination(self):
        plan = self.job()
        Order.objects.filter(pk=plan.order_id).update(version=2)
        second = self.order(plan.order.cargo, sequence=2)
        services.make_plan(self.operator, order_id=plan.order_id, expected_version=2,
                          observation_id=plan.observation_id, now=self.now + timedelta(minutes=2))
        services.make_plan(self.operator, order_id=second.pk, expected_version=1,
                          observation_id=plan.observation_id, now=self.now + timedelta(minutes=2))
        response = self.open()
        self.assertContains(response, '계획이 막힘')
        self.assertContains(response, '처리할 것 2건')
        self.assertEqual(response.context['alert_total'], 2)
        self.assertEqual(response.context['alert_total'], sum(row['count'] for row in response.context['alerts']))
        for row in response.context['alerts']:
            self.assertTrue(row['href'].startswith('/'))
            self.assertEqual(self.client.get(row['href']).status_code, 200)

    def test_stale_or_superseded_selection_links_to_board_with_same_pin(self):
        self.cargo()
        old = self.observation(observed_at=self.now - timedelta(minutes=2))
        self.observation(source_revision='new')
        response = self.open(query=f'?observation={old.pk}')
        self.assertEqual(response.context['alert_total'], 2)
        self.assertContains(response, '야드 현황을 새로 받아야 합니다')
        self.assertContains(response, '더 최근 야드 현황이 있습니다')
        for row in response.context['alerts']:
            self.assertEqual(row['href'], f'/?observation={old.pk}#observation-select')
        self.assertEqual(self.open(query='?observation=invalid').status_code, 404)

    def test_open_episodes_only_and_field_assignment_scope(self):
        order = self.order(kind='INBOUND')
        self.observation()
        other = User.objects.create_user('other-field')
        other.groups.add(Group.objects.get(name='field'))
        episodes = []
        for assignee, state in [(self.field, 'OPEN'), (other, 'OPEN'), (self.field, 'CONFIRMED'),
                                 (self.field, 'MISMATCH'), (self.field, 'UNABLE'), (self.field, 'REASSIGNED')]:
            episodes.append(FieldEpisode.objects.create(order=order, assigned_to=assignee,
                vehicle_id=f'vehicle-{len(episodes)}', created_by=self.operator, state=state))
        response = self.open()
        self.assertEqual([row['href'] for row in response.context['alerts']],
                         [f'/field/{episode.pk}/' for episode in episodes[:2]])
        response = self.open(self.field)
        self.assertEqual([row['href'] for row in response.context['alerts']], [f'/field/{episodes[0].pk}/'])
        self.assertContains(response, '적재면 확인 대기')
        self.assertNotContains(response, 'vehicle-1')
        self.assertEqual(self.client.get(response.context['alerts'][0]['href']).status_code, 200)
        viewer = User.objects.create_user('viewer')
        viewer.groups.add(Group.objects.get_or_create(name='viewer')[0])
        response = self.open(viewer)
        self.assertEqual(response.context['alert_total'], 2)
        self.assertTrue(all(row['href'] is None for row in response.context['alerts']))

    def test_changed_order_requires_plan_review(self):
        plan = self.job()
        Order.objects.filter(pk=plan.order_id).update(version=2)
        response = self.open()
        self.assertContains(response, '계획 재검토 필요')
        self.assertEqual(response.context['alert_total'], 1)
        self.assertEqual(response.context['alerts'][0]['href'],
                         f'/?observation={plan.observation_id}#proposal-title')

    def test_visible_copy_uses_operational_terms(self):
        self.job('BLOCKED')
        response = self.open()
        content = response.content.decode().split('id="alerts-page"', 1)[1]
        content = re.sub(r'<details\b.*?</details>', '', content, flags=re.S)
        visible = re.sub(r'<[^>]+>', '', content)
        self.assertNotRegex(visible, r'관측|원본|인계|대조|회차|#\d+|READY|HOLD|BLOCKED')
        self.assertNotIn('<form', content)
        self.assertContains(response, '상태는 ROSY 실행 결과로 바뀜')

    def test_page_is_read_only_for_every_role(self):
        command = self.job('UNKNOWN')
        models = (Audit, Command, FieldEpisode, Observation, Order, Plan)
        before = [list(model.objects.values()) for model in models]
        viewer = User.objects.create_user('readonly-viewer')
        viewer.groups.add(Group.objects.get_or_create(name='viewer')[0])
        for user in (self.operator, self.admin, self.field, viewer):
            self.assertEqual(self.open(user).status_code, 200)
            self.assertEqual(self.client.post('/alerts/', {'action': 'resolve', 'command': command.pk}).status_code, 405)
        self.assertEqual(before, [list(model.objects.values()) for model in models])

    def _open_episode(self, *, vehicle_id='v-release', age=timedelta(0)):
        code = 'IN-C'
        cargo = self.cargo(code)
        self.observation(code=code)
        order = self.order(cargo, kind='INBOUND', sequence=Order.objects.count() + 1)
        episode = FieldEpisode.objects.create(order=order, assigned_to=self.field, vehicle_id=vehicle_id,
                                              created_by=self.operator, state='OPEN')
        if age:
            FieldEpisode.objects.filter(pk=episode.pk).update(created_at=timezone.now() - age)
            episode.refresh_from_db()
        return episode

    def test_open_episode_under_ten_minutes_stays_waiting(self):
        now = timezone.now()
        episode = self._open_episode(vehicle_id='v-fresh')
        FieldEpisode.objects.filter(pk=episode.pk).update(created_at=now - timedelta(minutes=9, seconds=59))
        with patch.dict(os.environ, {'HARBOR_RELEASE_OVERDUE_MINUTES': '', 'HARBOR_LEDGER_PATH': ''}), \
                patch('harbor_web.alert_pages.timezone.now', return_value=now):
            response = self.open()
        self.assertContains(response, 'IN-C · 적재면 확인 대기')
        self.assertContains(response, '차량 v-fresh')
        self.assertNotContains(response, 'ROSY 사건이 안 옴')

    def test_open_episode_overdue_without_release_shows_emergency_message(self):
        now = timezone.now()
        episode = self._open_episode(vehicle_id='v-overdue')
        FieldEpisode.objects.filter(pk=episode.pk).update(created_at=now - timedelta(minutes=10))
        with patch.dict(os.environ, {'HARBOR_RELEASE_OVERDUE_MINUTES': '', 'HARBOR_LEDGER_PATH': ''}), \
                patch('harbor_web.alert_pages.timezone.now', return_value=now):
            response = self.open()
        self.assertContains(response, 'IN-C · ROSY 사건이 안 옴 → 비상 적재면 확인')
        self.assertContains(response, '차량 v-overdue · 10분 경과')
        content = response.content.decode().split('id="alerts-page"', 1)[1]
        visible = re.sub(r'<[^>]+>', '', content)
        self.assertNotRegex(visible, r'관측|원본|인계|대조|회차|#\d+|READY|HOLD|BLOCKED')

    def test_custom_release_overdue_minutes_setting_and_env_are_respected(self):
        now = timezone.now()
        episode = self._open_episode(vehicle_id='v-custom')
        FieldEpisode.objects.filter(pk=episode.pk).update(created_at=now - timedelta(minutes=6))

        with patch.dict(os.environ, {'HARBOR_RELEASE_OVERDUE_MINUTES': '', 'HARBOR_LEDGER_PATH': ''}), \
                override_settings(HARBOR_RELEASE_OVERDUE_MINUTES=5), \
                patch('harbor_web.alert_pages.timezone.now', return_value=now):
            self.assertEqual(release_overdue_minutes(), 5.0)
            self.assertContains(self.open(), 'ROSY 사건이 안 옴 → 비상 적재면 확인')

        with patch.dict(os.environ, {'HARBOR_RELEASE_OVERDUE_MINUTES': '15', 'HARBOR_LEDGER_PATH': ''}), \
                override_settings(HARBOR_RELEASE_OVERDUE_MINUTES=5), \
                patch('harbor_web.alert_pages.timezone.now', return_value=now):
            self.assertEqual(release_overdue_minutes(), 15.0)
            response = self.open()
            self.assertContains(response, '적재면 확인 대기')
            self.assertNotContains(response, 'ROSY 사건이 안 옴')

        for invalid in ('abc', '0', '-2', 'nan', 'inf'):
            with patch.dict(os.environ, {'HARBOR_RELEASE_OVERDUE_MINUTES': invalid}):
                self.assertEqual(release_overdue_minutes(), 10.0)

    def test_recorded_release_permit_in_ledger_or_intent_suppresses_overdue(self):
        now = timezone.now()
        episode = self._open_episode(vehicle_id='v-ledger')
        FieldEpisode.objects.filter(pk=episode.pk).update(created_at=now - timedelta(minutes=15))
        episode.refresh_from_db()

        with tempfile.TemporaryDirectory() as tmp:
            ledger_path = Path(tmp) / 'ledger.sqlite3'
            ledger = DepartureLedger(ledger_path)
            with closing(sqlite3.connect(ledger_path)) as conn, conn:
                # Older permit or another vehicle's permit must not suppress this arrival.
                record_release(conn, dict(event_id='a' * 64, vehicle_id='v-ledger',
                                          observed_at=episode.created_at - timedelta(seconds=1)))
                record_release(conn, dict(event_id='b' * 64, vehicle_id='v-other', observed_at=now))
            with patch.dict(os.environ, {'HARBOR_LEDGER_PATH': str(ledger_path), 'HARBOR_RELEASE_OVERDUE_MINUTES': ''}), \
                    patch('harbor_web.alert_pages.timezone.now', return_value=now):
                self.assertContains(self.open(), 'ROSY 사건이 안 옴 → 비상 적재면 확인')

                with closing(sqlite3.connect(ledger_path)) as conn, conn:
                    record_release(conn, dict(event_id='c' * 64, vehicle_id='v-ledger',
                                              observed_at=episode.created_at))
                resp_permitted = self.open()
                self.assertContains(resp_permitted, '적재면 확인 대기')
                self.assertNotContains(resp_permitted, 'ROSY 사건이 안 옴')

                # Spending the permit (PERMITTED -> AUTHORIZED) must still suppress the overdue alert.
                self.assertEqual(ledger.authorize('v-ledger', since=episode.created_at), 'c' * 64)
                resp_authorized = self.open()
                self.assertContains(resp_authorized, '적재면 확인 대기')
                self.assertNotContains(resp_authorized, 'ROSY 사건이 안 옴')

        # Recorded DepartureIntent.release_event_id also suppresses even without HARBOR_LEDGER_PATH.
        DepartureIntent.objects.create(episode=episode, release_event_id='d' * 64,
                                       vehicle_id=episode.vehicle_id, cargo=episode.order.cargo)
        with patch.dict(os.environ, {'HARBOR_LEDGER_PATH': '', 'HARBOR_RELEASE_OVERDUE_MINUTES': ''}), \
                patch('harbor_web.alert_pages.timezone.now', return_value=now):
            resp_intent = self.open()
            self.assertContains(resp_intent, '적재면 확인 대기')
            self.assertNotContains(resp_intent, 'ROSY 사건이 안 옴')
