import re
from django.contrib.auth.models import Group, User
from django.test import TestCase
from harbor_web import access

PERSONA_USERS = ('viewer', 'operator', 'field', 'admin')


def menu_of(response):
    return re.search(r'<nav aria-label="주 메뉴">(.*?)</nav>', response.content.decode(), re.S).group(1)


class AccessPolicyTests(TestCase):
    def setUp(self):
        for name in PERSONA_USERS:
            User.objects.create_user(name).groups.add(Group.objects.get_or_create(name=name)[0])
        User.objects.create_user('nobody')

    def login(self, name):
        self.client.force_login(User.objects.get(username=name))

    def test_every_static_page_matches_registered_roles(self):
        for page in access.PAGES:
            for name in PERSONA_USERS + ('nobody',):
                self.login(name)
                response = self.client.get(page.path)
                if page.path == '/' and name == 'field':
                    self.assertEqual((response.status_code, response['Location']), (302, '/field/'))
                    continue
                expected = 200 if name in page.roles else 403
                self.assertEqual(response.status_code, expected, (page.key, name))

    def test_inactive_user_is_sent_to_login(self):
        user = User.objects.get(username='field')
        self.login('field')
        user.is_active = False
        user.save()
        for path in ('/', '/field/'):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 302, path)
            self.assertTrue(response['Location'].startswith('/accounts/login/'), path)

    def test_nav_groups_follow_roles(self):
        keys = lambda roles: [(g['key'], [p.key for p in g['items']]) for g in access.nav_groups(roles)]
        self.assertEqual(keys({'field'}), [('field', ['field']), ('help', ['workflow'])])
        self.assertEqual([g for g, _ in keys({'viewer'})], ['work', 'evidence', 'help'])
        self.assertEqual([g for g, _ in keys({'operator'})], ['work', 'evidence', 'field', 'help'])
        self.assertEqual([g for g, _ in keys({'admin'})], ['work', 'evidence', 'field', 'admin', 'help'])
        self.assertEqual(keys(set()), [])

    def test_combined_roles_list_each_page_once(self):
        for roles in ({'operator', 'field'}, {'admin', 'viewer'}):
            items = [p.key for g in access.nav_groups(roles) for p in g['items']]
            self.assertEqual(len(items), len(set(items)), roles)

    def test_no_role_user_sees_no_links(self):
        self.login('nobody')
        response = self.client.get('/accounts/password_change/')
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('href=', menu_of(response))
        self.assertContains(response, '역할 미배정')

    def test_menu_renders_groups_and_persona_names(self):
        self.login('operator')
        response = self.client.get('/workflow/')
        self.assertContains(response, 'data-nav-group="work"')
        self.assertContains(response, 'data-nav-group="field"')
        self.assertNotContains(response, 'data-nav-group="admin"')
        self.assertContains(response, '업무 처리</p>')
        self.assertContains(response, '<span>업무 담당</span>')
        self.assertNotContains(response, '<span>operator</span>')

    def test_group_label_has_no_icon_and_keeps_tooltips(self):
        self.login('operator')
        response = self.client.get('/outbound/')
        self.assertContains(response, 'class="nav-icon"', count=10)
        self.assertContains(response, 'data-nav-tip="반출 관제 · 우선순위·재취급 계획"')
        self.assertEqual(menu_of(response).count('aria-current="page"'), 1)

    def test_crumb_uses_registered_label(self):
        self.login('operator')
        response = self.client.get('/rearrangement/')
        self.assertRegex(response.content.decode(), r'<span class="crumb">\s*자동 재배치\s*</span>')
