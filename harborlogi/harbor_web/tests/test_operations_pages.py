from django.test import TestCase
from harbor_web.models import Observation, Plan, Audit
from harbor_web.tests import test_autoplanning


class OperationsPagesTests(TestCase):
    setUp = test_autoplanning.AutomaticPlanningTests.setUp
    cargo = test_autoplanning.AutomaticPlanningTests.cargo
    condition = test_autoplanning.AutomaticPlanningTests.condition
    observe = test_autoplanning.AutomaticPlanningTests.observe
    enabled = test_autoplanning.AutomaticPlanningTests.enabled

    def test_observation_pin_and_inbound_recommendation_are_visible_without_writes(self):
        order, observation, plan = self.enabled()
        newer_condition = self.condition()
        self.client.force_login(self.operator)
        counts = (Plan.objects.count(), Observation.objects.count(), Audit.objects.count())
        response = self.client.get(f'/?observation={observation.pk}')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['condition'].pk, observation.condition_id)
        self.assertNotEqual(response.context['condition'].pk, newer_condition.pk)
        self.assertContains(response, '입고 배치 추천')
        self.assertContains(response, '현장 인계: 배정 전')
        self.assertEqual(response.context['operations']['actual']['completed'], 0)
        self.assertContains(response, '시간당 물동량')
        self.assertEqual(self.client.get('/?observation=missing').status_code, 404)
        self.assertEqual(counts, (Plan.objects.count(), Observation.objects.count(), Audit.objects.count()))

    def test_inbound_plan_has_pinned_route_comparison_and_geometry_link(self):
        order, observation, plan = self.enabled()
        self.client.force_login(self.operator)
        response = self.client.get(f'/plans/{plan.pk}/')
        self.assertContains(response, '화물별 이동 계획')
        self.assertContains(response, 'data-plan-sequence', count=1)
        self.assertContains(response, f'<h3>{order.cargo.code} · ')
        self.assertContains(response, '관측된 출발 위치 없음')
