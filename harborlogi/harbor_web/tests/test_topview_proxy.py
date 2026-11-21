"""탑뷰 프록시의 역할·출처 확인과 자격 비노출 시험."""
import os
from unittest.mock import patch

from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.urls import reverse

from harbor.rosy_vision import StaleFrameError


class TopviewProxyTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('topview-reader')
        self.user.groups.add(Group.objects.get_or_create(name='viewer')[0])
        self.client.force_login(self.user)
        self.url = reverse('topview_frame', args=['test-cam'])
        self.env = patch.dict(os.environ, {
            'HARBOR_FLEET_ORIGIN': 'https://fleet.example',
            'HARBOR_FLEET_TOKEN': 'test-fleet-secret',
            'HARBOR_FLEET_CA_FILE': '',
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.observations = patch('harbor_web.topview_proxy.RosyClient')
        self.reader = self.observations.start().return_value
        self.addCleanup(self.observations.stop)
        self.reader.workcell_observations.return_value = {
            'workcells': [{'cameras': [{'source_id': 'test-cam', 'role': 'top'}]}]}
        self.vision = patch('harbor_web.topview_proxy.VisionClient')
        self.camera = self.vision.start().return_value
        self.addCleanup(self.vision.stop)
        self.camera.lease.return_value = {
            'source_id': 'test-cam', 'lease': 'test-lease-secret',
            'frame_path': '/api/vision/sources/test-cam/frame'}
        self.camera.fetch_frame.return_value = (b'jpeg-test', {
            'X-Frame-Seq': '42', 'Authorization': 'test-lease-secret',
            'Set-Cookie': 'test-lease-secret', 'X-Frame-Untrusted': 'test-lease-secret'})

    def test_frame_and_only_selected_headers_are_returned(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b'jpeg-test')
        self.assertEqual(response['Content-Type'], 'image/jpeg')
        self.assertIn('no-store', response['Cache-Control'])
        self.assertEqual(response['X-Frame-Seq'], '42')
        self.assertNotIn('X-Frame-Untrusted', response)
        self.assertNotIn('test-lease-secret', str(dict(response.headers)))
        self.assertNotIn(b'test-lease-secret', response.content)

    def test_login_and_read_roles_are_required(self):
        self.client.logout()
        self.assertEqual(self.client.get(self.url).status_code, 302)
        self.client.force_login(self.user)
        for role, status in (('viewer', 200), ('operator', 200), ('admin', 200),
                             ('field', 200), (None, 403)):
            with self.subTest(role=role):
                self.user.groups.clear()
                if role is not None:
                    self.user.groups.add(Group.objects.get_or_create(name=role)[0])
                self.assertEqual(self.client.get(self.url).status_code, status)

    def test_only_get_is_allowed(self):
        for method in ('post', 'put', 'delete', 'head'):
            self.assertEqual(getattr(self.client, method)(self.url).status_code, 405)

    def test_invalid_frame_header_becomes_unavailable(self):
        self.camera.fetch_frame.return_value = (b'jpeg-test', {'X-Frame-Seq': '42\r\n folded'})
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 502)

    def test_missing_settings(self):
        for name in ('HARBOR_FLEET_ORIGIN', 'HARBOR_FLEET_TOKEN'):
            with self.subTest(name=name), patch.dict(os.environ, {name: ''}):
                response = self.client.get(self.url)
                self.assertEqual(response.status_code, 503)
                self.assertContains(response, 'ROSY 연동 전', status_code=503)

    def test_unknown_camera_is_rejected_before_leasing(self):
        response = self.client.get(reverse('topview_frame', args=['unknown']))
        self.assertEqual(response.status_code, 404)
        self.camera.lease.assert_not_called()

    def test_unusable_peer_source_does_not_block_known_camera(self):
        self.reader.workcell_observations.return_value['workcells'][0]['cameras'].append({})
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b'jpeg-test')

    def test_stale_and_network_errors_do_not_expose_credentials(self):
        for error, status in ((StaleFrameError('test-lease-secret'), 404),
                              (OSError('test-lease-secret'), 502),
                              (ValueError('test-lease-secret'), 502)):
            with self.subTest(error=type(error).__name__):
                self.camera.fetch_frame.side_effect = error
                response = self.client.get(self.url)
                self.assertEqual(response.status_code, status)
                self.assertNotIn(b'test-lease-secret', response.content)
                self.assertNotIn('test-lease-secret', str(dict(response.headers)))
