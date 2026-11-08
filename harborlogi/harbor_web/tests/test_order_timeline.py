from datetime import timedelta

from django.test import TestCase

from harbor_web.command_models import EvidenceSubmission
from harbor_web.commands import _save
from harbor_web.models import Audit, Command, CommandFact, Plan
from harbor_web.tests import test_services as fixtures
from harbor_web.tests import test_workspace as workspace_fixtures


class OrderTimelineTests(TestCase):
    setUp = fixtures.ServicesTests.setUp
    cargo = fixtures.ServicesTests.cargo
    condition = fixtures.ServicesTests.condition
    observation = fixtures.ServicesTests.observation
    order = fixtures.ServicesTests.order
    buried_plan = workspace_fixtures.WorkspaceTests.buried_plan

    def command(self, plan, *states):
        command = Command.objects.create(order=plan.order, plan=plan, actor=self.operator, plan_version=1,
            ledger_identity='l', fleet_origin='o', fleet_principal='p', body_digest='a' * 64, binding={})
        for state in states or ('RECONCILED',):
            _save(command, self.operator, state)  # the real write path: state plus its audit row
        return command

    def get(self, order):
        self.client.force_login(self.operator)
        return self.client.get(f'/requests/{order.pk}/timeline/')

    def test_plan_line_has_expected_steps_and_actual_line_is_empty_without_a_command(self):
        plan = self.buried_plan()
        response = self.get(plan.order)
        self.assertContains(response, 'Plan #%d' % plan.pk)
        self.assertContains(response, f'href="/plans/{plan.pk}/"')
        self.assertContains(response, '예상 · 재취급')
        self.assertContains(response, '예상 · 반출')
        self.assertContains(response, '실행 기록 없음')
        self.assertEqual(response.context['actual_line'], [])

    def test_actual_line_names_sources_marks_evidence_unreconciled_and_never_infers_completion(self):
        plan = self.buried_plan()
        command = self.command(plan, 'DISPATCHING', 'RECONCILED')
        CommandFact.objects.create(created_by=self.operator, command=command, command_key=command.key,
            body_digest=command.body_digest, fleet_origin='o', fleet_principal='p', job=dict(status='GOAL_CONFIRMED'),
            receipt=dict(receipt_id='r'), read_at=self.now + timedelta(seconds=5))
        EvidenceSubmission.objects.create(created_by=self.operator, command=command, placement={}, retreat={})
        response = self.get(plan.order)
        events = response.context['actual_line']
        self.assertEqual({'실행 지시', '지시 상태', 'ROSY 조회', '업무 근거'}, {e['kind'] for e in events})
        transitions = [e['text'] for e in events if e['kind'] == '지시 상태']
        self.assertEqual(transitions, [f'Plan #{plan.pk} 지시 → 제출 대조 필요', f'Plan #{plan.pk} 지시 → 원본 대조됨'])
        self.assertTrue(all(e['source'].startswith('Harbor 저장 시각 · 감사 기록 #') for e in events if e['kind'] == '지시 상태'))
        self.assertTrue(all(e['source'] for e in events))
        self.assertContains(response, '(대조 전)')
        self.assertContains(response, '물리 receipt 있음')
        self.assertContains(response, '원본 대조됨')
        self.assertNotContains(response, '업무 완료(업무 장부 대조)')
        self.assertEqual([e['at'] for e in events], sorted(e['at'] for e in events))
        Command.objects.filter(pk=command.pk).update(state='COMPLETED')  # a state with no audited transition
        self.assertNotContains(self.get(plan.order), '업무 완료(업무 장부 대조)')
        _save(command, self.operator, 'COMPLETED')
        self.assertContains(self.get(plan.order), '업무 완료(업무 장부 대조)')

    def test_get_writes_nothing_and_post_is_refused(self):
        plan = self.buried_plan()
        self.command(plan)
        before = (Audit.objects.count(), Plan.objects.count(), Command.objects.count())
        self.assertEqual(self.get(plan.order).status_code, 200)
        self.assertEqual(before, (Audit.objects.count(), Plan.objects.count(), Command.objects.count()))
        self.assertEqual(self.client.post(f'/requests/{plan.order.pk}/timeline/').status_code, 405)
        self.assertEqual(self.client.get('/requests/999999/timeline/').status_code, 404)
        self.assertContains(self.client.get(f'/requests/{plan.order.pk}/'), f'/requests/{plan.order.pk}/timeline/')
        self.client.logout()
        self.assertNotEqual(self.client.get(f'/requests/{plan.order.pk}/timeline/').status_code, 200)
