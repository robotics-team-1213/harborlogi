"""Rosy Account sign-in for Harbor (ADR 0027): authorization code + S256 PKCE against the Rosy hub.

Harbor stays a separate app. The hub decides who you are and which `harbor.*` site capabilities
you hold; Harbor maps them to its own groups and keeps every business permission check local.
The contract is rosy-platform docs/reference/rosy-account-protocol.md, "바깥 앱 수신 계약".
"""
import base64
import hashlib
import ipaddress
import json
import re
import secrets
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login, logout, user_logged_out
from django.contrib.auth import views as auth_views
from django.contrib.auth.models import Group, User
from django.core.exceptions import ImproperlyConfigured
from django.dispatch import receiver
from django.http import Http404, JsonResponse
from django.shortcuts import redirect
from django.utils.http import url_has_allowed_host_and_scheme

ROLE_BY_CAPABILITY = {'harbor.view': 'viewer', 'harbor.operate': 'operator', 'harbor.field': 'field', 'harbor.admin': 'admin'}
ROLES = tuple(ROLE_BY_CAPABILITY.values())
CHECK_EVERY_S = 10  # the contract: re-introspect at most every 10 s (30 s revocation bound)
CALLBACK_PATH = '/accounts/rosy/callback/'
_SUB = re.compile(r'^[A-Za-z0-9._-]{1,128}$')
_RESOURCE = re.compile(r'^site:[A-Za-z0-9._-]{1,96}$')
_SESSION, _FLOW = 'rosy_account', 'rosy_account_flow'


def _origin_ok(url, *, path_allowed=False):
    parsed = urlsplit(url if isinstance(url, str) else '')
    try:
        loopback = parsed.hostname == 'localhost' or ipaddress.ip_address(parsed.hostname or '').is_loopback
    except ValueError:
        loopback = False
    return (parsed.scheme == 'https' or parsed.scheme == 'http' and loopback) and bool(parsed.hostname) \
        and not parsed.username and not parsed.query and not parsed.fragment \
        and (path_allowed or parsed.path in {'', '/'})


def load_config(path):
    """The receiver file: {issuer, client_id, resource, callback}. A wrong file stops startup."""
    try:
        value = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise ImproperlyConfigured('HARBOR_ROSY_ACCOUNT_FILE is unreadable.') from exc
    if (not isinstance(value, dict) or set(value) != {'issuer', 'client_id', 'resource', 'callback'}
            or not all(isinstance(v, str) for v in value.values())
            or not _origin_ok(value['issuer']) or not _origin_ok(value['callback'], path_allowed=True)
            or urlsplit(value['callback']).path != CALLBACK_PATH
            or not _RESOURCE.fullmatch(value['resource'])
            or not re.fullmatch(r'[A-Za-z0-9._-]{1,96}', value['client_id'])):  # Account registers at most 96
        raise ImproperlyConfigured('HARBOR_ROSY_ACCOUNT_FILE needs exact issuer, client_id, site resource and callback.')
    return dict(value, issuer=value['issuer'].rstrip('/'))


