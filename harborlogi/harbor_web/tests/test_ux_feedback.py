"""Screen feedback from the 2026-11-04 review (KAN-115): back button, overview actions, sorting, labels."""
from datetime import datetime, timezone as dt_timezone

from django.contrib.auth.models import Group, User
from django.template.loader import render_to_string
from django.test import TestCase

from harbor_web import services
from harbor_web.models import Observation


class UxFeedbackTests(TestCase):
    def setUp(self):
        self.operator = User.objects.create_user('operator')
        self.operator.groups.add(Group.objects.get_or_create(name='operator')[0])
        self.client.force_login(self.operator)

    def cargo(self, code, weight, volume):
        return services.register_cargo(self.operator, code=code, name=code, weight_kg=weight, volume_m3=volume, destination='반출대')

    def test_every_signed_in_page_offers_a_hidden_back_button(self):
        for path in ('/', '/cargo/', '/outbound/'):
            self.assertContains(self.client.get(path), 'data-history-back hidden')

    def test_overview_page_actions_are_buttons_as_in_the_control_mockup(self):
        html = self.client.get('/').content.decode()
        self.assertIn('<a class="button quiet" href="/requests/new/">업무 요청 등록</a>', html)
        self.assertIn('<a class="button" href="/outbound/">반출 관제 열기</a>', html)
        self.assertIn('class="attention-list"', html)
        self.assertNotIn('<span aria-hidden="true">→</span>', html)

    def test_cargo_list_sorts_by_weight_and_volume(self):
        self.cargo('LIGHT', '1.5', '0.300')
        self.cargo('HEAVY', '9.0', '0.100')
        self.cargo('MID', '4.0', '0.200')
        def codes(query):
            response = self.client.get('/cargo/' + query)
            return [cargo.code for cargo in response.context['page']]
        self.assertEqual(codes('?sort=-weight'), ['HEAVY', 'MID', 'LIGHT'])
        self.assertEqual(codes('?sort=weight'), ['LIGHT', 'MID', 'HEAVY'])
        self.assertEqual(codes('?sort=-volume'), ['LIGHT', 'MID', 'HEAVY'])
        self.assertEqual(codes('?sort=bogus'), ['MID', 'HEAVY', 'LIGHT'])
        response = self.client.get('/cargo/?sort=-weight')
        self.assertContains(response, 'aria-sort="descending"')
        self.assertContains(response, 'sort=weight')

    def test_observation_label_drops_the_raw_revision(self):
        observation = Observation(pk=7, source_id='yard-camera', source_revision='2026-11-05T11:03:56.725430+00:00',
                                  observed_at=datetime(2026, 11, 5, 11, 3, tzinfo=dt_timezone.utc), provenance='SYNTHETIC')
        self.assertEqual(observation.display_label, '#7 · yard-camera · 11-05 20:03 기준 · 합성 자료')

    def test_recommendation_shows_two_candidates_and_folds_the_rest(self):
        candidate = dict(score=dict(different_area_count=0, different_position_count=0, same_area_manhattan_distance=0, final_level_sum=1), moves=[])
        optimization = dict(score=candidate['score'], quality_label='추천', improved=False, score_rows=[], search_complete=True,
                            alternatives=[candidate] * 5, complete_plans_compared=5, expanded_states=1, examined_candidates=1,
                            objective_version='v', state_limit=1, candidate_limit=1)
        html = render_to_string('planning_recommendation.html', dict(optimization=optimization, candidate_plan_id=3))
        folded = html.split('<details class="more-candidates">', 1)
        self.assertEqual(len(folded), 2)
        self.assertEqual(folded[0].count('class="recommendation-candidate"'), 2)
        self.assertIn('나머지 후보 3개 더 보기', folded[1])
        two = render_to_string('planning_recommendation.html', dict(optimization=dict(optimization, alternatives=[candidate] * 2), candidate_plan_id=3))
        self.assertNotIn('more-candidates', two)
