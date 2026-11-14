from django.contrib.auth.models import Group, User
from django.test import TestCase
from harbor_web import access, scenarios


class WorkflowGuideTests(TestCase):
    def setUp(self):
        for name in ('viewer', 'operator', 'field', 'admin'):
            User.objects.create_user(name).groups.add(Group.objects.get_or_create(name=name)[0])

    def get(self, name):
        self.client.force_login(User.objects.get(username=name))
        return self.client.get('/workflow/')

    def test_every_step_points_to_a_registered_page(self):
        self.assertEqual([s.key for s in scenarios.SCENARIOS], [f'S{n}' for n in range(1, 12)])
        for scenario in scenarios.SCENARIOS:
            self.assertIn(scenario.persona, access.PERSONAS)
            self.assertTrue(scenario.steps)
            for step in scenario.steps:
                self.assertEqual(access.page(step.page).key, step.page)

    def test_field_sees_own_scenario_first(self):
        html = self.get('field').content.decode()
        mine, others = html.split('다른 담당의 일')
        self.assertIn('S5', mine)
        self.assertNotIn('/requests/new/', html)
        self.assertIn('입고·반출 지시', others)
        self.assertIn('<details', html[html.index('다른 담당의 일') - 40:])

    def test_operator_steps_link_only_openable_pages(self):
        html = self.get('operator').content.decode()
        self.assertIn('href="/cargo/new/"', html)
        self.assertNotIn('href="/inventory/import/"', html)
        self.assertIn('설치·운영 관리', html)

    def test_existing_guidance_is_kept(self):
        self.assertContains(self.get('viewer'), '현재 운영 연결의 범위')
        self.assertContains(self.get('field'), '5분')
        self.assertContains(self.get('admin'), '설정된 연동과 남은 조건 확인')
