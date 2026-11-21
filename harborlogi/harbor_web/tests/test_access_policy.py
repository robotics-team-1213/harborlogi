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
                    self.assertEqual((response.status_code, response['Location']), (302, '/alerts/'))
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
        self.assertEqual([p.key for p in access.MENU_PAGES],
                         ['overview', 'work', 'inventory', 'alerts', 'records', 'settings', 'workflow'])
        keys = lambda roles: [(g['key'], [p.key for p in g['items']]) for g in access.nav_groups(roles)]
        self.assertEqual(keys({'field'}), [('main', ['inventory', 'alerts', 'workflow'])])
        self.assertEqual(keys({'viewer'}), [('main', ['overview', 'work', 'inventory', 'records', 'workflow'])])
        self.assertEqual(keys({'operator'}), [('main', ['overview', 'work', 'inventory', 'alerts', 'records', 'workflow'])])
        self.assertEqual(keys({'admin'}), [('main', ['overview', 'work', 'inventory', 'alerts', 'records', 'settings', 'workflow'])])
        self.assertEqual(keys(set()), [])

    def test_viewer_opens_alerts_without_showing_it_in_menu(self):
        self.assertTrue(access.can_open({'viewer'}, 'alerts'))
        self.assertFalse(access.visible_in_menu(access.page('alerts'), {'viewer'}))
        self.login('viewer')
        response = self.client.get('/alerts/')
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('href="/alerts/"', menu_of(response))

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
        self.assertContains(response, 'data-nav-group="main"')
        self.assertContains(response, '주 메뉴</p>')
        self.assertContains(response, '<span>업무 담당</span>')
        self.assertNotContains(response, '<span>operator</span>')
        nav = menu_of(response)
        for hidden in ('/settings/', '/outbound/', '/requests/', '/rearrangement/', '/field/'):
            self.assertNotIn(f'href="{hidden}"', nav)

    def test_group_label_has_no_icon_and_keeps_tooltips(self):
        self.login('operator')
        response = self.client.get('/work/')
        self.assertContains(response, 'class="nav-icon"', count=7)
        self.assertContains(response, 'data-nav-tip="작업 · 오늘 시간표와 반출 순서"')
        self.assertEqual(menu_of(response).count('aria-current="page"'), 1)

    def test_crumb_uses_registered_label(self):
        self.login('operator')
        response = self.client.get('/work/')
        self.assertRegex(response.content.decode(), r'<span class="crumb">\s*작업\s*</span>')
        self.login('field')
        field_yard = self.client.get('/inventory/')
        self.assertEqual(field_yard.status_code, 200)
        self.assertRegex(field_yard.content.decode(), r'<span class="crumb">\s*야드\s*</span>')

    def test_settings_page_renders_role_matrix_from_menu_pages(self):
        self.login('admin')
        response = self.client.get('/settings/')
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn('<div class="page-head"><div><h1>설정</h1></div>', html)
        for href in ('/settings/maps/new/', '/settings/conditions/new/', '/settings/integration/'):
            self.assertIn(f'href="{href}"', html)
        self.assertIn('계정 관리에는 staff 권한이 필요합니다', html)
        matrix = re.search(r'<table data-role-matrix>(.*?)</table>', html, re.S).group(1)
        headers = re.findall(r'<th scope="col">([^<]+)</th>', matrix)
        self.assertEqual(headers, ['메뉴', *[access.PERSONAS[r] for r in ('operator', 'field', 'admin', 'viewer')]])
        row_labels = re.findall(r'<th scope="row">([^<]+)</th>', matrix)
        self.assertEqual(row_labels, [p.label for p in access.MENU_PAGES])
        table = response.context['role_table']
        by_key = {row['page'].key: {cell['role']: cell for cell in row['cells']} for row in table['rows']}
        self.assertFalse(by_key['alerts']['viewer']['in_menu'])
        self.assertTrue(by_key['alerts']['viewer']['can_open'])
        self.assertEqual(by_key['alerts']['viewer']['text'], '읽기(메뉴 숨김)')
        self.assertEqual([row['page'].key for row in table['rows'] if by_key[row['page'].key]['field']['in_menu']],
                         ['inventory', 'alerts', 'workflow'])
        self.assertEqual(by_key['settings']['admin']['text'], '읽기·편집')
        self.assertEqual(by_key['work']['admin']['text'], '읽기')
        self.assertEqual(by_key['alerts']['admin']['text'], '읽기')
