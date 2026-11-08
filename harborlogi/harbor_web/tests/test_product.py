from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import Client, TestCase
from django.utils import timezone

from harbor_web.models import Cargo, Order, Plan, Command
from harbor_web import services


class ProductPagesTests(TestCase):
    def test_duplicate_cargo_shows_one_business_recovery_error(self):
        self.client.force_login(self.operator)
        data = dict(code='C1', name='화물', weight_kg='1', volume_m3='1', destination='반출대', length_cells=1)
        self.assertEqual(self.client.post('/cargo/new/', data).status_code, 302)
        response = self.client.post('/cargo/new/', data)
        self.assertContains(response, '이미 등록된 화물 ID입니다.')
        self.assertNotContains(response, 'Use nonempty trimmed text')
        self.assertEqual(Cargo.objects.count(), 1)

    def test_brand_and_token_assets_are_discoverable(self):
        from django.contrib.staticfiles import finders
        self.assertIsNotNone(finders.find('tokens.css'))
        self.assertIsNotNone(finders.find('harbor-app-icon-20261023.png'))
        self.assertRedirects(self.client.get('/favicon.ico'), '/static/harbor-app-icon-20261023.png', status_code=301, fetch_redirect_response=False)

    @classmethod
    def setUpTestData(cls):
        cls.operator = get_user_model().objects.create_user('operator-page')
        cls.viewer = get_user_model().objects.create_user('viewer-page')
        cls.field = get_user_model().objects.create_user('field-page')
        cls.admin = get_user_model().objects.create_user('admin-page')
        cls.other_field = get_user_model().objects.create_user('other-field-page')
        for user, role in [(cls.operator, 'operator'), (cls.viewer, 'viewer'), (cls.field, 'field'), (cls.admin, 'admin'), (cls.other_field, 'field')]:
            user.groups.add(Group.objects.get_or_create(name=role)[0])

    def test_persistent_cargo_and_request_round_trip(self):
        self.client.force_login(self.operator)
        response = self.client.post('/cargo/new/', dict(code='C1', name='시험 화물', weight_kg='12.3', volume_m3='0.12', destination='부산', length_cells=1))
        self.assertEqual(response.status_code, 302)
        cargo = Cargo.objects.get(code='C1')
        self.assertEqual(self.client.get('/cargo/?q=C1').status_code, 200)
        response = self.client.post('/requests/new/', dict(cargo=cargo.pk, kind='OUTBOUND', priority_group=1, sequence=1, tie_policy='FIXED', note='원래 지시'))
        self.assertEqual(response.status_code, 302)
        order = Order.objects.get()
        self.assertContains(self.client.get(f'/requests/{order.pk}/'), 'C1')

    def test_missing_cargo_shows_plain_field_error_without_constraint_name(self):
        self.client.force_login(self.operator)
        for kind in ('INBOUND', 'OUTBOUND'):
            response = self.client.post('/requests/new/', dict(kind=kind, priority_group=1, sequence=1, tie_policy='FIXED'))
            self.assertContains(response, '입고·반출 요청은 화물을 선택해야 합니다.')
            self.assertNotContains(response, 'order_cargo_unless_rearrange')
            self.assertIn('cargo', response.context['form'].errors)
        self.assertEqual(Order.objects.count(), 0)

    def test_request_detail_forms_have_no_duplicate_ids(self):
        import re
        self.client.force_login(self.operator)
        self.client.post('/cargo/new/', dict(code='C1', name='화물', weight_kg='1', volume_m3='1', destination='부산', length_cells=1))
        self.client.post('/requests/new/', dict(cargo=Cargo.objects.get().pk, kind='OUTBOUND', priority_group=1, sequence=1, tie_policy='FIXED'))
        html = self.client.get(f'/requests/{Order.objects.get().pk}/').content.decode()
        ids = re.findall(r'id="([^"]+)"', html)
        self.assertEqual(sorted(i for i in set(ids) if ids.count(i) > 1), [])
        self.assertIn('name="expected_version"', html)

    def test_role_guards_and_csrf(self):
        self.client.force_login(self.viewer)
        self.assertEqual(self.client.post('/cargo/new/', {}).status_code, 403)
        self.client.force_login(self.field)
        self.assertEqual(self.client.get('/cargo/').status_code, 403)
        self.assertRedirects(self.client.get('/'), '/field/', fetch_redirect_response=False)
        csrf = Client(enforce_csrf_checks=True)
        csrf.force_login(self.operator)
        self.assertEqual(csrf.post('/cargo/new/', {}).status_code, 403)

    def test_invalid_input_preserved_and_empty_state(self):
        self.client.force_login(self.operator)
        self.assertContains(self.client.get('/'), '조치')
        response = self.client.post('/cargo/new/', dict(code='작성중', name='', weight_kg='-1', volume_m3='', destination=''))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '작성중')
        self.assertFalse(Cargo.objects.exists())

    def test_stored_plan_and_observation_render_with_closed_execution_gate(self):
        for code in ('C1', 'C2'):
            services.register_cargo(self.operator, code=code, name=code, weight_kg=1, volume_m3=1, destination='부산')
        condition = services.register_condition(self.admin, name='시험 설치', calibration_ref='synthetic-calibration', payload=dict(outbound_stand='stand', temporary_bays=['temp'], allowed_moves=[['yard', 'temp'], ['temp', 'yard'], ['yard', 'stand']], max_observation_age_seconds=300, conveyor='conveyor', arm_reach=[['A', 'yard'], ['A', 'temp'], ['A', 'stand']]))
        observation = services.record_observation(self.admin, source_id='synthetic-camera', source_revision='1', observed_at=timezone.now(), provenance='SYNTHETIC', condition=condition, payload=dict(bays=[dict(bay_id='yard', cargo=['C1', 'C2'], capacity=3), dict(bay_id='temp', cargo=[], capacity=2), dict(bay_id='stand', cargo=[], capacity=1)], holdings=[]))
        order = services.create_order(self.operator, cargo=Cargo.objects.get(code='C1'), kind='OUTBOUND', priority_group=1, sequence=1, tie_policy='FIXED')
        self.client.force_login(self.operator)
        response = self.client.post(f'/requests/{order.pk}/', dict(action='plan', expected_version=1, observation=observation.pk))
        self.assertEqual(response.status_code, 302)
        plan = Plan.objects.get()
        self.assertEqual(len(plan.result['moves']), 3)
        response = self.client.get(f'/plans/{plan.pk}/')
        self.assertContains(response, '관측한 배치')
        self.assertContains(response, '계획 후 예상 배치')
        self.assertNotContains(response, '현재 계획의 인계 요청 준비')
        command = Command.objects.create(order=order, plan=plan, actor=self.operator, plan_version=1,
            ledger_identity='test-ledger', fleet_origin='http://127.0.0.1:9999', fleet_principal='test-principal',
            body_digest='a' * 64, binding={}, state='UNKNOWN')
        home = self.client.get('/')
        self.assertContains(home, '제출 결과가 불명입니다')
        self.assertContains(home, f'/work/{command.pk}/')
        self.assertEqual(self.client.get('/inventory/').status_code, 200)
        self.assertEqual(self.client.get('/settings/').status_code, 403)
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get('/settings/').status_code, 200)
        self.assertEqual(self.client.get('/inventory/import/').status_code, 200)

    def test_request_conflict_and_field_scope_through_real_routes(self):
        cargo = services.register_cargo(self.operator, code='I1', name='입고', weight_kg=1, volume_m3=1, destination='부산')
        order = services.create_order(self.operator, cargo=cargo, kind='INBOUND', priority_group=1, sequence=1, tie_policy='FIXED')
        self.client.force_login(self.operator)
        response = self.client.post(f'/requests/{order.pk}/', dict(action='priority', expected_version=0, priority_group=2, sequence=1, tie_policy='FIXED', note='변경'))
        self.assertEqual(response.status_code, 409)
        episode = services.assign_episode(self.operator, order_id=order.pk, vehicle_id='pinky-1', assigned_to=self.field)
        self.client.force_login(self.other_field)
        self.assertEqual(self.client.get(f'/field/{episode.pk}/').status_code, 403)
        self.client.force_login(self.field)
        self.assertContains(self.client.get(f'/field/{episode.pk}/'), 'pinky-1')
        response = self.client.post(f'/field/{episode.pk}/', dict(action='confirm', expected_version=1, observed_at=timezone.localtime().isoformat(), outcome='CLEAR'))
        self.assertEqual(response.status_code, 302)
        self.assertContains(self.client.get(f'/field/{episode.pk}/'), '적재면 비움 확인')
        self.client.force_login(self.operator)
        self.assertEqual(self.client.get('/records/').status_code, 200)
        self.assertEqual(self.client.get('/records/compare/').status_code, 200)
        self.assertEqual(self.client.get('/records/measurements/new/').status_code, 200)
