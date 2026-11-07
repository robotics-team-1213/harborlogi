"""Control board layout and the yard plan by rows for 1×3 containers (KAN-115 시안)."""
import json
from pathlib import Path

from django.contrib.auth.models import Group, User
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from harbor.spatial import Area, CargoShape, Placement, SpatialSnapshot
from harbor_web import services
from harbor_web.models import CargoFootprint
from harbor_web.tests import test_multicell_view
from harbor_web.yard_rows import place_label, rows_from_snapshot

ROOT = Path(__file__).resolve().parents[2]


class YardRowsTests(SimpleTestCase):
    def test_mixed_footprints_keep_every_run_of_a_row_level(self):
        snapshot = test_multicell_view.SpatialViewTests.snapshot(self)  # C1 2×1 under C2 1×1 in row 1, C5 3×1 in row 3
        view = rows_from_snapshot(snapshot, labels={'temp': '임시 장치', 'stand': '반출대'}, temporary=['temp'],
                                  outbound='stand', targets={'C1'}, rehandles={'C2'})
        rows = {row['number']: row for row in view['yard']['rows']}
        level = {entry['level']: entry['runs'] for entry in rows[1]['levels']}
        self.assertEqual([(run['cargo'], run['span'], run['role']) for run in level[1]], [('C1', 2, 'target'), (None, 1, 'empty')])
        self.assertEqual([(run['cargo'], run['span'], run['role']) for run in level[2]], [('C2', 1, 'rehandle'), (None, 2, 'empty')])
        self.assertEqual([rows[n]['height'] for n in (1, 2, 3)], [2, 0, 1])
        self.assertEqual(rows[1]['cells'], 'Y11–Y13')
        whole = rows[3]['levels'][-1]['runs']
        self.assertEqual([(run['cargo'], run['whole'], run['role']) for run in whole], [('C5', True, 'cargo')])
        self.assertEqual([(a['id'], a['role'], a['free']) for a in view['auxiliary']], [('temp', '임시 장치', 3), ('stand', '반출대', 1)])
        self.assertEqual(view['dimensions'], '3층 · 3행 × 3열')

    def test_a_one_by_three_container_is_one_slot_of_its_row(self):
        base = test_multicell_view.SpatialViewTests.snapshot(self)
        snapshot = SpatialSnapshot(1, base.observed_at, base.layout, base.areas,
            (CargoShape('A', 3, 1), CargoShape('B', 3, 2)), (Placement('A', 'yard', 2, 1, 1), Placement('B', 'yard', 2, 1, 2)))
        row = rows_from_snapshot(snapshot)['yard']['rows'][1]
        self.assertEqual(row['height'], 2)
        self.assertEqual([[(run['cargo'], run['whole']) for run in entry['runs']] for entry in row['levels']],
                         [[(None, True)], [('B', True)], [('A', True)]])

    def test_place_label_reads_rows_not_cells(self):
        self.assertEqual(place_label(dict(area_id='yard', row=2, column=1, level=3)), '2행 3층')
        self.assertEqual(place_label(dict(area_id='temp', row=1, column=1, level=1), {'temp': '임시 장치'}), '임시 장치 1층')
        self.assertEqual(place_label(None), '관측 출발 위치 없음')


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
            payload=dict(schema='grid-placement-v1', observed_areas=['yard', 'temp', 'stand'], placements=placements, holdings=[]))
        from harbor_web.models import Cargo
        self.order = services.create_order(self.operator, cargo=Cargo.objects.get(code='C1'), kind='OUTBOUND',
                                           priority_group=1, sequence=1, tie_policy='FIXED')
        self.plan = services.make_plan(self.operator, order_id=self.order.pk, expected_version=1, observation_id=self.observation.pk)
        self.client.force_login(self.operator)

    def test_overview_links_jobs_to_yard_rows_and_their_timeline(self):
        self.assertEqual(self.plan.result['status'], 'READY')
        html = self.client.get(f'/?observation={self.observation.pk}').content.decode()
        for text in ('data-yard-row="yard-R1"', 'data-yard-row="yard-R3"', f'data-job="{self.plan.pk}"',
                     f'data-job-timeline="{self.plan.pk}"', '반출 C1', 'slot role-target span-3', 'slot role-rehandle span-3',
                     '실제 완료량·처리율', 'id="observation-select"', '장비 상태판 · 보류'):
            self.assertIn(text, html)
        rows = html.split(f'data-job="{self.plan.pk}" data-rows="', 1)[1].split('"', 1)[0].split()
        self.assertIn('yard-R1', rows)
        self.assertIn('<span class="crumb">운영 현황</span>', html)

    def test_outbound_workspace_shows_the_same_row_view(self):
        page = self.client.get(f'/requests/{self.order.pk}/')
        self.assertContains(page, '행별 적재 · 재취급 예정')
        self.assertContains(page, 'slot role-target span-3')
        self.assertContains(page, '1행</b><span class="cells">Y11–Y13</span>')
