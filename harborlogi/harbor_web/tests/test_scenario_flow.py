"""Scenario workbook import, inbound decisions and the rearrangement scenario (KAN-117 · KAN-118 · KAN-119)."""
import json
from pathlib import Path

from django.contrib.auth.models import Group, User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase, override_settings

from harbor_web import scenario_import, services
from harbor_web.inbound_decision import DECIDE_ACTION, inbound_progress
from harbor_web.models import Audit, Observation, Order, Plan
from harbor_web.xlsx import SheetError, read_xlsx, write_xlsx

ROOT = Path(__file__).resolve().parents[2]


def workbook(layout, events):
    return write_xlsx({scenario_import.LAYOUT_SHEET: [list(scenario_import.LAYOUT_COLUMNS), *layout],
                       scenario_import.EVENT_SHEET: [list(scenario_import.EVENT_COLUMNS), *events]})


class WorkbookTests(SimpleTestCase):
    def test_round_trip_keeps_cells_and_sheet_names(self):
        sheets = read_xlsx(write_xlsx({'가': [['화물 ID', '층'], ['C1', '2']], 'B': [['x', '', 'z']]}))
        self.assertEqual(sheets, {'가': [['화물 ID', '층'], ['C1', '2']], 'B': [['x', '', 'z']]})
        self.assertEqual(set(read_xlsx(scenario_import.template_workbook())), {'초기 배치', '이벤트', '설명'})
        with self.assertRaises(SheetError):
            read_xlsx(b'not a zip')

    def test_every_problem_is_reported_with_its_row(self):
        data = workbook([['C1', '', '1', '2'], ['C1', '', '4', '1']],
                        [['1', '반출', 'Z9', '', '우선', ''], ['2', '입고', 'C1', '', '최우선', ''], ['3', '보관', 'C2', '', '', '']])
        scenario, errors = scenario_import.parse_workbook(data)
        self.assertIsNone(scenario)
        joined = '\n'.join(errors)
        for text in ('「초기 배치」 3행: 화물 C1가 두 번 나옵니다.', '「초기 배치」 3행: 행과 층은 1~3입니다.',
                     '1행 1층이 비어 있어 받칠 수 없습니다', '「이벤트」 2행: Z9는 초기 배치나 앞선 입고에 없어 반출할 수 없습니다.',
                     '「이벤트」 3행: 단계는 긴급 · 우선 · 일반입니다.', '「이벤트」 3행: C1는 이미 야드에 있어 입고할 수 없습니다.',
                     '「이벤트」 4행: 종류는 입고 또는 반출입니다.'):
            self.assertIn(text, joined)
        _, missing = scenario_import.parse_workbook(write_xlsx({'Sheet1': [['a']]}))
        self.assertIn('「초기 배치」 시트가 없습니다. 양식을 내려받아 쓰세요.', missing)


class ScenarioFlowTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('lead')
        self.user.groups.add(*[Group.objects.get_or_create(name=name)[0] for name in ('admin', 'operator', 'viewer')])
        policy = json.loads((ROOT / 'config/spatial-policy.example.json').read_text(encoding='utf-8'))
        policy['yard_layout'] = json.loads((ROOT / 'config/yard-layout.json').read_text(encoding='utf-8'))
        services.register_condition(self.user, name='1×3 rows', calibration_ref='synthetic', payload=policy)
        self.client.force_login(self.user)

    def upload(self, data, name='S1'):
        return self.client.post('/scenarios/', dict(name=name, workbook=SimpleUploadedFile('s.xlsx', data)))

    def test_template_registers_layout_and_every_event_up_front(self):
        self.assertEqual(self.client.get('/scenarios/template.xlsx')['Content-Type'],
                         'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        response = self.upload(scenario_import.template_workbook())
        observation = Observation.objects.get()
        self.assertRedirects(response, f'/?observation={observation.pk}', fetch_redirect_response=False)
        self.assertEqual(sorted((p['cargo_id'], p['row'], p['level']) for p in observation.payload['placements']),
                         [('C1', 1, 1), ('C2', 1, 2), ('C4', 2, 1)])
        orders = [(o.kind, o.cargo.code, o.tier_label) for o in Order.objects.select_related('cargo').order_by('pk')]
        self.assertEqual(orders, [('INBOUND', 'N1', '일반'), ('OUTBOUND', 'C1', '우선'), ('OUTBOUND', 'C2', '우선'), ('INBOUND', 'N2', '긴급')])
        for order in Order.objects.filter(kind='INBOUND'):
            plan = Plan.objects.filter(order=order).latest('pk')
            self.assertTrue(order.auto_replan)
            self.assertEqual((plan.result['plan_kind'], plan.result['status']), ('inbound-placement', 'READY'))
        self.assertEqual(Audit.objects.get(action='scenario.import').detail['inbound'], 2)
        self.assertContains(self.client.get('/scenarios/'), 'S1')

    def test_a_bad_sheet_creates_nothing(self):
        response = self.upload(workbook([['C1', '', '1', '1']], [['1', '반출', 'Z9', '', '우선', '']]))
        self.assertContains(response, '파일을 등록하지 않았습니다')
        self.assertFalse(Order.objects.exists())
        self.assertFalse(Observation.objects.exists())

    @override_settings(MODE='live')
    def test_live_mode_never_imports_synthetic_scenarios(self):
        response = self.upload(scenario_import.template_workbook())
        self.assertContains(response, '운영 연결 환경에서는 열지 않습니다')
        self.assertFalse(Order.objects.exists())

    def test_inbound_waits_for_a_person_then_rearrangement_previews(self):
        self.upload(scenario_import.template_workbook())
        page = self.client.get('/rearrangement/')
        self.assertContains(page, '입고 시나리오 진행 중')
        self.assertContains(page, '입고 결정 0 / 2')
        for order in Order.objects.filter(kind='INBOUND').order_by('pk'):
            plan = Plan.objects.filter(order=order).latest('pk')
            self.assertContains(self.client.get(f'/plans/{plan.pk}/'), '이 위치로 결정')
            response = self.client.post(f'/plans/{plan.pk}/', dict(action='decide', expected_version=order.version))
            self.assertRedirects(response, f'/plans/{plan.pk}/', fetch_redirect_response=False)
            self.assertContains(self.client.get(f'/plans/{plan.pk}/'), '결정됨')
            self.client.post(f'/plans/{plan.pk}/', dict(action='decide', expected_version=order.version))
        self.assertEqual(Audit.objects.filter(action=DECIDE_ACTION).count(), 2)  # deciding again adds nothing
        self.assertEqual(inbound_progress()['decided'], 2)
        page = self.client.get('/rearrangement/').content.decode()
        self.assertIn('재배치 시나리오', page)
        self.assertIn('+ 결정한 입고 위치', page)
        self.assertIn('name="action" value="request" disabled', page)
        self.assertContains(self.client.get('/'), '입고 위치 결정됨')

    def test_stale_decision_is_refused(self):
        self.upload(scenario_import.template_workbook())
        order = Order.objects.filter(kind='INBOUND').first()
        plan = Plan.objects.filter(order=order).latest('pk')
        self.assertEqual(self.client.post(f'/plans/{plan.pk}/', dict(action='decide', expected_version=order.version + 1)).status_code, 409)
        self.assertFalse(Audit.objects.filter(action=DECIDE_ACTION).exists())
