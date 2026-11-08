"""Outbound queue in three priority tiers, request titles and first-place registration (KAN-115 · KAN-116)."""
import json
from pathlib import Path

from django.contrib.auth.models import Group, User
from django.core.exceptions import PermissionDenied
from django.test import TestCase
from django.utils import timezone

from harbor_web import services
from harbor_web.models import Audit, Cargo, CargoFootprint, Order

ROOT = Path(__file__).resolve().parents[2]


class QueueEditingTests(TestCase):
    def setUp(self):
        self.operator = User.objects.create_user('operator')
        self.operator.groups.add(Group.objects.get_or_create(name='operator')[0])
        self.viewer = User.objects.create_user('viewer')
        self.viewer.groups.add(Group.objects.get_or_create(name='viewer')[0])

    def cargo(self, code):
        return services.register_cargo(self.operator, code=code, name=f'{code} 화물', weight_kg=1, volume_m3='0.010', destination='반출대')

    def order(self, code, group, sequence, tie_policy='FIXED', **extra):
        return services.create_order(self.operator, cargo=self.cargo(code), kind='OUTBOUND',
                                     priority_group=group, sequence=sequence, tie_policy=tie_policy, **extra)

    def lanes(self):
        return [(o.priority_group, o.sequence, o.tie_policy, o.cargo.code)
                for o in Order.objects.filter(kind='OUTBOUND').select_related('cargo').order_by('priority_group', 'sequence', 'pk')]

    def test_lanes_are_the_three_tiers_even_when_empty(self):
        self.order('A', 2, 1)
        self.order('OLD', 7, 1)  # a group from before the tiers reads as 일반
        lanes = services.outbound_lanes()
        self.assertEqual([(lane['group'], lane['name'], [o.cargo.code for o in lane['orders']]) for lane in lanes],
                         [(1, '긴급', []), (2, '우선', ['A']), (3, '일반', ['OLD'])])
        self.assertEqual([Order(priority_group=n).tier_label for n in (1, 2, 3, 7)], ['긴급', '우선', '일반', '일반'])

    def test_reorder_maps_lanes_to_tiers_and_bumps_only_changed_orders(self):
        a, b, c = self.order('A', 1, 1), self.order('B', 1, 2), self.order('C', 2, 1)
        services.reorder_outbound(self.operator, lanes=[
            dict(fixed=True, orders=[(c.pk, c.version)]),
            dict(fixed=False, orders=[(a.pk, a.version), (b.pk, b.version)]),
            dict(fixed=True, orders=[]),
        ])
        self.assertEqual(self.lanes(), [(1, 1, 'FIXED', 'C'), (2, 1, 'ANY', 'A'), (2, 2, 'ANY', 'B')])
        for order in (a, b, c):
            order.refresh_from_db()
            self.assertEqual(order.version, 2)
        update = Audit.objects.filter(action='order.update', object_id=str(c.pk)).get().detail
        self.assertEqual((update['before']['priority_group'], update['after']['priority_group']), (2, 1))

    def test_unchanged_order_keeps_its_version(self):
        a, b = self.order('A', 1, 1), self.order('B', 2, 1)
        services.reorder_outbound(self.operator, lanes=[dict(fixed=True, orders=[(a.pk, 1)]),
                                                        dict(fixed=True, orders=[(b.pk, 1)]), dict(fixed=True, orders=[])])
        a.refresh_from_db(); b.refresh_from_db()
        self.assertEqual((a.version, b.version), (1, 1))
        self.assertFalse(Audit.objects.filter(action='order.update').exists())

    def test_stale_version_is_a_conflict_and_changes_nothing(self):
        a, b = self.order('A', 1, 1), self.order('B', 2, 1)
        with self.assertRaises(services.Conflict):
            services.reorder_outbound(self.operator, lanes=[dict(fixed=True, orders=[(b.pk, 1), (a.pk, 9)]),
                                                            dict(fixed=True, orders=[]), dict(fixed=True, orders=[])])
        self.assertEqual(self.lanes(), [(1, 1, 'FIXED', 'A'), (2, 1, 'FIXED', 'B')])

    def test_lanes_are_three_and_list_every_open_outbound_order_once(self):
        a, b = self.order('A', 1, 1), self.order('B', 2, 1)
        empty = dict(fixed=True, orders=[])
        for lanes in ([dict(fixed=True, orders=[(a.pk, 1), (b.pk, 1)])],
                      [dict(fixed=True, orders=[(a.pk, 1)]), empty, empty],
                      [dict(fixed=True, orders=[(a.pk, 1), (a.pk, 1), (b.pk, 1)]), empty, empty]):
            with self.assertRaises(services.Conflict):
                services.reorder_outbound(self.operator, lanes=lanes)

    def test_reorder_requires_operator(self):
        a = self.order('A', 1, 1)
        with self.assertRaises(PermissionDenied):
            services.reorder_outbound(self.viewer, lanes=[dict(fixed=True, orders=[(a.pk, 1)]), dict(fixed=True, orders=[]),
                                                          dict(fixed=True, orders=[])])

    def test_title_is_saved_and_kept_in_request_snapshot(self):
        order = self.order('A', 1, 1, title='본선 1차 선적')
        self.assertEqual(order.title, '본선 1차 선적')
        created = Audit.objects.get(action='order.create').detail['after']
        self.assertEqual(created['title'], '본선 1차 선적')
        changed = services.update_order(self.operator, order_id=order.pk, expected_version=1, priority_group=1, sequence=1,
                                        tie_policy='FIXED', note='', title='변경된 제목')
        self.assertEqual(changed.title, '변경된 제목')
        same = services.update_order(self.operator, order_id=order.pk, expected_version=2, priority_group=1, sequence=1, tie_policy='FIXED')
        self.assertEqual(same.title, '변경된 제목')

    def test_new_request_goes_last_in_the_tier_it_picks(self):
        self.order('A', 3, 1)
        self.order('B', 3, 2)
        self.client.force_login(self.operator)
        page = self.client.get('/requests/new/?kind=OUTBOUND')
        self.assertContains(page, 'name="priority_group" value="3"')
        self.assertContains(page, 'data-queue-lane data-tier=', count=3)
        for code, group, expected in (('C', 3, (3, 3, 'FIXED', 'C')), ('D', 1, (1, 1, 'FIXED', 'D'))):
            response = self.client.post('/requests/new/', dict(cargo=self.cargo(code).pk, kind='OUTBOUND', title='야간 반출',
                                                               priority_group=group, tie_policy='FIXED'))
            self.assertEqual(response.status_code, 302)
            self.assertIn(expected, self.lanes())

    def test_first_place_registration_goes_first_in_its_tier(self):
        self.order('A', 3, 1)
        self.order('B', 3, 2)
        self.order('X', 1, 1)
        self.client.force_login(self.operator)
        response = self.client.post('/requests/new/', dict(cargo=self.cargo('C').pk, kind='OUTBOUND', priority_group=3,
                                                           tie_policy='FIXED', make_first='on'))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.lanes(), [(1, 1, 'FIXED', 'X'), (3, 1, 'FIXED', 'C'), (3, 2, 'FIXED', 'A'), (3, 3, 'FIXED', 'B')])

    def test_unchecked_fixed_order_box_saves_any(self):
        self.client.force_login(self.operator)
        self.client.post('/requests/new/', dict(cargo=self.cargo('A').pk, kind='OUTBOUND', priority_group=2))
        self.assertEqual(Order.objects.get().tie_policy, 'ANY')

    def test_outbound_page_posts_the_three_tier_lanes(self):
        a, b = self.order('A', 1, 1, title='첫 요청'), self.order('B', 2, 1)
        self.client.force_login(self.operator)
        page = self.client.get('/outbound/')
        self.assertContains(page, 'data-queue-editor')
        self.assertContains(page, '첫 요청')
        self.assertContains(page, 'data-lane-empty-label="일반 단계에 요청 없음"')
        response = self.client.post('/outbound/reorder/', dict(lane=[f'ANY|{b.pk}:1,{a.pk}:1', 'FIXED|', 'FIXED|']))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.lanes(), [(1, 1, 'ANY', 'B'), (1, 2, 'ANY', 'A')])

    def test_outbound_reorder_rejects_malformed_and_stale_input(self):
        a = self.order('A', 1, 1)
        self.client.force_login(self.operator)
        self.assertEqual(self.client.post('/outbound/reorder/', dict(lane=['FIXED|x:1', 'FIXED|', 'FIXED|'])).status_code, 400)
        self.assertEqual(self.client.post('/outbound/reorder/', dict(lane=[f'FIXED|{a.pk}:7', 'FIXED|', 'FIXED|'])).status_code, 409)
        self.assertEqual(self.client.post('/outbound/reorder/', dict(lane=[f'FIXED|{a.pk}:1'])).status_code, 409)
        self.client.force_login(self.viewer)
        self.assertEqual(self.client.post('/outbound/reorder/', dict(lane=[f'FIXED|{a.pk}:1', 'FIXED|', 'FIXED|'])).status_code, 403)

    def test_applying_a_scenario_fixes_only_that_tier(self):
        a, b, x = self.order('A', 2, 1), self.order('B', 2, 2), self.order('X', 3, 1)
        self.client.force_login(self.operator)
        response = self.client.post('/outbound/tiers/apply/', dict(group='2', order=f'{b.pk}:1,{a.pk}:1'))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.lanes(), [(2, 1, 'FIXED', 'B'), (2, 2, 'FIXED', 'A'), (3, 1, 'FIXED', 'X')])
        x.refresh_from_db()
        self.assertEqual(x.version, 1)
        self.assertEqual(self.client.post('/outbound/tiers/apply/', dict(group='4', order=f'{a.pk}:2')).status_code, 400)
        self.assertEqual(self.client.post('/outbound/tiers/apply/', dict(group='2', order=f'{a.pk}:2')).status_code, 409)  # B missing
        self.client.force_login(self.viewer)
        self.assertEqual(self.client.post('/outbound/tiers/apply/', dict(group='2', order=f'{b.pk}:2,{a.pk}:2')).status_code, 403)