class AccountUnavailable(Exception):
    """The hub could not be reached or answered unclearly."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def post_form(url, fields):
    """POST a form to the hub; (status, JSON object). Tests replace this."""
    request = Request(url, data=urlencode(fields).encode(), method='POST',
                      headers={'Content-Type': 'application/x-www-form-urlencoded', 'Accept': 'application/json'})
    try:
        with build_opener(_NoRedirect()).open(request, timeout=10) as response:
            status, raw = response.status, response.read(65537)
    except HTTPError as exc:
        status, raw = exc.code, exc.read(65537)
    except (URLError, OSError, ValueError) as exc:
        raise AccountUnavailable() from exc
    try:
        body = json.loads(raw) if len(raw) <= 65536 else None
    except ValueError:
        body = None
    if status >= 500 or not isinstance(body, dict):
        raise AccountUnavailable()
    return status, body


def _call(endpoint, fields):
    return post_form(settings.ROSY_ACCOUNT['issuer'] + '/api/fleet/auth/' + endpoint, fields)


def _claims(token):
    # The token comes straight from the hub's token endpoint over the server's own HTTPS call, never
    # from the browser, so its payload is read without a local signature check; introspect re-checks it.
    config = settings.ROSY_ACCOUNT
    try:
        payload = token.split('.')[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4)))
    except (AttributeError, IndexError, ValueError):
        return None
    if (not isinstance(claims, dict) or claims.get('iss') != config['issuer'] or claims.get('aud') != config['resource']
            or not isinstance(claims.get('sub'), str) or not _SUB.fullmatch(claims['sub'])
            or not isinstance(claims.get('exp'), int) or claims['exp'] <= time.time()
            or not isinstance(claims.get('grants'), list)):
        return None
    return claims


def granted_roles(claims):
    capabilities = {c for g in claims['grants'] if isinstance(g, dict) and g.get('resource') == claims['aud']
                    for c in g.get('capabilities', ()) if isinstance(c, str)}
    return {role for capability, role in ROLE_BY_CAPABILITY.items() if capability in capabilities}


def _tokens(body):
    claims = _claims(body.get('access_token'))
    if claims is None or not isinstance(body.get('refresh_token'), str):
        return None
    return {'access': body['access_token'], 'refresh': body['refresh_token'], 'exp': claims['exp'],
            'sub': claims['sub'], 'checked': time.time()}, claims


def _sync(user, names):
    user.groups.remove(*user.groups.filter(name__in=ROLES))
    user.groups.add(*Group.objects.filter(name__in=names))
    if user.is_staff != ('admin' in names):
        user.is_staff = 'admin' in names
        user.save(update_fields=['is_staff'])


class LoginView(auth_views.LoginView):
    """The local form stays for recovery accounts; it offers the Rosy path when configured."""
    def get_context_data(self, **kwargs):
        return {**super().get_context_data(**kwargs), 'rosy_account': bool(settings.ROSY_ACCOUNT)}


def start(request):
    config = settings.ROSY_ACCOUNT
    if not config:
        raise Http404
    target = request.GET.get('next', '')
    if not url_has_allowed_host_and_scheme(target, {request.get_host()}, require_https=request.is_secure()):
        target = settings.LOGIN_REDIRECT_URL
    verifier = secrets.token_urlsafe(64)
    state = secrets.token_urlsafe(32)
    request.session[_FLOW] = {'state': state, 'verifier': verifier, 'next': target}
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
    return redirect(config['issuer'] + '/api/fleet/auth/authorize?' + urlencode({
        'response_type': 'code', 'client_id': config['client_id'], 'redirect_uri': config['callback'],
        'scope': config['resource'], 'state': state, 'code_challenge': challenge, 'code_challenge_method': 'S256'}))


def _refuse(request, text):
    messages.error(request, text)
    return redirect('login')


def callback(request):
    config = settings.ROSY_ACCOUNT
    if not config:
        raise Http404
    flow = request.session.pop(_FLOW, None)
    state, code = request.GET.get('state', ''), request.GET.get('code', '')
    if not flow or not secrets.compare_digest(state, flow['state']):
        return _refuse(request, 'Rosy 로그인 요청이 만료되었습니다. 다시 시도하세요.')
    if not code:
        return _refuse(request, 'Rosy 계정이 Harbor 접근을 허용하지 않았습니다.')
    try:
        status, body = _call('token', {'grant_type': 'authorization_code', 'client_id': config['client_id'],
                                       'code': code, 'redirect_uri': config['callback'],
                                       'code_verifier': flow['verifier']})
    except AccountUnavailable:
        return _refuse(request, 'Rosy 계정 서버에 연결하지 못했습니다. 잠시 뒤 다시 시도하세요.')
    found = _tokens(body) if status == 200 else None
    if found is None:
        return _refuse(request, 'Rosy 로그인을 확인하지 못했습니다. 다시 시도하세요.')
    tokens, claims = found
    names = granted_roles(claims)
    if not names:
        _revoke(tokens['refresh'])
        return _refuse(request, 'Rosy 계정에 Harbor 권한이 없습니다. 계정 관리자에게 harbor 권한을 요청하세요.')
    user, created = User.objects.get_or_create(username='rosy-' + claims['sub'])
    if created:
        user.set_unusable_password()
        user.save()
    if not user.is_active:
        _revoke(tokens['refresh'])
        return _refuse(request, '이 계정은 Harbor에서 사용이 중지되었습니다.')
    try:
        status, info = _call('userinfo', {'token': tokens['access']})
        if status == 200 and info.get('account_id') == claims['sub'] and isinstance(info.get('display_name'), str):
            User.objects.filter(pk=user.pk).update(first_name=info['display_name'][:150])
    except AccountUnavailable:
        pass  # The display name is cosmetic; authorization already came from the token.
    _sync(user, names)
    login(request, user, backend='django.contrib.auth.backends.ModelBackend')
    request.session[_SESSION] = tokens
    return redirect(flow['next'])


def session(request):
    """JSON proof of a Rosy sign-in, read by ROSY's site login check (deploy/site/account_login_check.py)."""
    if not settings.ROSY_ACCOUNT:
        raise Http404
    tokens = request.session.get(_SESSION) if request.user.is_authenticated else None
    return JsonResponse({'authenticated': True, 'account_id': tokens['sub']} if tokens else {'authenticated': False})


def _revoke(token):
    try:
        _call('revoke', {'token': token})
    except AccountUnavailable:
        pass  # shortcut: no durable retry queue; the hub family still expires on its own. Add one if outages linger.


@receiver(user_logged_out)
def _central_logout(sender, request, user, **kwargs):
    tokens = request.session.get(_SESSION) if request is not None and settings.ROSY_ACCOUNT else None
    if tokens:
        _revoke(tokens['refresh'])


class RosyAccountMiddleware:
    """Re-check a Rosy session every 10 s; refresh on rotation, sign out on revocation."""
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        tokens = request.session.get(_SESSION) if settings.ROSY_ACCOUNT else None
        if tokens and request.user.is_authenticated and time.time() - tokens['checked'] >= CHECK_EVERY_S:
            self._check(request, tokens)
        return self.get_response(request)

    def _check(self, request, tokens):
        config = settings.ROSY_ACCOUNT
        try:
            status, body = _call('introspect', {'token': tokens['access']})
            if status == 200 and body.get('active') is True and body.get('sub') == tokens['sub']:
                request.session[_SESSION] = dict(tokens, checked=time.time())
                return
            status, body = _call('token', {'grant_type': 'refresh_token', 'client_id': config['client_id'],
                                           'refresh_token': tokens['refresh']})
        except AccountUnavailable:
            # Hub outage: the last verified access stays valid only until its own expiry.
            if time.time() >= tokens['exp']:
                self._end(request)
            return
        found = _tokens(body) if status == 200 else None
        if found is None or found[1]['sub'] != tokens['sub'] or not granted_roles(found[1]):
            self._end(request)
            return
        _sync(request.user, granted_roles(found[1]))
        request.session[_SESSION] = found[0]

    @staticmethod
    def _end(request):
        request.session.pop(_SESSION, None)  # already invalid at the hub; skip the revoke call
        logout(request)
