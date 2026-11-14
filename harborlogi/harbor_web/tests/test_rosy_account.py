import base64
import json
import tempfile
import time
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from django.contrib.auth.models import User
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.test import TestCase, override_settings

from harbor_web import rosy_account
from harbor_web.auth import roles

CONFIG = {'issuer': 'https://rosy.test', 'client_id': 'harbor', 'resource': 'site:one',
          'callback': 'https://harbor.test/accounts/rosy/callback/'}


def access(capabilities, sub='acct-1', resource='site:one', exp=None):
    claims = {'iss': CONFIG['issuer'], 'aud': resource, 'sub': sub, 'exp': exp or int(time.time()) + 300,
              'grants': [{'resource': resource, 'capabilities': capabilities}]}
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b'=').decode()
    return 'h.' + payload + '.s'


class Hub:
    """Records calls and answers like the Rosy hub's /api/fleet/auth/* endpoints."""
    def __init__(self, capabilities=('harbor.operate',)):
        self.calls, self.capabilities, self.active, self.down = [], list(capabilities), True, False
        self.resource = 'site:one'

    def __call__(self, url, fields):
        endpoint = url.rsplit('/', 1)[1]
        self.calls.append((endpoint, fields))
        if self.down:
            raise rosy_account.AccountUnavailable()
        if endpoint == 'token':
            if not self.capabilities and fields['grant_type'] == 'refresh_token':
                return 400, {'error': 'invalid_grant'}
            return 200, {'access_token': access(self.capabilities, resource=self.resource), 'refresh_token': 'r-' + str(len(self.calls))}
        if endpoint == 'introspect':
            return 200, {'active': True, 'sub': 'acct-1'} if self.active else {'active': False}
        if endpoint == 'userinfo':
            return 200, {'account_id': 'acct-1', 'login': 'kim', 'display_name': 'Kim'}
        return 200, {}


@override_settings(ROSY_ACCOUNT=CONFIG, ALLOWED_HOSTS=['testserver', 'harbor.test'])
class RosyAccountTests(TestCase):
    def setUp(self):
        call_command('bootstrap_roles', verbosity=0)
        self.hub = Hub()
        patcher = mock.patch.object(rosy_account, 'post_form', self.hub)
        patcher.start()
        self.addCleanup(patcher.stop)

    def sign_in(self):
        start = self.client.get('/accounts/rosy/?next=/plans/')
        query = parse_qs(urlsplit(start['Location']).query)
        self.assertEqual(query['code_challenge_method'], ['S256'])
        self.assertEqual(query['scope'], ['site:one'])
        self.assertEqual(query['redirect_uri'], [CONFIG['callback']])
        return self.client.get('/accounts/rosy/callback/', {'state': query['state'][0], 'code': 'c1'})

    def test_the_session_proof_names_only_a_rosy_sign_in(self):
        self.assertEqual(self.client.get('/accounts/rosy/session/').json(), {'authenticated': False})
        self.sign_in()
        response = self.client.get('/accounts/rosy/session/')
        self.assertEqual(response.json(), {'authenticated': True, 'account_id': 'acct-1'})
        self.assertIn('no-store', response['Cache-Control'])  # set for every page by the security middleware

    def test_capabilities_become_harbor_groups(self):
        self.hub.capabilities = ['harbor.view', 'harbor.field', 'fleet.read']
        response = self.sign_in()
        self.assertEqual(response['Location'], '/plans/')
        user = User.objects.get(username='rosy-acct-1')
        self.assertEqual(roles(user), {'viewer', 'field'})
        self.assertFalse(user.has_usable_password())
        self.assertEqual(user.first_name, 'Kim')
        token = dict(self.hub.calls)['token']
        self.assertEqual(token['grant_type'], 'authorization_code')
        self.assertTrue(token['code_verifier'])

    def test_no_harbor_capability_refuses_and_revokes(self):
        self.hub.capabilities = ['fleet.read']
        response = self.sign_in()
        self.assertEqual(response['Location'], '/accounts/login/')
        self.assertFalse(User.objects.filter(username='rosy-acct-1').exists())
        self.assertIn('revoke', [endpoint for endpoint, _ in self.hub.calls])

    def test_a_wrong_state_or_other_site_token_is_refused(self):
        self.client.get('/accounts/rosy/')
        self.assertEqual(self.client.get('/accounts/rosy/callback/', {'state': 'forged', 'code': 'c1'})['Location'],
                         '/accounts/login/')
        self.assertEqual(self.hub.calls, [])
        self.hub.capabilities, self.hub.resource = ['harbor.admin'], 'site:two'
        self.assertEqual(self.sign_in()['Location'], '/accounts/login/')
        self.assertFalse(User.objects.filter(username='rosy-acct-1').exists())

    def test_revocation_at_the_hub_ends_the_harbor_session(self):
        self.sign_in()
        session = self.client.session
        session['rosy_account'] = dict(session['rosy_account'], checked=0)
        session.save()
        self.hub.active, self.hub.capabilities = False, []
        self.client.get('/accounts/login/')
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_a_hub_outage_keeps_the_session_only_until_expiry(self):
        self.sign_in()
        self.hub.down = True
        session = self.client.session
        session['rosy_account'] = dict(session['rosy_account'], checked=0)
        session.save()
        self.client.get('/accounts/login/')
        self.assertIn('_auth_user_id', self.client.session)
        session = self.client.session
        session['rosy_account'] = dict(session['rosy_account'], checked=0, exp=0)
        session.save()
        self.client.get('/accounts/login/')
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_logout_revokes_the_hub_family(self):
        self.sign_in()
        self.client.post('/accounts/logout/')
        endpoint, fields = self.hub.calls[-1]
        self.assertEqual(endpoint, 'revoke')
        self.assertTrue(fields['token'].startswith('r-'))


class ConfigTests(TestCase):
    def write(self, value):
        handle = tempfile.NamedTemporaryFile('w', suffix='.json', delete=False, encoding='utf-8')
        with handle:
            json.dump(value, handle)
        self.addCleanup(Path(handle.name).unlink)
        return handle.name

    def test_the_receiver_file_must_be_exact(self):
        self.assertEqual(rosy_account.load_config(self.write(dict(CONFIG, issuer='https://rosy.test/')))['issuer'],
                         'https://rosy.test')
        for bad in [dict(CONFIG, issuer='http://rosy.test'), dict(CONFIG, resource='robot:one'),
                    dict(CONFIG, callback='https://harbor.test/other/'), dict(CONFIG, extra='x')]:
            with self.assertRaises(ImproperlyConfigured):
                rosy_account.load_config(self.write(bad))

    @override_settings(ROSY_ACCOUNT=None)
    def test_without_a_receiver_file_only_local_login_exists(self):
        self.assertEqual(self.client.get('/accounts/rosy/').status_code, 404)
        self.assertEqual(self.client.get('/accounts/rosy/callback/').status_code, 404)
        self.assertEqual(self.client.get('/accounts/rosy/session/').status_code, 404)
