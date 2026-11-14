from django.contrib.auth.models import Group, User
from django.core.management import call_command
from django.test import TestCase


class AccessFallbackTests(TestCase):
    def setUp(self):
        call_command('bootstrap_roles', verbosity=0)

    def login(self, *role_names, **fields):
        user = User.objects.create_user('person', password='long-test-password-472!', **fields)
        for name in role_names:
            user.groups.add(Group.objects.get(name=name))
        self.client.force_login(user)
        return user

    def test_reader_returns_to_overview(self):
        self.login('viewer')
        response = self.client.get('/settings/')
        self.assertEqual(response.status_code, 403)
        self.assertContains(response, '<a href="/">운영 현황으로 돌아가기</a>', status_code=403, html=False)

    def test_field_only_returns_to_field(self):
        self.login('field')
        response = self.client.get('/requests/')
        self.assertEqual(response.status_code, 403)
        self.assertContains(response, '<a href="/field/">현장 확인으로 돌아가기</a>', status_code=403)
        self.assertNotContains(response, '운영 현황으로 돌아가기', status_code=403)

    def test_no_role_user_gets_role_notice(self):
        self.login()
        response = self.client.get('/')
        self.assertEqual(response.status_code, 403)
        self.assertContains(response, '역할이 아직 없습니다', status_code=403)
        self.assertContains(response, 'href="/accounts/password_change/"', status_code=403)
        self.assertNotContains(response, '운영 현황으로 돌아가기', status_code=403)

    def test_account_admin_link_needs_staff(self):
        self.login('admin')
        response = self.client.get('/settings/')
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'href="/admin/')
        self.assertContains(response, '계정 관리에는 staff 권한이 필요합니다')

    def test_staff_admin_sees_account_links(self):
        self.login('admin', is_staff=True)
        response = self.client.get('/settings/')
        self.assertContains(response, 'href="/admin/auth/user/"')
        self.assertNotContains(response, '계정 관리에는 staff 권한이 필요합니다')
