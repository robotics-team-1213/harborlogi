from django.test import TestCase
from harbor_web import services
from harbor_web.models import Plan
from harbor_web.tests import test_workspace


class ReviewFlowTests(TestCase):
    setUp = test_workspace.WorkspaceTests.setUp
    cargo = test_workspace.WorkspaceTests.cargo
    condition = test_workspace.WorkspaceTests.condition
    observation = test_workspace.WorkspaceTests.observation
    order = test_workspace.WorkspaceTests.order
    buried_plan = test_workspace.WorkspaceTests.buried_plan

    def test_selected_observation_survives_overview_and_outbound_drilldown(self):
        plan = self.buried_plan()
        selected = plan.observation
        self.observation(source_revision='newer-source')
        self.client.force_login(self.operator)
        overview = self.client.get(f'/?observation={selected.pk}')
        self.assertContains(overview, f'href="/outbound/?observation={selected.pk}"')
        self.assertContains(overview, f'href="/requests/{plan.order_id}/?observation={selected.pk}"')
        listing = self.client.get(f'/outbound/?observation={selected.pk}&q=C1')
        self.assertContains(listing, f'name="observation" value="{selected.pk}"')
        self.assertContains(listing, f'href="/requests/{plan.order_id}/?observation={selected.pk}"')
        detail = self.client.get(f'/requests/{plan.order_id}/?observation={selected.pk}')
        self.assertEqual(detail.context['source'].pk, plan.observation_id)
        self.assertEqual(detail.context['review_observation'].pk, selected.pk)
        self.assertEqual(self.client.get('/outbound/?observation=invalid').status_code, 404)

    def test_route_steps_and_comparison_filters_preserve_plans(self):
        original = self.buried_plan()
        newer = services.make_plan(self.operator, order_id=original.order_id, expected_version=1,
            observation_id=original.observation_id)
        self.client.force_login(self.operator)
        count = Plan.objects.count()
        response = self.client.get(f'/plans/{newer.pk}/')
        self.assertContains(response, 'data-plan-sequence', count=1)
        self.assertContains(response, '2단계 · ')
        self.assertNotContains(response, 'data-plan-step')
        self.assertContains(response, 'data-plan-change="unchanged"', count=2)
        self.assertContains(response, 'data-changes-only')
        self.assertContains(response, '변경된 화물이 없습니다')
        self.assertEqual(Plan.objects.count(), count)
