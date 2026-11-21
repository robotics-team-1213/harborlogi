"""두 야드 화면의 탑뷰 목록·장애 격리·읽기 권한 시험."""
import os
from unittest.mock import patch

from django.contrib.auth.models import Group
from django.test import TestCase

from harbor_web.tests import test_multicell_services as fixtures


class TopviewPagesTests(TestCase):
    condition = fixtures.MulticellServicesTests.condition
    cargo = fixtures.MulticellServicesTests.cargo
    payload = fixtures.MulticellServicesTests.payload
    observe = fixtures.MulticellServicesTests.observe

    def setUp(self):
        fixtures.MulticellServicesTests.setUp(self)
        self.client.force_login(self.operator)
        env = patch.dict(os.environ, {
            'HARBOR_FLEET_ORIGIN': 'https://fleet.example',
            'HARBOR_FLEET_TOKEN': 'test-fleet-secret',
            'HARBOR_FLEET_CA_FILE': '',
        })
        env.start()
        self.addCleanup(env.stop)
        reader = patch('harbor_web.topview_proxy.RosyClient')
        self.reader_class = reader.start()
        self.addCleanup(reader.stop)
        self.reader = self.reader_class.return_value
        self.reader.workcell_observations.return_value = {
            'workcells': [{'cameras': [{'source_id': 'cam-one'}, {'source_id': 'cam-two'}]}]}

    def pages(self):
        for path in ('/', '/inventory/'):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, 'ROSY 탑뷰', count=1)
            yield response

    def test_missing_settings_are_visible_even_without_yard_observation(self):
        for key in ('HARBOR_FLEET_ORIGIN', 'HARBOR_FLEET_TOKEN'):
            with self.subTest(key=key), patch.dict(os.environ, {key: ''}):
                for response in self.pages():
                    self.assertContains(response, 'ROSY 연동 전')
                    self.assertNotContains(response, 'data-topview-source')
        self.reader.workcell_observations.assert_not_called()

    def test_shared_panel_and_camera_selection_next_to_observed_yard(self):
        self.observe(self.condition(), self.payload(self.cargo()))
        for response in self.pages():
            self.assertTemplateUsed(response, 'topview_panel.html')
            self.assertContains(response, 'data-topview-source', count=1)
            self.assertContains(response, 'value="/topview/cam-one/frame/" selected')
            self.assertContains(response, 'value="/topview/cam-two/frame/"')
            self.assertContains(response, 'class="yard-topview"', count=1)
            self.assertContains(response, 'harbor_web/topview.mjs')
            self.assertIn('blob:', response['Content-Security-Policy'])
            self.assertNotContains(response, 'test-fleet-secret')
        self.assertEqual(self.reader_class.call_args.kwargs['timeout'], 2)

    def test_one_camera_starts_directly_without_selector(self):
        self.reader.workcell_observations.return_value = {
            'workcells': [{'cameras': [{'source_id': 'cam-one'}, {'source_id': 'cam-one'}]}]}
        for response in self.pages():
            self.assertContains(response, 'data-topview-url="/topview/cam-one/frame/"')
            self.assertNotContains(response, 'data-topview-source')

    def test_invalid_peer_sources_do_not_hide_a_valid_camera(self):
        self.reader.workcell_observations.return_value = {'workcells': [{'cameras': [
            {}, {'source_id': None}, {'source_id': 'bad/source'}, {'source_id': '.'},
            {'source_id': '..'}, {'source_id': 'cam-one'},
        ]}]}
        for response in self.pages():
            self.assertContains(response, 'data-topview-url="/topview/cam-one/frame/"')
            self.assertNotContains(response, 'data-topview-source')

    def test_connection_errors_leave_both_pages_available(self):
        for error in (OSError('test-fleet-secret'), ValueError('invalid response')):
            with self.subTest(error=type(error).__name__):
                self.reader.workcell_observations.side_effect = error
                for response in self.pages():
                    self.assertContains(response, 'ROSY 연결 안 됨')
                    self.assertNotContains(response, 'test-fleet-secret')

    def test_empty_or_malformed_cameras_leave_both_pages_available(self):
        for snapshot in ({}, {'workcells': None}, {'workcells': [None]},
                         {'workcells': [{'cameras': {}}]}, {'workcells': [{'cameras': [None]}]},
                         {'workcells': [{'cameras': [{'source_id': None}]}]},
                         {'workcells': [{'cameras': [{'source_id': 'bad/source'}]}]},
                         {'workcells': []}):
            with self.subTest(snapshot=snapshot):
                self.reader.workcell_observations.return_value = snapshot
                for response in self.pages():
                    self.assertContains(response, 'ROSY 연결 안 됨')

    def test_all_read_roles_can_see_the_panel(self):
        for role in ('viewer', 'operator', 'admin'):
            with self.subTest(role=role):
                self.operator.groups.set([Group.objects.get_or_create(name=role)[0]])
                list(self.pages())

    def test_field_only_users_see_panel_on_inventory_without_redirect_lookup(self):
        self.operator.groups.set([Group.objects.get_or_create(name='field')[0]])
        redirect_response = self.client.get('/')
        self.assertEqual((redirect_response.status_code, redirect_response['Location']), (302, '/alerts/'))
        self.assertEqual(self.client.get('/alerts/').status_code, 200)
        self.assertEqual(self.client.get('/settings/').status_code, 403)
        self.reader.workcell_observations.assert_not_called()
        inventory_response = self.client.get('/inventory/')
        self.assertEqual(inventory_response.status_code, 200)
        self.assertContains(inventory_response, 'ROSY 탑뷰', count=1)
