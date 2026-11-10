"""Control board layout and the yard plan by rows for 1×3 containers (KAN-115 시안)."""
import json
from pathlib import Path

from django.contrib.auth.models import Group, User
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from harbor.layout import YardLayout
from harbor.spatial import Area, CargoShape, Placement, SpatialSnapshot
from harbor_web import services
from harbor_web.models import Cargo, CargoFootprint
from harbor_web.tests import test_multicell_view
from harbor_web.yard_rows import place_context, place_label, placement_text, rows_from_snapshot, slot_key

ROOT = Path(__file__).resolve().parents[2]


def example_policy():
    """The 3×3 example site: no fixed slots."""
    policy = json.loads((ROOT / 'config/spatial-policy.example.json').read_text(encoding='utf-8'))
    policy['yard_layout'] = json.loads((ROOT / 'config/yard-layout.json').read_text(encoding='utf-8'))
    return policy


def site_policy():
    """The site map (KAN-115): 3×6 yard, two 1×3 slots per row, a 3-slot temp tray on each side, the conveyor."""
    policy = json.loads((ROOT / 'config/spatial-policy-3x6.json').read_text(encoding='utf-8'))
    policy['yard_layout'] = json.loads((ROOT / 'config/yard-layout-3x6.json').read_text(encoding='utf-8'))
    return policy


class YardRowsTests(SimpleTestCase):
    def test_mixed_footprints_show_every_cargo_of_a_slot_level(self):
        snapshot = test_multicell_view.SpatialViewTests.snapshot(self)  # C1 2×1 under C2 1×1 in row 1, C5 3×1 in row 3
        view = rows_from_snapshot(snapshot, labels={'temp': '임시 장치', 'stand': '반출대'}, temporary=['temp'],
                                  outbound='stand', targets={'C1'}, rehandles={'C2'})
        lanes = {lane['number']: lane for lane in view['yard']['lanes']}
        self.assertEqual([len(lane['slots']) for lane in lanes.values()], [1, 1, 1])  # a 3-cell row is one slot
        stack = lanes[1]['slots'][0]['stack']
        self.assertEqual([(e['level'], e['cargo'], e['role'], e['partial']) for e in stack],
                         [(1, 'C1', 'target', True), (2, 'C2', 'rehandle', True)])
        self.assertEqual(lanes[2]['slots'][0]['stack'], [])
        self.assertEqual([(e['cargo'], e['partial']) for e in lanes[3]['slots'][0]['stack']], [('C5', False)])
        self.assertEqual([(a['id'], a['role'], a['used'], a['slots']) for a in view['below']],
                         [('temp', '임시 적재', 0, 3), ('stand', '반출', 0, 1)])
        self.assertEqual(view['dimensions'], '3행 · 3층')

    def test_a_one_by_three_container_is_one_slot_of_its_row(self):
        base = test_multicell_view.SpatialViewTests.snapshot(self)
        snapshot = SpatialSnapshot(1, base.observed_at, base.layout, base.areas,
            (CargoShape('A', 3, 1), CargoShape('B', 3, 2)), (Placement('A', 'yard', 2, 1, 1), Placement('B', 'yard', 2, 1, 2)))
        slot = rows_from_snapshot(snapshot)['yard']['lanes'][1]['slots'][0]
        self.assertEqual(slot['height'], 2)
        self.assertEqual([(e['level'], e['cargo'], e['partial']) for e in slot['stack']], [(1, 'A', False), (2, 'B', False)])

    def test_site_map_has_two_slots_per_row_and_a_tray_on_each_side(self):
        policy = site_policy()
        areas = (Area('yard', 3, 6, 3), *(Area(**area) for area in policy['spatial_areas']))
        snapshot = SpatialSnapshot(1, timezone.now(), YardLayout.from_dict(policy['yard_layout']), areas,
            (CargoShape('K1', 3, 1), CargoShape('K2', 3, 2), CargoShape('T1', 3, 3)),
            (Placement('K1', 'yard', 1, 4, 1), Placement('K2', 'yard', 1, 4, 2), Placement('T1', 'temp_b', 2, 1, 1)))
        view = rows_from_snapshot(snapshot, labels={k: v['label'] for k, v in policy['area_presentation'].items()},
                                  temporary=policy['temporary_bays'], outbound='stand', targets={'K1'}, rehandles={'K2'},
                                  presentation=policy['area_presentation'], slot=policy['slot_cells'])
        self.assertEqual([[s['label'] for s in lane['slots']] for lane in view['yard']['lanes']], [['1칸', '2칸']] * 3)
        slot = view['yard']['lanes'][0]['slots'][1]
        self.assertEqual((slot['key'], [(e['level'], e['cargo'], e['role']) for e in slot['stack']]),
                         ('yard-R1S2', [(1, 'K1', 'target'), (2, 'K2', 'rehandle')]))
        self.assertEqual((view['yard']['used'], view['yard']['slots']), (2, 18))
        self.assertEqual([[a['id'] for a in view[side]] for side in ('left', 'right', 'below')], [['temp_a'], ['temp_b'], ['stand']])
        tray = view['right'][0]
        self.assertEqual([s['label'] for lane in tray['lanes'] for s in lane['slots']], ['1번', '2번', '3번'])
        self.assertEqual((tray['label'], tray['used'], tray['slots'], tray['cargo']), ('임시 장치 B', 1, 3, ['T1']))
        self.assertEqual(view['dimensions'], '3행 × 한 행 2자리 · 3층')

    def test_place_label_reads_rows_and_slots(self):
        self.assertEqual(place_label(dict(area_id='yard', row=2, column=1, level=3)), '2행 3층')
        self.assertEqual(place_label(dict(area_id='yard', row=2, column=4, level=1), slot=3), '2행 2칸 1층')
        self.assertEqual(place_label(dict(area_id='temp', row=1, column=1, level=1), {'temp': '임시 장치'}), '임시 장치 1층')
        self.assertEqual(place_label(dict(area_id='temp_a', row=3, column=1, level=1), {'temp_a': '임시 장치 A'}, 3),
                         '임시 장치 A 3번 1층')
        self.assertEqual(place_label(None), '반입(컨베이어)')
        self.assertEqual(slot_key(dict(area_id='yard', row=1, column=4), 3), 'yard-R1S2')
        self.assertEqual(placement_text(dict(area_id='yard', row=1, column=2, level=1, orientation=90)), '1행 1층 · 2열 · 90°')
        self.assertEqual(placement_text(dict(area_id='yard', row=1, column=4, level=1, orientation=0), slot=3), '1행 2칸 1층')

    def test_site_labels_drop_a_lone_slot_number_and_level(self):
        labels, site = place_context(site_policy())
        self.assertEqual((site.size, site.one_slot, site.one_level), (3, {'stand'}, {'temp_a', 'temp_b', 'stand'}))
        self.assertEqual(place_label(dict(area_id='yard', row=1, column=4, level=3), labels, site), '1행 2칸 3층')
        self.assertEqual(place_label(dict(area_id='temp_a', row=2, column=1, level=1), labels, site), '임시 장치 A 2번')
        self.assertEqual(place_label(dict(area_id='stand', row=1, column=1, level=1), labels, site), '반출대')
        self.assertEqual(slot_key(dict(area_id='yard', row=3, column=4), site), 'yard-R3S2')
        self.assertEqual(place_context(example_policy())[1], None)


