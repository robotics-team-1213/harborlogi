import re

from django.contrib.auth.models import Group, User
from django.test import TestCase
from harbor_web import access, role_guide, scenarios


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

    def test_owner_label_has_no_doubled_word(self):
        self.assertNotContains(self.get('field'), '담당 담당')

    def test_owner_is_the_scenario_persona_when_it_can_open_the_page(self):
        rows = scenarios.scenario_rows({'field'})['others']
        s11 = next(r for r in rows if r['key'] == 'S11')
        self.assertEqual([(bool(s['href']), s['owner']) for s in s11['steps']],
                         [(False, '조회'), (False, '조회'), (True, ''), (False, '조회')])
        s8 = next(r for r in rows if r['key'] == 'S8')
        self.assertEqual([s['owner'] for s in s8['steps']], ['', '설치·운영 관리'])
        s8_unopened = next(r for r in scenarios.scenario_rows(set())['others'] if r['key'] == 'S8')
        self.assertEqual([s['owner'] for s in s8_unopened['steps']], ['설치·운영 관리', '설치·운영 관리'])

    def test_field_scenarios_are_split_between_mine_and_others(self):
        mine = scenarios.scenario_rows({'field'})
        self.assertEqual([r['key'] for r in mine['mine']], ['S5'])
        self.assertNotIn('S5', [r['key'] for r in mine['others']])
        self.assertNotIn('S1', [r['key'] for r in mine['mine']])

    def test_help_renders_four_three_line_role_cards_sorted_with_current_role_first(self):
        expected_menus = {
            role: ' · '.join(p.label for p in access.MENU_PAGES if access.visible_in_menu(p, {role}))
            for role in ('operator', 'field', 'admin', 'viewer')
        }
        for role in ('operator', 'field', 'admin', 'viewer'):
            with self.subTest(role=role):
                response = self.get(role)
                html = response.content.decode()
                self.assertIn('<div class="page-head"><div><h1>도움말</h1></div></div>', html)
                self.assertIn('<details class="fold"><summary>업무 시나리오 자세히(지원 담당자용)</summary>', html)
                cards_section = html.split('<section class="help-cards"', 1)[1].split('</section>', 1)[0]
                order = re.findall(r'data-help-role="([^"]+)"', cards_section)
                self.assertEqual(len(order), 4)
                self.assertEqual(set(order), {'operator', 'field', 'admin', 'viewer'})
                self.assertEqual(order[0], role)
                self.assertNotRegex(cards_section, r'관측|원본|인계|대조|회차|#|READY|HOLD|담당 담당')
                self.assertNotIn('<a ', cards_section)
                cards = role_guide.role_cards({role})
                self.assertEqual([c['role'] for c in cards], order)
                for card in cards:
                    self.assertEqual(len(card['steps']), 3)
                    self.assertIn(f'보는 메뉴: {expected_menus[card["role"]]}', cards_section)
                self.assertEqual(cards_section.count('<ol class="help-steps">'), 4)
                self.assertEqual(cards_section.count('<li>'), 12)
