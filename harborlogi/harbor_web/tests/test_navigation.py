import re

from django.contrib.auth.models import Group, User
from django.test import TestCase
from harbor_web.models import FieldEpisode
from harbor_web.tests import test_services as fixtures


def menu_of(response):
    return re.search(r'<nav aria-label="주 메뉴">(.*?)</nav>', response.content.decode(), re.S).group(1)


class NavigationTests(TestCase):
    setUp = fixtures.ServicesTests.setUp
    cargo = fixtures.ServicesTests.cargo
    condition = fixtures.ServicesTests.condition
    observation = fixtures.ServicesTests.observation
    order = fixtures.ServicesTests.order

    def test_icons_keep_accessible_names_and_role_gates(self):
        self.client.force_login(self.operator)
        response = self.client.get('/work/')
        for label in ('상황판', '작업', '야드', '처리할 것', '기록', '도움말'):
            self.assertContains(response, f'aria-label="{label}"')
        self.assertContains(response, 'data-nav-tip="작업 · 오늘 시간표와 반출 순서"')
        self.assertContains(response, 'id="nav-tooltip"')
        self.assertContains(response, 'class="nav-icon"', count=7)
        self.assertContains(response, 'd="M18 8a6 6 0 0 0-12 0')
        self.assertNotContains(response, 'href="/settings/"')
        self.assertNotContains(response, 'href="/outbound/"')
        self.assertContains(response, 'aria-current="page"')
        self.assertContains(response, '<span class="nav-badge" data-nav-badge>1</span>')

    def test_alerts_badge_tracks_alert_count_and_viewer_menu_hides_alerts(self):
        self.cargo()
        self.observation()
        self.client.force_login(self.operator)
        clean = self.client.get('/work/')
        self.assertNotContains(clean, 'data-nav-badge')

        self.client.force_login(self.field)
        field_clean = self.client.get('/alerts/')
        self.assertNotContains(field_clean, 'data-nav-badge')

        inbound = self.order(self.cargo('IN1'), kind='INBOUND')
        FieldEpisode.objects.create(order=inbound, assigned_to=self.field, vehicle_id='v1',
                                    created_by=self.operator, state='OPEN')
        field_alert = self.client.get('/inventory/')
        self.assertContains(field_alert, '<span class="nav-badge" data-nav-badge>1</span>')

        viewer = User.objects.create_user('nav-viewer')
        viewer.groups.add(Group.objects.get_or_create(name='viewer')[0])
        self.client.force_login(viewer)
        viewer_page = self.client.get('/work/')
        self.assertNotIn('href="/alerts/"', menu_of(viewer_page))
        self.assertNotContains(viewer_page, 'data-nav-badge')