class ControlBoardPagesTests(TestCase):
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
        self.observation = services.record_observation(self.admin, source_id='row-camera', source_revision='1', observed_at=timezone.now(),
            provenance='SYNTHETIC', condition=condition,
            payload=dict(schema='grid-placement-v1', observed_areas=['yard', *(area['area_id'] for area in policy['spatial_areas'])], placements=placements, holdings=[]))
        self.order = services.create_order(self.operator, cargo=Cargo.objects.get(code='C1'), kind='OUTBOUND',
                                           priority_group=1, sequence=1, tie_policy='FIXED')
        self.plan = services.make_plan(self.operator, order_id=self.order.pk, expected_version=1, observation_id=self.observation.pk)
        self.client.force_login(self.operator)

    def test_overview_links_jobs_to_yard_slots_and_their_timeline(self):
        self.assertEqual(self.plan.result['status'], 'READY')
        html = self.client.get(f'/?observation={self.observation.pk}').content.decode()
        for text in ('data-yard-row="yard-R1S1"', 'data-yard-row="yard-R3S1"', f'data-job="{self.plan.pk}"',
                     f'data-job-timeline="{self.plan.pk}"', '반출 C1', 'slot-level role-target', 'slot-level role-rehandle',
                     '<b>1층</b> C1', '<b>2층</b> C2', '실제 완료량·처리율', 'id="observation-select"', '장비 상태판 · 보류'):
            self.assertIn(text, html)
        rows = html.split(f'data-job="{self.plan.pk}" data-rows="', 1)[1].split('"', 1)[0].split()
        self.assertIn('yard-R1S1', rows)
        self.assertIn('<span class="crumb">운영 현황</span>', html)

    def test_outbound_workspace_shows_the_same_slot_view(self):
        page = self.client.get(f'/requests/{self.order.pk}/')
        self.assertContains(page, '자리별 적재 · 재취급 예정')
        self.assertContains(page, 'slot-level role-target')
        self.assertContains(page, '<span class="lane-label">1행</span>')


