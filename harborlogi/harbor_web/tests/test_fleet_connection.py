import io
import json
import os
import socket
import ssl
from urllib.error import HTTPError, URLError
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import Client, TransactionTestCase

from harbor_web import commands, fleet_connection as bridge
from harbor_web.models import Audit, Command, FleetConnection
from harbor_web.tests import test_services as fixtures


class FleetConnectionTests(TransactionTestCase):
    cargo = fixtures.ServicesTests.cargo
    condition = fixtures.ServicesTests.condition
    observation = fixtures.ServicesTests.observation
    order = fixtures.ServicesTests.order

    def setUp(self):
        fixtures.ServicesTests.setUp(self)
        self.environment = patch.dict(os.environ, {
            'HARBOR_FLEET_ORIGIN': '', 'HARBOR_FLEET_PRINCIPAL': '',
            'HARBOR_FLEET_ALLOWED_ORIGINS': 'https://fleet.example',
            'HARBOR_FLEET_TOKEN': 'private-bridge-token',
            'HARBOR_LEDGER_PATH': '/private/ledger.db', 'HARBOR_BINDINGS_PATH': '/private/bindings.json',
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def save(self, **overrides):
        fields = dict(origin='https://fleet.example', principal='harbor-service',
                      arm_console_enabled=True, expected_version=0)
        fields.update(overrides)
        return bridge.save_connection(self.admin, **fields)

    def missing(self, code='ARM_TRANSFER_JOB_NOT_FOUND', status=404):
        return HTTPError('https://fleet.example/probe', status, 'not returned to UI', {},
                         io.BytesIO(json.dumps({'detail': {'code': code}}).encode()))

    def rest(self, code=None):
        # evidence, recheck, proposal, execution-profile probes; a generic 404 means the route is absent
        return [self.missing(code) for _ in range(4)]

    def test_saved_connection_feeds_existing_execution_and_contains_no_token(self):
        self.save()
        config = commands._config()
        self.assertEqual((config['origin'], config['principal']), ('https://fleet.example', 'harbor-service'))
        self.assertEqual(config['token'], 'private-bridge-token')
        self.assertNotIn('private-bridge-token', repr(FleetConnection.objects.values().get()))
        self.assertNotIn('private-bridge-token', repr(list(Audit.objects.values())))
        with self.assertRaises(ValidationError):
            self.save(origin='https://other.example', expected_version=1)
        with self.assertRaises(ValidationError):
            self.save(expected_version=0)

    def test_probe_verifies_identity_and_structured_read_only_arm_route(self):
        self.save()
        with patch('harbor.rosy_http.RosyClient._http', side_effect=[
            {'principal_id': 'harbor-service', 'role': 'service'}, self.missing(), *self.rest(),
        ]) as http:
            result = bridge.check_connection(self.admin, expected_version=1)
        self.assertEqual(result['status'], 'VERIFIED')
        self.assertTrue(all(call.args[0] == 'GET' for call in http.call_args_list))
        self.assertEqual(Command.objects.count(), 0)
        self.assertEqual(bridge.last_check().detail['status'], 'VERIFIED')
        with patch.dict(os.environ, HARBOR_FLEET_TOKEN='rotated-private-token'):
            self.assertIsNone(bridge.last_check())

    def test_failed_probe_never_treats_generic_404_or_wrong_identity_as_support(self):
        self.save()
        for session, error, expected in [
            ({'principal_id': 'other', 'role': 'service'}, None, 'PRINCIPAL_MISMATCH'),
            ({'principal_id': 'harbor-service', 'role': 'viewer'}, None, 'ROLE_NOT_SUITABLE'),
            ({'principal_id': 'harbor-service', 'role': 'operator'}, None, 'ROLE_NOT_SUITABLE'),
            ({'principal_id': 'harbor-service', 'role': 'service'}, self.missing('Not Found'), 'ARM_API_UNCONFIRMED'),
            ({'principal_id': 'harbor-service', 'role': 'service'}, self.missing('ARM_TRANSFER_JOB_UNAVAILABLE', 503), 'ARM_NOT_INSTALLED'),
        ]:
            with self.subTest(expected=expected):
                responses = [session] + ([error, *self.rest()] if error else [])
                with patch('harbor.rosy_http.RosyClient._http', side_effect=responses) as http:
                    result = bridge.check_connection(self.admin, expected_version=1)
                self.assertEqual(result['reason'], expected)
                self.assertNotEqual(result['status'], 'VERIFIED')
                self.assertEqual(http.call_count, 6 if error else 1)

    def test_get_is_read_only_and_only_admin_can_change_or_probe(self):
        self.save()
        self.client.force_login(self.admin)
        count = Audit.objects.count()
        with patch('harbor.rosy_http.RosyClient._http', side_effect=AssertionError('GET contacted Fleet')):
            response = self.client.get('/settings/integration/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Audit.objects.count(), count)
        self.assertNotContains(response, 'private-bridge-token')
        self.client.force_login(self.operator)
        self.assertEqual(self.client.post('/settings/integration/', {'action': 'check', 'expected_version': 1}).status_code, 403)
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.admin)
        self.assertEqual(csrf_client.post('/settings/integration/', {'action': 'check', 'expected_version': 1}).status_code, 403)

    def test_environment_origin_is_deployment_owned_and_not_overridden_by_ui(self):
        with patch.dict(os.environ, HARBOR_FLEET_ORIGIN='https://managed.example', HARBOR_FLEET_PRINCIPAL='managed-service'):
            self.assertEqual(bridge.connection_settings()['source'], 'deployment')
            with self.assertRaises(ValidationError):
                self.save()
        self.client.force_login(self.admin)
        with patch.dict(os.environ, HARBOR_FLEET_ORIGIN='ftp://invalid.example', HARBOR_FLEET_PRINCIPAL='managed-service'):
            with patch('harbor.rosy_http.RosyClient._http', side_effect=AssertionError('GET contacted Fleet')):
                response = self.client.get('/settings/integration/')
            self.assertFalse(response.context['connection_guidance']['can_check'])

    def test_missing_token_and_disallowed_saved_origin_never_contact_network(self):
        self.save()
        self.client.force_login(self.admin)
        for override in [{'HARBOR_FLEET_TOKEN': ''}, {'HARBOR_FLEET_ALLOWED_ORIGINS': ''}]:
            with patch.dict(os.environ, override), patch('harbor.rosy_http.RosyClient._http', side_effect=AssertionError('unsafe request')):
                result = bridge.check_connection(self.admin, expected_version=1)
                response = self.client.get('/settings/integration/')
                self.assertFalse(response.context['connection_guidance']['can_check'])
                self.assertContains(response, ' disabled>Fleet 연결 확인')
            self.assertEqual(result['status'], 'FAILED')

    def test_existing_command_locks_identity_and_timeout_is_sanitized(self):
        from harbor_web import services
        self.save()
        order = self.order()
        observation = self.observation()
        plan = services.make_plan(self.operator, order_id=order.pk, expected_version=1, observation_id=observation.pk)
        Command.objects.create(plan=plan, order=order, actor=self.operator, plan_version=1,
            ledger_identity='preserved', fleet_origin='https://fleet.example', fleet_principal='harbor-service',
            body_digest='a' * 64, binding={})
        with self.assertRaisesMessage(ValidationError, 'EXISTING_EXECUTION_IDENTITY_LOCKED'):
            self.save(principal='different-service', expected_version=1)
        with patch('harbor.rosy_http.RosyClient._http', side_effect=TimeoutError('private-bridge-token')):
            result = bridge.check_connection(self.admin, expected_version=1)
        self.assertEqual(result['reason'], 'FLEET_TIMEOUT')
        self.assertNotIn('private-bridge-token', repr(list(Audit.objects.values())))

    def test_network_failures_name_only_the_verified_cause(self):
        self.save()
        for error, reason in [
            (URLError(socket.gaierror('private-bridge-token')), 'FLEET_DNS_UNRESOLVED'),
            (URLError(ssl.SSLCertVerificationError('private-bridge-token')), 'FLEET_TLS_UNTRUSTED'),
            (ssl.SSLError('private-bridge-token'), 'FLEET_TLS_FAILED'),
            (URLError(TimeoutError('private-bridge-token')), 'FLEET_TIMEOUT'),
            (URLError('unclassified private-bridge-token'), 'FLEET_UNREACHABLE'),
        ]:
            with self.subTest(reason=reason), patch('harbor.rosy_http.RosyClient._http', side_effect=error) as http:
                result = bridge.check_connection(self.admin, expected_version=1)
                self.assertEqual(result['reason'], reason)
                self.assertEqual(result['status'], 'FAILED')
                self.assertEqual(http.call_count, 1)
                self.assertNotIn('private-bridge-token', repr(bridge.last_check().detail))

    def test_saved_partial_check_keeps_identity_success_and_explains_next_step(self):
        self.save()
        with patch('harbor.rosy_http.RosyClient._http', side_effect=[
            {'principal_id': 'harbor-service', 'role': 'service'},
            self.missing('ARM_TRANSFER_JOB_UNAVAILABLE', 503), *self.rest(),
        ]):
            bridge.check_connection(self.admin, expected_version=1)
        self.client.force_login(self.admin)
        count = Audit.objects.count()
        with patch('harbor.rosy_http.RosyClient._http', side_effect=AssertionError('GET contacted Fleet')):
            response = self.client.get('/settings/integration/')
        self.assertContains(response, '서비스 신원 확인됨')
        self.assertContains(response, '작업 조회 API')
        self.assertContains(response, '설치 필요')
        self.assertEqual(response.context['connection_guidance']['steps'][1]['state'], '확인됨')
        self.assertEqual(response.context['connection_guidance']['steps'][2]['state'], '설치 필요')
        self.assertContains(response, 'ROSY 설치 담당자')
        self.assertContains(response, '다음 조치')
        self.assertNotContains(response, '조회 연동 확인</span>')
        self.assertEqual(Audit.objects.count(), count)

    def test_saved_profile_identity_is_in_private_backup_manifest(self):
        import tempfile
        import sqlite3
        from contextlib import closing
        from pathlib import Path
        from django.db import connection
        from harbor_web.models import DeploymentState
        from harbor_web.runtime import backup_product
        self.save()
        DeploymentState.objects.update_or_create(pk=1, defaults={'mode': 'test'})
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'web.sqlite3'
            with closing(sqlite3.connect(source)) as target:
                connection.connection.backup(target)
            backup_product(root / 'backup', source, None, 'test')
            manifest = json.loads((root / 'backup/manifest.json').read_text())
            self.assertEqual((manifest['fleet_origin'], manifest['fleet_principal']), ('https://fleet.example', 'harbor-service'))
            self.assertNotIn('private-bridge-token', repr(manifest))

    def test_inflight_connection_change_cannot_publish_a_success(self):
        self.save()
        def changed(*args):
            os.environ['HARBOR_FLEET_TOKEN'] = 'rotated-while-checking'
            return {'principal_id': 'harbor-service', 'role': 'service'}
        calls = 0
        def response(*args):
            nonlocal calls
            calls += 1
            if calls == 1:
                return changed()
            raise self.missing()
        with patch('harbor.rosy_http.RosyClient._http', side_effect=response):
            with self.assertRaisesMessage(ValidationError, 'CONNECTION_CHANGED'):
                bridge.check_connection(self.admin, expected_version=1)
        self.assertFalse(Audit.objects.filter(action='fleet.check').exists())
