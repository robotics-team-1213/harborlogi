from copy import deepcopy
from unittest.mock import patch

from django.test import TestCase

from harbor_web import services
from harbor_web.models import Audit, CargoFootprint, Observation, Plan
from harbor_web.plan_explanation import explain_plan
from harbor_web.tests import test_multicell_services as fixtures


class RankedRecommendationTests(TestCase):
    setUp = fixtures.MulticellServicesTests.setUp
    cargo = fixtures.MulticellServicesTests.cargo
    condition = fixtures.MulticellServicesTests.condition
    observe = fixtures.MulticellServicesTests.observe
    order = fixtures.MulticellServicesTests.order
    payload = fixtures.MulticellServicesTests.payload

    def prepared(self):
        target, blocker = self.cargo(), self.cargo('blocker', 2)
        payload = self.payload(target)
        payload['placements'].append(dict(payload['placements'][0], cargo_id=blocker.code,
            level=2, footprint_revision=CargoFootprint.objects.get(cargo=blocker).pk))
        observed = self.observe(self.condition(), payload)
        return self.order(target), observed

    def calculate(self, order, observed):
        return services.make_plan(self.operator, order_id=order.pk, expected_version=1,
                                  observation_id=observed.pk, now=self.now)

    def test_calculated_scores_and_complete_candidates_are_persisted_and_read_only(self):
        order, observed = self.prepared()
        plan = self.calculate(order, observed)
        plan.refresh_from_db()
        saved = deepcopy(plan.result)
        report = saved['optimization']
        self.assertEqual(report['objective_version'], 'restoration_lex_v3')
        self.assertGreater(report['complete_plans_compared'], 1)
        self.assertEqual(report['alternatives'][0]['moves'], saved['moves'])
        self.assertEqual(len(saved['moves']), 3)
        counts = [m.objects.count() for m in (Plan, Observation, Audit)]
        self.client.force_login(self.operator)
        with patch('harbor.spatial.plan_spatial', side_effect=AssertionError('GET searched')):
            response = self.client.get(f'/plans/{plan.pk}/')
        self.assertContains(response, '계산 기반 계획 추천')
        self.assertContains(response, '같은 구역 주소 차이 합')
        self.assertContains(response, '현재 추천')
        self.assertEqual([m.objects.count() for m in (Plan, Observation, Audit)], counts)
        plan.refresh_from_db()
        self.assertEqual(plan.result, saved)

    def test_outbound_queue_ranks_reach_the_planner_and_v1_plans_stay_readable(self):
        from harbor.spatial import plan_spatial
        from harbor_web.recommendation_view import explain_optimization
        order, observed = self.prepared()
        with patch('harbor.spatial.plan_spatial', wraps=plan_spatial) as planner:
            plan = self.calculate(order, observed)
        self.assertEqual(planner.call_args.kwargs['schedule'][order.cargo.code][0], order.priority_group)
        report = deepcopy(plan.result['optimization'])
        self.assertEqual(len(explain_optimization(report)['score_rows']), 7)
        report['objective_version'] = 'restoration_lex_v1'
        for score in (report['score'], report['first_feasible_score'], *(a['score'] for a in report['alternatives'])):
            del score['earlier_outbound_buried'], score['unknown_outbound_order'], score['rotation_count']
        self.assertEqual([row['label'] for row in explain_optimization(report)['score_rows']][0], '복귀 구역 변경')

    def test_limited_complete_candidate_is_distinguished_from_unfinished_hold(self):
        order, observed = self.prepared()
        with patch('harbor.spatial.SPATIAL_SEARCH_LIMIT', 4):
            bounded = self.calculate(order, observed)
        self.assertEqual(bounded.result['status'], 'READY')
        self.assertFalse(bounded.result['optimization']['search_complete'])
        self.client.force_login(self.operator)
        response = self.client.get(f'/plans/{bounded.pk}/')
        self.assertContains(response, '탐색 한도 내 추천')
        self.assertContains(response, '최적성은 확정하지 않았습니다')
        with patch('harbor.spatial.SPATIAL_SEARCH_LIMIT', 1):
            held = self.calculate(order, observed)
        self.assertEqual(held.result['status'], 'HOLD')
        self.assertEqual(held.result['moves'], [])
        response = self.client.get(f'/plans/{held.pk}/')
        self.assertContains(response, '완전한 추천안 없음')

    def test_historical_plan_without_report_does_not_invent_scores(self):
        order, observed = self.prepared()
        old = self.calculate(order, observed)
        result = deepcopy(old.result)
        result.pop('optimization')
        old = Plan.objects.create(created_by=self.operator, order=order, order_version=1,
            observation=observed, condition=observed.condition, queue_digest=old.queue_digest, result=result)
        self.assertIsNone(explain_plan(old)['optimization'])
        self.client.force_login(self.operator)
        response = self.client.get(f'/plans/{old.pk}/')
        self.assertContains(response, '이 계획에는 후보 비교 자료가 없습니다')
        self.assertNotContains(response, '같은 구역 주소 차이 합')

    def test_better_restoration_than_first_feasible_is_explained(self):
        target, lower, upper = self.cargo(), self.cargo('b', 2), self.cargo('a', 1)
        condition = services.register_condition(self.admin, name='Ranked fixture', calibration_ref='synthetic-only', payload={
            'outbound_stand': 'out', 'temporary_bays': ['temp'], 'conveyor': 'belt',
            'allowed_moves': [['Y11', 'out']], 'max_observation_age_seconds': 60,
            'arm_reach': [['A', location] for location in ['Y11', 'Y12', 'Y13', 'temp', 'out']],
            'yard_layout': dict(rows=1, columns=3, levels=3,
                cells=[dict(bay_id=f'Y1{column}', row=1, column=column) for column in range(1, 4)]),
            'spatial_areas': [dict(area_id='temp', rows=1, columns=3, levels=1),
                              dict(area_id='out', rows=1, columns=2, levels=1)],
            'spatial_moves': [['yard', 'out'], ['yard', 'temp'], ['temp', 'yard']],
        })
        payload = self.payload(target)
        for cargo, level in [(lower, 2), (upper, 3)]:
            payload['placements'].append(dict(payload['placements'][0], cargo_id=cargo.code,
                level=level, footprint_revision=CargoFootprint.objects.get(cargo=cargo).pk))
        observed = self.observe(condition, payload)
        target_order = self.order(target)
        # 'a' leaves before 'b', so restacking 'a' on 'b' is free (design 32 order table).
        for cargo, group in [(upper, 2), (lower, 3)]:
            services.create_order(self.operator, cargo=cargo, kind='OUTBOUND', priority_group=group,
                                  sequence=1, tie_policy='FIXED')
        plan = self.calculate(target_order, observed)
        report = plan.result['optimization']
        self.assertEqual(report['first_feasible_score']['different_position_count'], 1)
        self.assertEqual(report['score']['different_position_count'], 0)
        self.assertTrue(explain_plan(plan)['optimization']['improved'])
        self.client.force_login(self.operator)
        self.assertContains(self.client.get(f'/plans/{plan.pk}/'), '첫 번째 성립안보다 복귀 배치 점수가 나은 계획')
