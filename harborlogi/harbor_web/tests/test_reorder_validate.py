import json
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.urls import resolve
from django.utils import timezone

from harbor_web import services
from harbor_web.models import Audit, Cargo, CargoFootprint, Command, Order, Plan

ROOT = Path(__file__).resolve().parents[2]


class ReorderValidateTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user('admin')
        self.operator = User.objects.create_user('operator')
        self.viewer = User.objects.create_user('viewer')
        self.field = User.objects.create_user('field')
        for user, roles in (
            (self.admin, ('admin', 'viewer')),
            (self.operator, ('operator', 'viewer')),
            (self.viewer, ('viewer',)),
            (self.field, ('field',)),
        ):
            user.groups.add(*[Group.objects.get_or_create(name=name)[0] for name in roles])

        policy = json.loads((ROOT / 'config/spatial-policy.example.json').read_text(encoding='utf-8'))
        policy['yard_layout'] = json.loads((ROOT / 'config/yard-layout.json').read_text(encoding='utf-8'))
        self.condition = services.register_condition(self.admin, name='1×3 rows', calibration_ref='synthetic', payload=policy)
        placements = []
        for code, row, level in (('C1', 1, 1), ('C2', 1, 2), ('C3', 2, 1)):
            services.register_cargo(self.operator, code=code, name=code, weight_kg=2, volume_m3='0.012', destination='반출대', length_cells=3)
            footprint = CargoFootprint.objects.filter(cargo__code=code).latest('pk')
            placements.append(dict(cargo_id=code, area_id='yard', row=row, column=1, level=level, orientation=0, footprint_revision=footprint.pk))
        self.obs = services.record_observation(
            self.admin,
            source_id='row-camera',
            source_revision='1',
            observed_at=timezone.now(),
            provenance='SYNTHETIC',
            condition=self.condition,
            payload=dict(
                schema='grid-placement-v1',
                observed_areas=['yard', *(area['area_id'] for area in policy['spatial_areas'])],
                placements=placements,
                holdings=[],
            ),
        )
        self.c1 = services.create_order(self.operator, cargo=Cargo.objects.get(code='C1'), kind='OUTBOUND', priority_group=2, sequence=1, tie_policy='FIXED')
        self.c2 = services.create_order(self.operator, cargo=Cargo.objects.get(code='C2'), kind='OUTBOUND', priority_group=2, sequence=2, tie_policy='FIXED')
        self.client.force_login(self.operator)

    def test_validate_route_permissions_and_read_only(self):
        self.assertEqual(resolve('/outbound/validate/').url_name, 'outbound_validate')
        payload = {'lane': ['FIXED|', f'FIXED|{self.c2.pk}:1,{self.c1.pk}:1', 'FIXED|']}
        before = (Order.objects.count(), Plan.objects.count(), Command.objects.count(), Audit.objects.count())
        with patch('harbor.rosy_http.RosyClient._http', side_effect=AssertionError('validate contacted Fleet')):
            resp = self.client.post('/outbound/validate/', payload)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data['ok'])
        self.assertEqual((data['rehandles'], data['moves']), (0, 2))
        self.assertEqual((data['current']['rehandles'], data['current']['moves']), (1, 4))
        self.assertNotIn('endTime', data)
        self.assertEqual(before, (Order.objects.count(), Plan.objects.count(), Command.objects.count(), Audit.objects.count()))
        self.assertEqual((Order.objects.get(pk=self.c1.pk).version, Order.objects.get(pk=self.c2.pk).version), (1, 1))

        self.assertEqual(self.client.get('/outbound/validate/').status_code, 405)
        for user in (self.viewer, self.admin, self.field):
            self.client.force_login(user)
            self.assertEqual(self.client.post('/outbound/validate/', payload).status_code, 403)
        self.client.logout()
        self.assertEqual(self.client.post('/outbound/validate/', payload).status_code, 302)

    def test_validate_rejects_malformed_stale_and_running_moved_back(self):
        self.assertEqual(self.client.post('/outbound/validate/', {'lane': ['FIXED|x:1', 'FIXED|', 'FIXED|']}).status_code, 400)
        self.assertEqual(self.client.post('/outbound/validate/', {'lane': ['FIXED|', f'FIXED|{self.c2.pk}:9,{self.c1.pk}:1', 'FIXED|']}).status_code, 409)

        plan = services.make_plan(self.operator, order_id=self.c1.pk, expected_version=1, observation_id=self.obs.pk)
        Command.objects.create(order=self.c1, plan=plan, actor=self.operator, plan_version=1,
            ledger_identity='test', fleet_origin='test', fleet_principal='test',
            body_digest='a' * 64, binding={}, state='RECONCILED')
        resp = self.client.post('/outbound/validate/', {'lane': ['FIXED|', f'FIXED|{self.c2.pk}:1,{self.c1.pk}:1', 'FIXED|']})
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.json()['ok'])
        self.assertIn('실행 중인 반출 작업보다 앞에 둘 수 없습니다.', resp.json()['reasons'])

    def test_work_hub_renders_reorder_editor_for_operator_only_and_keeps_query_count(self):
        from harbor_web.work_hub import hub_context
        with self.assertNumQueries(3):
            hub_context()
        services.make_plan(self.operator, order_id=self.c1.pk, expected_version=1, observation_id=self.obs.pk)
        with self.assertNumQueries(4):
            hub_context()
        html = self.client.get('/work/').content.decode()
        for text in ('data-reorder-editor', 'data-validate-url="/outbound/validate/"', 'data-reorder-url="/outbound/reorder/"', 'reorder.js'):
            self.assertIn(text, html)
        self.client.force_login(self.viewer)
        viewer_html = self.client.get('/work/').content.decode()
        self.assertNotIn('data-reorder-editor', viewer_html)
        self.assertNotIn('reorder.js', viewer_html)
