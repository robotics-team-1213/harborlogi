"""Real-browser bridge acceptance against a synthetic HTTP contract double, not actual Fleet."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import threading
import time
import traceback
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from urllib.request import urlopen
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(os.environ.get('LOCALAPPDATA', '')) / 'Temp/harbor-web-browser'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'runs/fleet-bridge-browser')
    args = parser.parse_args()
    output = args.output.resolve() / ('run-' + uuid.uuid4().hex)
    output.mkdir(parents=True)
    token, password = secrets.token_urlsafe(40), secrets.token_urlsafe(30)
    principal = 'synthetic-harbor-service'
    state = dict(mode='compatible', requests=[])

    class ContractDouble(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass  # Never log headers or credentials.

        def reply(self, status, payload):
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def handle_request(self):
            authorized = self.headers.get('Authorization') == 'Bearer ' + token
            state['requests'].append(dict(method=self.command, path=self.path, authorized=authorized))
            if not authorized:
                self.reply(401, dict(detail='Authentication required'))
                return
            if self.command != 'GET':
                self.reply(405, dict(detail='Synthetic read-only contract double'))
                return
            parsed = urlsplit(self.path)
            if parsed.path == '/api/fleet/session' and not parsed.query:
                self.reply(200, dict(principal_id=principal if state['mode'] != 'mismatch' else 'synthetic-other-service',
                    role='operator' if state['mode'] == 'human-role' else 'service'))
            elif parsed.path == '/api/fleet/arm-transfers/jobs' and set(parse_qs(parsed.query)) == {'request_key'} and parse_qs(parsed.query)['request_key'][0].startswith('harbor-probe-'):
                if state['mode'] == 'not-installed':
                    self.reply(503, dict(detail=dict(code='ARM_TRANSFER_JOB_UNAVAILABLE')))
                else:
                    self.reply(404, dict(detail='Not Found') if state['mode'] == 'generic-404'
                               else dict(detail=dict(code='ARM_TRANSFER_JOB_NOT_FOUND')))
            else:
                self.reply(404, dict(detail='Not Found'))

        do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = handle_request

    fleet = ThreadingHTTPServer(('127.0.0.1', 0), ContractDouble)
    thread = threading.Thread(target=fleet.serve_forever, daemon=True)
    thread.start()
    fleet_origin = f'http://127.0.0.1:{fleet.server_port}'
    result = dict(status='FAILED', output=str(output), actual_fleet=False,
        source='synthetic authenticated stdlib HTTP contract double', checks=[], viewports=[])
    child = None
    log = (output / 'server.log').open('w', encoding='utf-8')
    try:
        try:
            urlopen(fleet_origin + '/api/fleet/session', timeout=2)
            raise AssertionError('Synthetic endpoint accepted missing credentials')
        except HTTPError as error:
            assert error.code == 401
            error.close()
        state['requests'].clear()
        result['checks'].append('Synthetic HTTP endpoint rejects missing credentials before any session data')
        for key in list(os.environ):
            if key.startswith('HARBOR_'):
                del os.environ[key]
        bindings = output / 'bindings.json'
        bindings.write_text('[]', encoding='utf-8')
        os.environ.update(DJANGO_SETTINGS_MODULE='harbor_web.settings', DJANGO_ALLOW_ASYNC_UNSAFE='true',
            HARBOR_WEB_MODE='development', HARBOR_WEB_SECRET_KEY=secrets.token_urlsafe(64),
            HARBOR_WEB_DATABASE=str(output / 'web.sqlite3'), HARBOR_FLEET_ALLOWED_ORIGINS=fleet_origin,
            HARBOR_FLEET_TOKEN=token, HARBOR_LEDGER_PATH=str(output / 'ledger.sqlite3'),
            HARBOR_BINDINGS_PATH=str(bindings))
        import django
        django.setup()
        from django.core.management import call_command
        from django.contrib.auth.models import User, Group
        from harbor_web import commands, fleet_connection as bridge
        from harbor_web.guide_pages import CONNECTION_REASONS
        from harbor_web.models import Audit, Command, FleetConnection
        from playwright.sync_api import sync_playwright
        call_command('migrate', verbosity=0)
        call_command('bootstrap_roles', verbosity=0)
        users = {}
        for role in ('admin', 'operator'):
            user = User.objects.create_user('bridge-' + role, password=password)
            user.groups.add(Group.objects.get(name=role))
            users[role] = user
        with socket.socket() as allocated:
            allocated.bind(('127.0.0.1', 0))
            port = allocated.getsockname()[1]
        origin = f'http://127.0.0.1:{port}'
        child = subprocess.Popen([sys.executable, '-m', 'harbor_web', '--port', str(port)], cwd=ROOT,
            env=os.environ.copy(), stdout=log, stderr=log)
        deadline = time.monotonic() + 30
        while True:
            if child.poll() is not None:
                raise AssertionError('Isolated server exited; inspect server.log')
            try:
                if urlopen(origin + '/accounts/login/', timeout=1).status == 200:
                    break
            except OSError:
                if time.monotonic() >= deadline:
                    raise AssertionError('Isolated server readiness timeout')
                time.sleep(.2)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel='msedge', headless=True)
            page = browser.new_page(viewport=dict(width=1440, height=1000))
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))

            def login(target, role):
                target.goto(origin + '/accounts/login/')
                target.locator('[name=username]').fill(users[role].username)
                target.locator('[name=password]').fill(password)
                target.locator('main button[type=submit]').click()
                target.wait_for_load_state('networkidle')

            def safe_page():
                assert token not in page.content(), 'Service token leaked into rendered HTML'

            # A matched session also runs the ADR0022 readiness GETs: evidence, recheck, trip proposal, platform signal.
            READINESS_PROBES = 4

            def check(mode, reason, expected_requests):
                state['mode'] = mode
                before = len(state['requests'])
                page.get_by_role('button', name='Fleet 연결 확인', exact=True).click()
                page.wait_for_load_state('networkidle')
                audit = Audit.objects.filter(action='fleet.check').latest('pk')
                assert audit.detail['reason'] == reason, audit.detail
                assert len(state['requests']) - before == expected_requests
                assert audit.detail['status'] == ('VERIFIED' if reason == 'READ_API_VERIFIED' else 'FAILED')
                assert page.get_by_text(CONNECTION_REASONS[reason], exact=True).first.is_visible()
                safe_page()

            login(page, 'admin')
            page.goto(origin + '/settings/')
            page.locator('a[href="/settings/integration/"]').click()
            page.wait_for_load_state('networkidle')
            assert not state['requests'], 'Reading settings must not contact Fleet'
            assert page.get_by_role('button', name='Fleet 연결 확인', exact=True).is_disabled()
            safe_page()
            form = page.locator('form:has(input[name=action][value=save])')
            form.locator('[name=origin]').select_option(fleet_origin)
            form.locator('[name=principal]').fill(principal)
            form.locator('[name=arm_console_enabled]').check()
            form.get_by_role('button', name='연결 설정 저장', exact=True).click()
            page.wait_for_load_state('networkidle')
            profile = FleetConnection.objects.get(pk=1)
            assert (profile.origin, profile.principal, profile.version) == (fleet_origin, principal, 1)
            assert profile.arm_console_enabled and not state['requests']
            config = commands._config()
            assert config['origin'] == profile.origin and config['principal'] == profile.principal and config['token'] == token
            assert not os.environ.get('HARBOR_FLEET_ORIGIN') and not os.environ.get('HARBOR_FLEET_PRINCIPAL')
            result['checks'].append('Admin settings link saves allowlisted profile without Fleet traffic; commands._config consumes its origin and principal with server-only token')
            check('compatible', 'READ_API_VERIFIED', 2 + READINESS_PROBES)
            assert page.get_by_text('조회 연동 확인', exact=True).is_visible()
            check('human-role', 'ROLE_NOT_SUITABLE', 1)
            assert page.get_by_text('연결 확인 필요', exact=True).is_visible()
            assert page.get_by_text('사람의 운영자 자격 대신 Harbor 전용 service 역할의 기계 신원을 발급한 뒤 다시 검사하세요.', exact=True).is_visible()
            page.locator('details').filter(has=page.locator('summary', has_text='사람의 로그인')).locator('summary').click()
            assert page.get_by_text('사람의 비밀번호·세션을 전달하지 않습니다.', exact=False).is_visible()
            page.evaluate('scrollTo(0,0)')
            page.screenshot(path=str(output / 'human-role-rejected.png'), full_page=True)
            result['checks'].append('Same principal with human operator role is rejected before ARM traffic; independent human/service authority is explained without sharing credentials')
            check('generic-404', 'ARM_API_UNCONFIRMED', 2 + READINESS_PROBES)
            assert page.get_by_text('서비스 신원 확인됨 · API 준비 필요', exact=True).is_visible()
            page.screenshot(path=str(output / 'generic-404-rejected.png'), full_page=True)
            check('mismatch', 'PRINCIPAL_MISMATCH', 1)
            assert page.get_by_text('연결 확인 필요', exact=True).is_visible()
            page.screenshot(path=str(output / 'identity-mismatch-rejected.png'), full_page=True)
            check('compatible', 'READ_API_VERIFIED', 2 + READINESS_PROBES)
            result['checks'].append('Authenticated service session plus coded ARM request-key 404 verifies read API; generic route 404 and principal mismatch fail closed; mismatch never calls ARM')
            check('not-installed', 'ARM_NOT_INSTALLED', 2 + READINESS_PROBES)
            assert page.get_by_text('서비스 신원 확인됨 · API 준비 필요', exact=True).is_visible()
            assert page.get_by_text('설치 필요', exact=True).is_visible()
            assert page.get_by_text('담당 · ROSY 설치 담당자', exact=True).is_visible()
            page.screenshot(path=str(output / 'identity-confirmed-api-unavailable.png'), full_page=True)
            Audit.objects.create(actor=users['admin'], action='fleet.check', object_type='FleetConnection', object_id='1',
                detail=dict(status='FAILED', session='NOT_CHECKED', arm='NOT_CHECKED', reason='FLEET_DNS_UNRESOLVED',
                            fingerprint=bridge.fingerprint(bridge.connection_settings())))
            before = len(state['requests'])
            page.set_viewport_size(dict(width=390, height=844))
            page.reload()
            page.wait_for_load_state('networkidle')
            assert page.get_by_text(CONNECTION_REASONS['FLEET_DNS_UNRESOLVED'], exact=True).is_visible()
            assert page.get_by_role('heading', name='다음 조치', exact=True).is_visible()
            result['mobile_next_action_y'] = page.get_by_role('heading', name='다음 조치').bounding_box()['y']
            page.screenshot(path=str(output / 'saved-dns-failure-mobile.png'), full_page=True)
            assert result['mobile_next_action_y'] < 844, result['mobile_next_action_y']
            assert page.get_by_text('담당 · Harbor 설치 담당자', exact=True).is_visible()
            assert len(state['requests']) == before
            assert not page.evaluate('document.documentElement.scrollWidth > innerWidth')
            safe_page()
            result['checks'].append('Real synthetic HTTP503 preserves verified identity and identifies ROSY installation action; saved DNS fixture gives server-side recovery on mobile without network traffic')
            check('compatible', 'READ_API_VERIFIED', 2 + READINESS_PROBES)
            for width, height in ((1440, 1000), (1280, 800), (768, 1024), (390, 844)):
                page.set_viewport_size(dict(width=width, height=height))
                page.goto(origin + '/settings/integration/')
                page.wait_for_load_state('networkidle')
                assert not page.evaluate('document.documentElement.scrollWidth > innerWidth'), width
                safe_page()
                page.screenshot(path=str(output / f'{width}-integration.png'), full_page=True)
                result['viewports'].append(dict(width=width, height=height, overflow=False))
            before = len(state['requests'])
            restricted = browser.new_page()
            login(restricted, 'operator')
            assert restricted.goto(origin + '/settings/integration/').status == 403
            csrf = next(cookie['value'] for cookie in restricted.context.cookies() if cookie['name'] == 'csrftoken')
            for action in ('save', 'check'):
                response = restricted.request.post(origin + '/settings/integration/',
                    form=dict(action=action, expected_version='1', origin=fleet_origin, principal=principal),
                    headers={'X-CSRFToken': csrf, 'Referer': origin + '/settings/integration/'})
                assert response.status == 403
            assert len(state['requests']) == before
            assert FleetConnection.objects.get(pk=1).version == 1
            assert not Command.objects.exists()
            assert all(request['method'] == 'GET' and request['authorized'] for request in state['requests'])
            assert len(state['requests']) == 12 + 5 * READINESS_PROBES  # five checks reached a matched session
            assert token not in json.dumps(list(Audit.objects.values_list('detail', flat=True)))
            assert not errors, errors
            result['checks'].append('Operator GET/save/check denied; four widths have no overflow; token absent from HTML/audit; all twelve Fleet requests are authenticated GETs; no Harbor Commands or Fleet jobs created')
            result.update(status='PASSED', fleet_requests=state['requests'], browser_errors=errors)
            browser.close()
    except Exception:
        result['traceback'] = traceback.format_exc()
    finally:
        if child is not None:
            child.terminate()
            child.wait(timeout=10)
        fleet.shutdown()
        fleet.server_close()
        log.close()
        (output / 'report.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['status'] == 'PASSED' else 1


if __name__ == '__main__':
    raise SystemExit(main())