class TierScenarioPageTests(TestCase):
    """A tier with two requests shows the simulated order first and the other scenario folded (KAN-116)."""
    def setUp(self):
        self.admin = User.objects.create_user('admin')
        self.operator = User.objects.create_user('operator')
        for user, roles in ((self.admin, ('admin', 'viewer')), (self.operator, ('operator', 'viewer'))):
            user.groups.add(*[Group.objects.get_or_create(name=name)[0] for name in roles])
        policy = json.loads((ROOT / 'config/spatial-policy.example.json').read_text(encoding='utf-8'))
        policy['yard_layout'] = json.loads((ROOT / 'config/yard-layout.json').read_text(encoding='utf-8'))
        condition = services.register_condition(self.admin, name='1×3 rows', calibration_ref='synthetic', payload=policy)
        placements = []
        for code, row, level in (('C1', 1, 1), ('C2', 1, 2), ('C4', 2, 1)):
            services.register_cargo(self.operator, code=code, name=code, weight_kg=2, volume_m3='0.012', destination='반출대', length_cells=3)
            footprint = CargoFootprint.objects.filter(cargo__code=code).latest('pk')
            placements.append(dict(cargo_id=code, area_id='yard', row=row, column=1, level=level, orientation=0, footprint_revision=footprint.pk))
        services.record_observation(self.admin, source_id='row-camera', source_revision='1', observed_at=timezone.now(),
            provenance='SYNTHETIC', condition=condition,
            payload=dict(schema='grid-placement-v1', observed_areas=['yard', *(area['area_id'] for area in policy['spatial_areas'])], placements=placements, holdings=[]))
        self.c1 = services.create_order(self.operator, cargo=Cargo.objects.get(code='C1'), kind='OUTBOUND', priority_group=2, sequence=1, tie_policy='FIXED')
        self.c2 = services.create_order(self.operator, cargo=Cargo.objects.get(code='C2'), kind='OUTBOUND', priority_group=2, sequence=2, tie_policy='FIXED')
        self.client.force_login(self.operator)

    def test_recommended_order_comes_first_and_can_be_applied(self):
        html = self.client.get('/outbound/').content.decode()
        for text in ('추천 시나리오', '<b>C2</b> → <b>C1</b>', '총 이동 2회 · 재취급 0회', '추천 순서로 시행', '다른 시나리오 보기',
                     '<b>C1</b> → <b>C2</b>', '이 시나리오로 교체'):
            self.assertIn(text, html)
        response = self.client.post('/outbound/tiers/apply/', dict(group='2', order=f'{self.c2.pk}:1,{self.c1.pk}:1'))
        self.assertEqual(response.status_code, 302)
        self.assertEqual([o.cargo.code for o in Order.objects.filter(kind='OUTBOUND').order_by('sequence')], ['C2', 'C1'])
        self.assertContains(self.client.get('/outbound/'), '지금 순서가 추천 순서입니다')
