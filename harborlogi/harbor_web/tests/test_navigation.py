from django.test import TestCase
from harbor_web.tests import test_services as fixtures


class NavigationTests(TestCase):
    setUp = fixtures.ServicesTests.setUp

    def test_icons_keep_accessible_names_and_role_gates(self):
        self.client.force_login(self.operator)
        response = self.client.get('/outbound/')
        self.assertContains(response, 'aria-label="반출 관제"')
        self.assertContains(response, 'data-nav-tip="반출 관제 · 우선순위·재취급 계획"')
        self.assertContains(response, 'id="nav-tooltip"')
        self.assertContains(response, 'class="nav-icon"', count=10)
        self.assertNotContains(response, 'href="/settings/"')
        self.assertContains(response, 'aria-label="현장 확인"')
        self.assertContains(response, 'aria-current="page"')
