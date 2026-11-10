"""Inbound schedule sheet, rearrangement slots and the operations work plan (ADR 0031, KAN-118)."""
from datetime import date, datetime, time, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth.models import Group, User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from harbor_web import inbound_schedule, rearrangement, scenario_import, services
from harbor_web.models import Audit, CargoPriority, Observation, Order, Plan, ScheduledInbound
from harbor_web.plan_service import _outbound_schedule
from harbor_web.tests.test_control_board import site_policy
from harbor_web.xlsx import read_xlsx, write_xlsx

LAYOUT = [list(scenario_import.LAYOUT_COLUMNS) + ['종류'], ['C1', '', '1', '1', '1', '긴급'], ['C2', '', '1', '1', '2', '']]


def schedule_book(rows, layout=LAYOUT):
    return write_xlsx({scenario_import.LAYOUT_SHEET: layout,
                       scenario_import.SCHEDULE_SHEET: [list(scenario_import.SCHEDULE_COLUMNS), *rows]})


def stamp(at):
    return timezone.localtime(at).strftime('%Y-%m-%d %H:%M')


class TimeCellTests(SimpleTestCase):
    def test_text_and_spreadsheet_times(self):
        cases = {'9:05': (None, time(9, 5)), '09:05:30': (None, time(9, 5, 30)), '2026-11-10 09:05': (date(2026, 11, 10), time(9, 5)),
                 '0.375': (None, time(9)), '0.3958333333333333': (None, time(9, 30)), '46336.5': (date(2026, 11, 10), time(12)),
                 '0': (None, time(0))}
        for text, expected in cases.items():
            self.assertEqual(inbound_schedule.parse_time(text), expected, text)

    def test_what_is_not_a_time(self):
        for text in ('', '24:00', '9:60', '9', '1.5', '아홉 시', '٩:٠٥', '2026-02-30 09:00'):
            self.assertIsNone(inbound_schedule.parse_time(text), text)


class DeliveryTests(SimpleTestCase):
    def entry(self, pk, minute, vehicle, position=1):
        return SimpleNamespace(pk=pk, order_id=pk, expected_at=datetime(2026, 11, 10, 9, 0) + timedelta(minutes=minute),
                               vehicle=vehicle, stack_position=position)

    def test_one_pinky_at_one_time_is_one_delivery_top_first(self):
        found = inbound_schedule.deliveries([self.entry(2, 0, '1', 2), self.entry(1, 0, '1', 1), self.entry(3, 0, '2')])
        self.assertEqual([(d.vehicle, [e.pk for e in d.entries], d.end.time()) for d in found],
                         [('1', [1, 2], time(9, 4)), ('2', [3], time(9, 6))])  # Pinky 2 waits for Pinky 1's two containers

    def test_the_same_pinky_at_two_times_is_two_deliveries(self):
        found = inbound_schedule.deliveries([self.entry(1, 0, '1'), self.entry(2, 30, '1')])
        self.assertEqual([d.start.time() for d in found], [time(9, 0), time(9, 30)])

    def test_a_gap_of_ten_minutes_after_the_estimated_end_holds_a_rearrangement(self):
        found = inbound_schedule.slots(inbound_schedule.deliveries(
            [self.entry(1, 0, '1'), self.entry(2, 0, '1', 2), self.entry(3, 13, '2'), self.entry(4, 26, '3')]))
        # 09:04 → 09:13 is 9 minutes: no slot. 09:15 → 09:26 is 11 minutes: a slot. The last delivery ends 09:28.
        self.assertEqual([(s.start.time(), s.end and s.end.time(), s.gap) for s in found],
                         [(time(9, 15), time(9, 26), timedelta(minutes=11)), (time(9, 28), None, None)])

    def test_overlapping_deliveries_leave_no_gap(self):
        found = inbound_schedule.slots(inbound_schedule.deliveries(
            [self.entry(n, 0, str(n)) for n in range(1, 7)] + [self.entry(7, 15, '7')]))
        self.assertEqual([(s.start.time(), s.end) for s in found], [(time(9, 17), None)])  # 6 × 2 min end 09:12, 3 min gap

    def test_the_gate_opens_in_slots_and_caps_the_moves(self):
        entries = [self.entry(1, 10, '1'), self.entry(2, 40, '2')]
        at = lambda minute: datetime(2026, 11, 10, 9, 0) + timedelta(minutes=minute)
        gate = lambda minute, finished=(): inbound_schedule.rearrange_gate(at(minute), entries, set(finished))
        self.assertEqual(gate(5), ('INBOUND_SCHEDULE_BUSY', None))  # before the first delivery
        self.assertEqual(gate(11), ('INBOUND_SCHEDULE_BUSY', None))  # storing the first container
        self.assertEqual(gate(20), (None, 10))
        self.assertEqual(gate(35), (None, 2))
        self.assertEqual(gate(39.5), ('INBOUND_SCHEDULE_BUSY', None))  # no move fits before the next arrival
        self.assertEqual(gate(50), (None, None))
        self.assertEqual(gate(11, finished=(1, 2)), (None, None))  # every container stored: the schedule holds nothing
        self.assertEqual(inbound_schedule.rearrange_gate(at(11), [], set()), (None, None))


class ScheduleSheetTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('lead')
        self.user.groups.add(*[Group.objects.get_or_create(name=name)[0] for name in ('admin', 'operator', 'viewer')])
        services.register_condition(self.user, name='3×6 site', calibration_ref='synthetic', payload=site_policy())
        self.client.force_login(self.user)
        self.dims = scenario_import.site_dims()

    def upload(self, data, name='S1'):
        return self.client.post('/scenarios/', dict(name=name, workbook=SimpleUploadedFile('s.xlsx', data)))

    def test_rows_become_deliveries_in_arrival_order_top_first(self):
        scenario, errors = scenario_import.parse_workbook(schedule_book(
            [['09:30', '2', 'N4', ''], ['09:00', '1', 'N1', '일반'], ['09:00', '1', 'N2', '긴급'], ['09:00', '3', 'N3', '우선']]), self.dims)
        self.assertEqual(errors, [])
        self.assertEqual(scenario['mode'], 'schedule')
        self.assertEqual([(i['code'], timezone.localtime(i['at']).strftime('%H:%M'), i['vehicle'], i['stack_position'], i['delivery_size'], i['tier'])
                          for i in scenario['schedule']],
                         [('N1', '09:00', '1', 1, 2, 3), ('N2', '09:00', '1', 2, 2, 1), ('N3', '09:00', '3', 1, 1, 2), ('N4', '09:30', '2', 1, 1, 3)])
        self.assertEqual([(e['kind'], e['code']) for e in scenario['events']], [('INBOUND', c) for c in ('N1', 'N2', 'N3', 'N4')])
        self.assertEqual(timezone.localtime(scenario['schedule'][0]['at']).date(), timezone.localdate())
        self.assertEqual([(i['code'], i['tier']) for i in scenario['layout']], [('C1', 1), ('C2', 3)])

    def test_errors_name_the_sheet_and_row(self):
        _, errors = scenario_import.parse_workbook(schedule_book(
            [['9시', '1', 'N1', ''], ['09:00', '', 'N2', ''], ['09:00', '1', 'C1', ''], ['09:00', '1', 'N2', '보통'],
             ['09:00', '1', 'N2', ''], ['09:00', '1', '', '']]), self.dims)
        for text in ('「입고 일정」 2행: 시각은 9:05 · 09:05:30 · 2026-11-10 09:05 처럼 적습니다.', '「입고 일정」 3행: 핑키 번호가 비어 있습니다.',
                     '「입고 일정」 4행: C1는 이미 야드에 있어 입고할 수 없습니다.', '「입고 일정」 5행: 종류는 긴급 · 우선 · 일반입니다.',
                     '「입고 일정」 6행: 컨테이너 N2가 두 번 나옵니다.', '「입고 일정」 7행: 컨테이너 번호가 비어 있습니다.'):
            self.assertIn(text, errors)
        _, errors = scenario_import.parse_workbook(write_xlsx({'초기 배치': LAYOUT, '입고 일정': [list(scenario_import.SCHEDULE_COLUMNS)],
                                                               '이벤트': [list(scenario_import.EVENT_COLUMNS)]}), self.dims)
        self.assertEqual(errors, ['「입고 일정」과 「이벤트」 중 하나만 둡니다.'])
        _, errors = scenario_import.parse_workbook(write_xlsx({'초기 배치': LAYOUT}), self.dims)
        self.assertEqual(errors, ['「입고 일정」 시트가 없습니다. 양식을 내려받아 쓰세요.'])
        _, errors = scenario_import.parse_workbook(write_xlsx({'초기 배치': LAYOUT, '이벤트': [
            list(scenario_import.EVENT_COLUMNS), ['1', '입고', 'N1', '', '일반', '']]}), self.dims)
        self.assertEqual(errors, ['「초기 배치」 2행: 종류는 「입고 일정」 시트와 함께 씁니다.'])

    def test_registration_keeps_the_schedule_and_the_tiers(self):
        response = self.upload(scenario_import.template_workbook(self.dims))
        observation = Observation.objects.get()
        self.assertRedirects(response, f'/?observation={observation.pk}', fetch_redirect_response=False)
        rows = [(s.order.cargo.code, timezone.localtime(s.expected_at).strftime('%H:%M'), s.vehicle, s.stack_position, s.delivery_size)
                for s in ScheduledInbound.objects.select_related('order__cargo').order_by('order_id')]
        self.assertEqual(rows, [('N1', '09:00', '1', 1, 2), ('N2', '09:00', '1', 2, 2), ('N3', '09:05', '2', 1, 1), ('N4', '09:40', '1', 1, 1)])
        self.assertEqual(sorted(CargoPriority.objects.values_list('cargo__code', 'tier')),
                         [('C1', 1), ('C2', 3), ('C4', 2), ('N1', 3), ('N2', 1), ('N3', 2), ('N4', 3)])
        self.assertEqual(_outbound_schedule()['N2'], (1, None))
        for order in Order.objects.filter(kind='INBOUND'):
            self.assertEqual(Plan.objects.filter(order=order).latest('pk').result['status'], 'READY')
        self.assertEqual(Audit.objects.get(action='scenario.import').detail['mode'], 'schedule')
        self.assertContains(self.client.get(f'/?observation={observation.pk}'), '입고 사이 빈 시간 33분')  # 09:07 → 09:40
        template = read_xlsx(self.client.get('/scenarios/template.xlsx').content)
        self.assertEqual(set(template), {'초기 배치', '입고 일정', '설명'})

    def test_an_outbound_request_outranks_the_tier(self):
        self.upload(scenario_import.template_workbook(self.dims))
        services.create_order(self.user, cargo=Order.objects.get(cargo__code='N2').cargo, kind='OUTBOUND', priority_group=3,
                              sequence=1, tie_policy='FIXED')
        self.assertEqual(_outbound_schedule()['N2'], (3, 1))
        self.assertEqual(_outbound_schedule()['C1'], (1, None))


class ScheduleGateTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('lead')
        self.user.groups.add(*[Group.objects.get_or_create(name=name)[0] for name in ('admin', 'operator', 'viewer')])
        services.register_condition(self.user, name='3×6 site', calibration_ref='synthetic', payload=site_policy())
        self.base = timezone.localtime().replace(second=0, microsecond=0) + timedelta(minutes=1)
        scenario, errors = scenario_import.parse_workbook(schedule_book(
            [[stamp(self.base + timedelta(minutes=10)), '1', 'N1', ''], [stamp(self.base + timedelta(minutes=40)), '2', 'N2', '긴급']]),
            scenario_import.site_dims())
        self.assertEqual(errors, [])
        scenario_import.import_scenario(self.user, scenario, name='S1')
        rearrangement.enable_authorization(self.user, max_moves=6, valid_until=self.base + timedelta(days=1))

    def check(self, minutes):
        seen = {}

        def proposal(observation, options, *, now):
            seen.update(options)
            return SimpleNamespace(status='HOLD', reason='REARRANGE_NOT_WORTH')
        with patch.object(rearrangement, 'rearrange_proposal', proposal):
            return rearrangement.auto_check(self.base + timedelta(minutes=minutes)), seen.get('max_moves')

    def test_automatic_rearrangement_waits_for_the_slots(self):
        self.assertEqual(self.check(11), ('INBOUND_SCHEDULE_BUSY', None))  # storing N1 (10 → 12)
        self.assertEqual(self.check(35), ('REARRANGE_NOT_WORTH', 2))  # 5 minutes before Pinky 2: two moves fit
        self.assertEqual(self.check(50), ('REARRANGE_NOT_WORTH', 6))
        self.assertEqual(list(Audit.objects.filter(action=rearrangement.HOLD_ACTION).values_list('detail__reason', flat=True)),
                         ['INBOUND_SCHEDULE_BUSY', 'REARRANGE_NOT_WORTH'])


class WorkPlanTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('lead')
        self.user.groups.add(*[Group.objects.get_or_create(name=name)[0] for name in ('admin', 'operator', 'viewer')])
        services.register_condition(self.user, name='3×6 site', calibration_ref='synthetic', payload=site_policy())
        self.client.force_login(self.user)

    def test_the_work_plan_lists_inbound_and_rearrangement_by_expected_time(self):
        base = timezone.localtime().replace(second=0, microsecond=0) + timedelta(hours=1)
        rows = [[stamp(base), '1', 'N1', ''], [stamp(base), '1', 'N2', '긴급'], [stamp(base + timedelta(minutes=30)), '2', 'N3', '']]
        self.client.post('/scenarios/', dict(name='S1', workbook=SimpleUploadedFile('s.xlsx', schedule_book(rows))))
        observation = Observation.objects.get()
        response = self.client.get(f'/?observation={observation.pk}')
        plans = response.context['operations']['workload']['plans']
        def clock(minutes):
            at = base + timedelta(minutes=minutes)
            return at.strftime('%H:%M' if at.date() == timezone.localdate() else '%m-%d %H:%M')
        self.assertEqual([(row['kind'], row['title'], row['when_text'], row['state']) for row in plans],
                         [('inbound', 'N1', clock(0), '결정 대기'), ('inbound', 'N2', clock(0), '결정 대기'),
                          ('rearrange', '자동 재배치', clock(4), '예정'), ('inbound', 'N3', clock(30), '결정 대기'),
                          ('rearrange', '자동 재배치', clock(32), '예정')])
        self.assertEqual([row['when_note'] for row in plans],
                         ['핑키 1 · 1 / 2', '핑키 1 · 2 / 2', '입고 사이 빈 시간 26분', '핑키 2 · 1 / 1', '모든 입고 뒤'])
        self.assertTrue(plans[0]['touched'])  # the inbound plan marks its slot on the yard plan
        html = response.content.decode()
        self.assertIn('<h3 id="proposal-title">작업 계획</h3>', html)
        for text in ('이 현황 기준', '요청 #', '계획 생성', 'Plan #'):
            self.assertNotIn(text, html)
        self.assertIn(f'<time datetime="{timezone.localtime(base).isoformat()}">{clock(0)}</time>', html)
        self.assertIn('<span class="badge tone-wait">결정 대기</span>', html)
        self.assertIn('data-job-timeline="slot-1"', html)