class SiteMapPagesTests(TestCase):
    """The 3×6 site: slots instead of cells, no sizes or turns, occupancy counted in slots."""
    def setUp(self):
        self.admin = User.objects.create_user('admin')
        self.operator = User.objects.create_user('operator')
        for user, roles in ((self.admin, ('admin', 'viewer')), (self.operator, ('operator', 'viewer'))):
            user.groups.add(*[Group.objects.get_or_create(name=name)[0] for name in roles])
        condition = services.register_condition(self.admin, name='3×6 site', calibration_ref='synthetic', payload=site_policy())
        placements = []
        for code, row, column, level in (('K1', 1, 1, 1), ('K2', 1, 1, 2), ('K3', 1, 4, 1), ('K4', 3, 4, 1)):
            services.register_cargo(self.operator, code=code, name=code, weight_kg=2, volume_m3='0.012', destination='컨베이어', length_cells=3)
            footprint = CargoFootprint.objects.filter(cargo__code=code).latest('pk')
            placements.append(dict(cargo_id=code, area_id='yard', row=row, column=column, level=level, orientation=0, footprint_revision=footprint.pk))
        self.observation = services.record_observation(self.admin, source_id='site-camera', source_revision='1', observed_at=timezone.now(),
            provenance='SYNTHETIC', condition=condition, payload=dict(schema='grid-placement-v1', placements=placements, holdings=[],
                                                                      observed_areas=['yard', 'temp_a', 'temp_b', 'stand']))
        self.order = services.create_order(self.operator, cargo=Cargo.objects.get(code='K1'), kind='OUTBOUND',
                                           priority_group=1, sequence=1, tie_policy='FIXED')
        self.plan = services.make_plan(self.operator, order_id=self.order.pk, expected_version=1, observation_id=self.observation.pk)
        self.client.force_login(self.operator)

    def test_overview_counts_slots_and_lays_out_the_site(self):
        self.assertEqual(self.plan.result['status'], 'READY')
        response = self.client.get(f'/?observation={self.observation.pk}')
        inventory = response.context['operations']['inventory']
        self.assertEqual((inventory['occupied'], inventory['capacity'], inventory['occupancy_percent']), (4, 18, 22))
        html = response.content.decode()
        for text in ('장치 야드 4 / 18자리', '임시 장치 A', '임시 장치 B', '반출대', 'site-left', 'site-right',
                     'data-yard-row="yard-R1S2"', '<span class="slot-name">2칸</span>', '3행 × 한 행 2자리 · 3층'):
            self.assertIn(text, html)
        for move in self.plan.result['moves']:
            self.assertEqual((move['destination_placement']['column'] - 1) % 3, 0)
        steps = response.context['operations']['workload']['plans'][0]['steps']
        self.assertEqual([(s['phase'], s['source'], s['destination']) for s in steps if s['phase'] == '반출'],
                         [('반출', '1행 1칸 1층', '반출대')])
        self.assertNotIn('반출대 1번', html)

    def test_inventory_lists_slots_without_sizes_or_turns(self):
        response = self.client.get(f'/inventory/?observation={self.observation.pk}')
        self.assertIsNotNone(response.context['yard_stage']['slot_map'])
        self.assertEqual(response.context['yard_scene']['slot_cells'], 3)  # the 3D page draws the same slots
        html = response.content.decode()
        for text in ('data-yard-plan', '자리 평면', '<td>K3</td><td>1행 2칸 1층</td>', '<b>2층</b> K2'):
            self.assertIn(text, html)
        for text in ('방향·크기', 'colspan="3"', '크기 원본 #', 'covered-addresses'):  # the what-if editor keeps its own inputs
            self.assertNotIn(text, html)
        self.assertContains(self.client.get(f'/plans/{self.plan.pk}/'), '1행 1칸')
