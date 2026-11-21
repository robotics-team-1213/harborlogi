"""Saved inbound alternatives stay consistent across decisions, previews and reservations."""
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth.models import Group, User
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase

from harbor_web import services
from harbor_web.inbound_decision import (
    DECIDE_ACTION, decide_placement, inbound_progress, projected_snapshot,
)
from harbor_web.models import Audit, Command, Plan
from harbor_web.tests import test_autoplanning


class InboundAlternativeTests(TestCase):
    setUp = test_autoplanning.AutomaticPlanningTests.setUp
    cargo = test_autoplanning.AutomaticPlanningTests.cargo
    condition = test_autoplanning.AutomaticPlanningTests.condition
    observe = test_autoplanning.AutomaticPlanningTests.observe
    enabled = test_autoplanning.AutomaticPlanningTests.enabled

    def post_decision(self, plan, **fields):
        return self.client.post(f'/plans/{plan.pk}/', dict(
            action='decide', expected_version=plan.order_version, **fields))

    def test_saved_alternative_is_audited_projected_and_displayed_without_changing_inputs(self):
        order, observation, plan = self.enabled()
        self.client.force_login(self.operator)
        page = self.client.get(f'/plans/{plan.pk}/')
        self.assertContains(page, '<summary>자리 바꾸기(예외)</summary>', html=True)
        rank = len(plan.result['alternatives'])
        chosen = plan.result['alternatives'][rank - 1]['placement']
        original = plan.result
        self.assertEqual(self.post_decision(plan, alternative_rank=rank).status_code, 302)
        decision = Audit.objects.get(action=DECIDE_ACTION)
        self.assertEqual(decision.actor, self.operator)
        self.assertEqual(decision.detail['alternative_rank'], rank)
        self.assertEqual(decision.detail['recommendation'], chosen)
        projected = projected_snapshot(services.spatial_snapshot(observation), inbound_progress())
        self.assertEqual(vars(projected.placements[0]), chosen)
        self.assertEqual(self.post_decision(plan, alternative_rank=rank).status_code, 302)
        self.assertEqual(self.post_decision(plan, alternative_rank=0).status_code, 409)
        self.assertEqual(Audit.objects.filter(action=DECIDE_ACTION).count(), 1)
        page = self.client.get(f'/plans/{plan.pk}/')
        self.assertNotContains(page, '자리 바꾸기(예외)')
        self.assertContains(page, page.context['decision_place'])
        self.assertNotEqual(page.context['decision_place'], page.context['recommendation_place'])
        frames = page.context['yard_scene']['frames']
        self.assertEqual(len(frames), 3)
        self.assertNotEqual(frames[1]['boxes'], frames[2]['boxes'])
        self.assertEqual(frames[0]['boxes'], [])
        plan.refresh_from_db()
        observation.refresh_from_db()
        self.assertEqual(plan.result, original)
        self.assertEqual(observation.payload['placements'], [])
        self.assertFalse(Command.objects.exists())
        # Older audit rows lack alternative_rank and must still project their pinned placement.
        legacy = SimpleNamespace(detail={k: v for k, v in decision.detail.items() if k != 'alternative_rank'})
        self.assertEqual(projected_snapshot(services.spatial_snapshot(observation),
            {'rows': [{'decision': legacy}]}), projected)

    def test_omitting_rank_still_decides_recommendation(self):
        _, _, plan = self.enabled()
        self.client.force_login(self.operator)
        self.assertEqual(self.post_decision(plan).status_code, 302)
        decision = Audit.objects.get(action=DECIDE_ACTION)
        self.assertEqual(decision.detail['alternative_rank'], 0)
        self.assertEqual(decision.detail['recommendation'], plan.result['recommendation'])
        self.assertEqual(self.post_decision(plan, alternative_rank=0).status_code, 302)
        self.assertEqual(Audit.objects.filter(action=DECIDE_ACTION).count(), 1)

    def test_invalid_ranks_create_no_decision(self):
        _, _, plan = self.enabled()
        for rank in (-1, len(plan.result['alternatives']) + 1, True, 1.0, '1', None):
            with self.subTest(rank=rank), self.assertRaises(ValidationError):
                decide_placement(self.operator, plan_id=plan.pk, expected_version=1, alternative_rank=rank)
        self.client.force_login(self.operator)
        for value in ('-1', 'bad', '1.5', '9' * 5000):
            with self.subTest(value=value[:20]):
                self.assertEqual(self.post_decision(plan, alternative_rank=value).status_code, 400)
        self.assertEqual(self.post_decision(plan, alternative_rank=999).status_code, 409)
        self.assertFalse(Audit.objects.filter(action=DECIDE_ACTION).exists())

    def test_only_operator_can_choose_an_alternative(self):
        _, _, plan = self.enabled()
        viewer = User.objects.create_user('viewer')
        viewer.groups.add(Group.objects.get_or_create(name='viewer')[0])
        for user in (self.admin, viewer):
            with self.subTest(user=user.username):
                self.client.force_login(user)
                self.assertNotContains(self.client.get(f'/plans/{plan.pk}/'), '자리 바꾸기(예외)')
                self.assertEqual(self.post_decision(plan, alternative_rank=1).status_code, 403)
                with self.assertRaises(PermissionDenied):
                    decide_placement(user, plan_id=plan.pk, expected_version=1, alternative_rank=1)
        self.assertFalse(Audit.objects.filter(action=DECIDE_ACTION).exists())

    def test_alternatives_obey_freshness_version_and_latest_plan_guards(self):
        order, observation, plan = self.enabled()
        with self.assertRaises(services.Conflict):
            decide_placement(self.operator, plan_id=plan.pk, expected_version=2, alternative_rank=1)
        with patch('django.utils.timezone.now', return_value=self.now + timedelta(minutes=2)):
            with self.assertRaises(services.Conflict):
                decide_placement(self.operator, plan_id=plan.pk, expected_version=1, alternative_rank=1)
            self.client.force_login(self.operator)
            self.assertNotContains(self.client.get(f'/plans/{plan.pk}/'), '자리 바꾸기(예외)')
        services.make_plan(self.operator, order_id=order.pk, observation_id=observation.pk, expected_version=1)
        with self.assertRaises(services.Conflict):
            decide_placement(self.operator, plan_id=plan.pk, expected_version=1, alternative_rank=1)
        self.assertFalse(Audit.objects.filter(action=DECIDE_ACTION).exists())

    def test_conflicting_choice_rolls_back_and_recalculation_reserves_chosen_alternative(self):
        first, observation, _ = self.enabled()
        second = services.create_order(self.operator, cargo=self.cargo('second', 1), kind='INBOUND',
            priority_group=1, sequence=2, tie_policy='FIXED')
        second_plan = services.make_plan(self.operator, order_id=second.pk, expected_version=1,
            observation_id=observation.pk, automatic=True)
        first_plan = Plan.objects.filter(order=first).latest('pk')
        self.assertEqual(first_plan.result['alternatives'][0]['placement']['column'],
                         second_plan.result['recommendation']['column'])
        decide_placement(self.operator, plan_id=first_plan.pk, expected_version=1, alternative_rank=1)
        with self.assertRaises(services.Conflict):
            decide_placement(self.operator, plan_id=second_plan.pk, expected_version=1)
        self.assertEqual(Audit.objects.filter(action=DECIDE_ACTION).count(), 1)
        revised = services.make_plan(self.operator, order_id=second.pk, expected_version=1,
            observation_id=observation.pk)
        self.assertNotEqual(revised.result['recommendation']['column'],
                            first_plan.result['alternatives'][0]['placement']['column'])
        decide_placement(self.operator, plan_id=revised.pk, expected_version=1)
        self.assertEqual(len(projected_snapshot(services.spatial_snapshot(observation),
            inbound_progress()).placements), 2)

    def test_no_alternatives_and_held_plans_do_not_offer_an_override(self):
        order, observation, original = self.enabled()
        self.client.force_login(self.operator)
        for status in ('READY', 'HOLD'):
            plan = Plan.objects.create(order=order, order_version=order.version, observation=observation,
                condition=original.condition, queue_digest=original.queue_digest, created_by=self.operator,
                result={**original.result, 'status': status, 'alternatives': []})
            self.assertNotContains(self.client.get(f'/plans/{plan.pk}/'), '자리 바꾸기(예외)')
            with self.assertRaises(ValidationError):
                decide_placement(self.operator, plan_id=plan.pk, expected_version=1, alternative_rank=1)
        self.assertFalse(Audit.objects.filter(action=DECIDE_ACTION).exists())
