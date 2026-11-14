"""Scenario workbook import, inbound decisions and the rearrangement scenario (KAN-117 · KAN-118 · KAN-119)."""
from datetime import timedelta
from decimal import Decimal
import io
import json
from pathlib import Path
import zipfile

from django.contrib.auth.models import Group, User
from django.core.files.uploadedfile import SimpleUploadedFile
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.db import transaction
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from harbor_web import scenario_import, services
from harbor_web.coordination import queue_lock
from harbor_web.inbound_decision import DECIDE_ACTION, inbound_progress
from harbor_web.models import Audit, Observation, Order, Plan
from harbor_web.xlsx import SheetError, read_xlsx, write_xlsx

ROOT = Path(__file__).resolve().parents[2]


def workbook(layout, events):
    return write_xlsx({scenario_import.LAYOUT_SHEET: [list(scenario_import.LAYOUT_COLUMNS), *layout],
                       scenario_import.EVENT_SHEET: [list(scenario_import.EVENT_COLUMNS), *events]})


class WorkbookTests(SimpleTestCase):
    def test_a_cargo_leaves_once_and_order_numbers_are_whole_numbers(self):
        data = workbook([['C1', '', '1', '1']], [['1', '반출', 'C1', '', '우선', ''], ['2', '반출', 'C1', '', '우선', ''],
                                                   ['3', '입고', 'C1', '', '', ''], ['abc', '입고', 'N1', '', '', ''],
                                                   ['-1', '입고', 'N2', '', '', ''], ['', '입고', 'N3', '', '', '']])
        _, errors = scenario_import.parse_workbook(data)
        for text in ('「이벤트」 3행: C1는 앞선 반출로 이미 나가 다시 반출할 수 없습니다.',
                     '「이벤트」 4행: C1는 앞선 반출로 나간 화물이라 이 시나리오에서 다시 입고할 수 없습니다.',
                     '「이벤트」 5행: 순번은 0~1000000 정수이거나 비워 둡니다.', '「이벤트」 6행: 순번은 0~1000000 정수이거나 비워 둡니다.'):
            self.assertIn(text, errors)
        self.assertFalse([error for error in errors if '7행' in error])  # a blank order number is allowed

    def test_overlong_digit_runs_are_reported_not_raised(self):
        # int() refuses more than 4300 digits; the parser must report the cell, not answer 500.
        _, errors = scenario_import.parse_workbook(workbook([['C1', '', '9' * 5000, '1']], [['9' * 5000, '반출', 'C1', '', '', '']]))
        self.assertIn('「초기 배치」 2행: 행과 층은 1~3입니다.', errors)
        self.assertIn('「이벤트」 2행: 순번은 0~1000000 정수이거나 비워 둡니다.', errors)

    def test_odd_shared_string_indexes_read_as_empty_cells(self):
        source, buffer = zipfile.ZipFile(io.BytesIO(write_xlsx({'S': [['x']]}))), io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as archive:
            for item in source.infolist():
                if item.filename != 'xl/worksheets/sheet1.xml':
                    archive.writestr(item, source.read(item))
            archive.writestr('xl/sharedStrings.xml', '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                                                     '<si><t>hello</t></si></sst>')
            archive.writestr('xl/worksheets/sheet1.xml', '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                '<sheetData><row r="1"><c r="A1" t="s"><v>²</v></c><c r="B1" t="s"><v>' + '9' * 5000 + '</v></c>'
                '<c r="C1" t="s"><v>0</v></c></row></sheetData></worksheet>')
        self.assertEqual(read_xlsx(buffer.getvalue()), {'S': [['', '', 'hello']]})

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


    def test_unicode_digits_are_reported_not_crashed(self):
        data = workbook([['C1', '', '²', '1']], [['³', '입고', 'N1', '', '일반', '']])
        scenario, errors = scenario_import.parse_workbook(data)  # '²'.isdigit() is True but int('²') raises
        self.assertIsNone(scenario)
        self.assertIn('「초기 배치」 2행: 행과 층은 1~3입니다.', errors)

    def test_outbound_must_follow_its_inbound_in_registration_order(self):
        # Registration follows 순번, so the "inbound before outbound" check must use 순번, not sheet row order.
        later_rows_first = workbook([['C1', '', '1', '1']], [['2', '반출', 'N1', '', '우선', ''], ['1', '입고', 'N1', '', '일반', '']])
        scenario, errors = scenario_import.parse_workbook(later_rows_first)
        self.assertEqual(errors, [])
        self.assertEqual([(e['kind'], e['code']) for e in scenario['events']], [('INBOUND', 'N1'), ('OUTBOUND', 'N1')])
        sheet_order_lies = workbook([['C1', '', '1', '1']], [['2', '입고', 'N1', '', '일반', ''], ['1', '반출', 'N1', '', '우선', '']])
        scenario, errors = scenario_import.parse_workbook(sheet_order_lies)
        self.assertIsNone(scenario)
        self.assertIn('「이벤트」 3행: N1는 초기 배치나 앞선 입고에 없어 반출할 수 없습니다.', errors)


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
        self.assertIn('id="scenario-state">재배치 시나리오</h3>', page)  # the preview, not the page head
        self.assertIn('+ 결정한 입고 위치', page)
        self.assertIn('name="action" value="request" disabled', page)
        self.assertContains(self.client.get('/'), '입고 위치 결정됨')

    def test_stale_decision_is_refused(self):
        self.upload(scenario_import.template_workbook())
        order = Order.objects.filter(kind='INBOUND').first()
        plan = Plan.objects.filter(order=order).latest('pk')
        self.assertEqual(self.client.post(f'/plans/{plan.pk}/', dict(action='decide', expected_version=order.version + 1)).status_code, 409)
        self.assertFalse(Audit.objects.filter(action=DECIDE_ACTION).exists())

    def _inbound(self, code):
        cargo = services.register_cargo(self.user, code=code, name=code, weight_kg=Decimal('1.000'), volume_m3=Decimal('0.0120'),
                                        destination='시험', length_cells=3)
        group, sequence = services.back_of_tier(3, 'INBOUND')
        return services.create_order(self.user, cargo=cargo, kind='INBOUND', priority_group=group, sequence=sequence, tie_policy='FIXED')

    def test_undecidable_inbound_does_not_hold_the_rearrangement(self):
        self.upload(scenario_import.template_workbook())
        observation = Observation.objects.get()
        unplanned = self._inbound('U1')  # no plan at all: nothing a person could decide
        held = self._inbound('H1')  # the latest plan is HOLD: decide_placement accepts READY only
        Plan.objects.create(order=held, order_version=held.version, observation=observation, condition=observation.condition,
                            queue_digest='held', result=dict(plan_kind='inbound-placement', status='HOLD', reason='NO_SPACE'),
                            created_by=self.user)
        for order in Order.objects.filter(kind='INBOUND', auto_replan=True).order_by('pk'):  # new requests re-planned these
            plan = Plan.objects.filter(order=order).latest('pk')
            self.client.post(f'/plans/{plan.pk}/', dict(action='decide', expected_version=order.version))
        progress = inbound_progress()
        self.assertEqual(progress['waiting'], [])
        self.assertEqual([row['order'].pk for row in progress['undecidable']], [unplanned.pk, held.pk])
        self.assertEqual((progress['decided'], progress['total']), (2, 4))
        page = self.client.get('/rearrangement/').content.decode()
        self.assertIn('id="scenario-state">재배치 시나리오</h3>', page)  # the preview, not the page head
        self.assertIn('결정할 수 없는 입고 2건', page)

    def test_a_plan_whose_basis_changed_is_undecidable_everywhere(self):
        # The decide button hides once the plan's observation expired (workspace.plan_changes). Such a request must
        # not keep the rearrangement waiting, and the server must refuse it the same way (review of #44).
        self.upload(scenario_import.template_workbook())
        order = Order.objects.filter(kind='INBOUND').order_by('pk').first()
        plan = Plan.objects.filter(order=order).latest('pk')
        with patch('django.utils.timezone.now', return_value=timezone.now() + timedelta(seconds=1801)):  # max age 1800 s
            progress = inbound_progress()
            self.assertEqual(progress['waiting'], [])
            self.assertEqual([row['reason'] for row in progress['undecidable']], ['관측 유효 시간 지남 · 새 관측 필요'] * 2)
            page = self.client.get('/rearrangement/').content.decode()
            self.assertIn('id="scenario-state">재배치 시나리오</h3>', page)
            self.assertIn('관측 유효 시간 지남 · 새 관측 필요', page)
            self.assertIn('유효 시간이 지났습니다. 새 관측이 들어와야', page)  # the preview's own observation expired too
            self.assertIn('name="action" value="request" disabled', page)
            self.client.post('/rearrangement/', dict(action='request'))
            self.assertFalse(Order.objects.filter(kind='REARRANGE').exists())  # R2 would only hold it
            self.assertNotContains(self.client.get(f'/plans/{plan.pk}/'), '이 위치로 결정')
            self.assertEqual(self.client.post(f'/plans/{plan.pk}/', dict(action='decide', expected_version=order.version)).status_code, 409)
        self.assertFalse(Audit.objects.filter(action=DECIDE_ACTION).exists())

    def test_a_hand_planned_inbound_whose_queue_changed_waits_for_a_new_calculation_not_a_person(self):
        self.upload(scenario_import.template_workbook())
        manual = self._inbound('M1')  # planned by hand: a queue change does not re-plan it
        services.make_plan(self.user, order_id=manual.pk, expected_version=manual.version, observation_id=Observation.objects.get().pk)
        self.assertIn(manual.pk, [row['order'].pk for row in inbound_progress()['waiting']])  # fresh: a person can decide it
        later = self._inbound('M2')
        for order in Order.objects.filter(kind='INBOUND', auto_replan=True).order_by('pk'):  # the scenario's own, re-planned
            plan = Plan.objects.filter(order=order).latest('pk')
            self.client.post(f'/plans/{plan.pk}/', dict(action='decide', expected_version=order.version))
        progress = inbound_progress()
        self.assertEqual(progress['waiting'], [])
        self.assertEqual([(row['order'].pk, row['reason']) for row in progress['undecidable']],
                         [(manual.pk, '요청이나 대기열이 바뀜 · 다시 계산 필요'), (later.pk, '계획 없음 · 계산 필요')])

    def test_an_older_plan_points_to_the_newest_instead_of_offering_a_refused_decision(self):
        self.upload(scenario_import.template_workbook())
        manual = self._inbound('M1')
        observation = Observation.objects.get()
        first = services.make_plan(self.user, order_id=manual.pk, expected_version=manual.version, observation_id=observation.pk)
        newest = services.make_plan(self.user, order_id=manual.pk, expected_version=manual.version, observation_id=observation.pk)
        page = self.client.get(f'/plans/{first.pk}/')
        self.assertNotContains(page, '이 위치로 결정')
        self.assertContains(page, f'최신 계획 #{newest.pk}에서 결정')
        self.assertContains(self.client.get(f'/plans/{newest.pk}/'), '이 위치로 결정')

    def test_the_automatic_planning_budget_message_reaches_the_page(self):
        for i in range(15):
            self._inbound(f'A{i}')
        Order.objects.update(auto_replan=True)
        response = self.upload(scenario_import.template_workbook(), name='S2')
        self.assertContains(response, '자동 계획 한도 16건 중 15건을 이전 요청이 쓰고 있어 입고 2건을 더 등록할 수 없습니다.')
        self.assertFalse(Observation.objects.exists())

    def test_import_holds_the_queue_lock_before_opening_its_transaction(self):
        # Every other mutating service takes queue_lock first and the SQLite write lock inside it; the import
        # must do the same or two requests can wait on each other ("database is locked").
        owned_at_atomic, real_atomic = [], transaction.atomic
        def spy(*args, **kwargs):
            owned_at_atomic.append(queue_lock._is_owned())
            return real_atomic(*args, **kwargs)
        scenario, errors = scenario_import.parse_workbook(scenario_import.template_workbook())
        self.assertEqual(errors, [])
        with patch.object(scenario_import.transaction, 'atomic', spy):
            scenario_import.import_scenario(self.user, scenario, name='S1')
        self.assertTrue(owned_at_atomic)
        self.assertTrue(all(owned_at_atomic), owned_at_atomic)

    def test_automatic_planning_budget_is_checked_before_anything_is_created(self):
        # make_plan allows 16 automatic requests per site and earlier scenarios keep theirs, so a later import
        # must fail up front with the reason instead of rolling back half-way with make_plan's message.
        for i in range(15):
            self._inbound(f'A{i}')
        Order.objects.update(auto_replan=True)
        scenario, _ = scenario_import.parse_workbook(scenario_import.template_workbook())  # 2 inbound
        with self.assertRaises(ValidationError) as caught:
            scenario_import.import_scenario(self.user, scenario, name='S2')
        self.assertIn('자동 계획 한도 16건 중 15건을 이전 요청이 쓰고 있어 입고 2건을 더 등록할 수 없습니다.', caught.exception.messages[0])
        self.assertFalse(Observation.objects.exists())
        self.assertFalse(Order.objects.filter(cargo__code='N1').exists())
