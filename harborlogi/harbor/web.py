"""Local read-only Harbor workbench. Never owns execution or ledger recovery."""
import argparse
from datetime import datetime, timezone
from http import HTTPStatus
import ipaddress
from importlib.metadata import distribution
import json
import os
import re
from pathlib import Path
import sqlite3
from urllib.parse import parse_qs, urlsplit
from wsgiref.simple_server import make_server, WSGIRequestHandler

from harbor.process import ArmTransferBinding
from harbor.execution import arm_execution_view
from harbor.ledger import ReadOnlyLedger, plan_payload
from harbor.planning_view import preview
from harbor.rosy_http import RosyClient


ASSETS = Path(__file__).with_name('web_assets')
BRAND = Path(__file__).resolve().parents[1] / 'assets' / 'brand'


def _brand_file(name):
    source = BRAND / name
    if source.is_file():
        return source
    target = Path(__file__).resolve().parents[1] / 'share' / 'harborlogi' / 'brand' / name
    if target.is_file():  # pip --target relocates data files alongside the package.
        return target
    installed = distribution('harborlogi')
    for file in installed.files or ():
        if str(file).endswith('share/harborlogi/brand/' + name):
            return Path(installed.locate_file(file))
    raise FileNotFoundError('Brand asset not installed')




def _summary(row):
    payload = plan_payload(row)
    return {key: row.get(key) for key in ('request_key', 'state', 'row_version', 'step_index', 'mission_id',
                                          'attempt_id', 'goal_confirmed')} | {
        'cargo_id': payload['outbound_id'], 'revision': payload['revision'],
        'step_count': len(payload['moves']), 'execution_kind': payload.get('execution_kind', 'CELL'),
    }


def _console_url(value):
    if not value:
        return None
    parsed = urlsplit(value)
    try:
        local = parsed.hostname == 'localhost' or ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        local = False
    if (not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.scheme not in {'https', 'http'} or parsed.scheme == 'http' and not local
            or any(ord(c) <= 32 for c in value) or '\\' in value):
        raise ValueError('Expected a console HTTPS URL or loopback HTTP URL without credentials or query')
    parsed.port  # Reject malformed ports before emitting a link.
    return value


class Workbench:
    def __init__(self, ledger, client=None, *, console_url=None, demo=False):
        self.ledger, self.client, self.demo = ledger, client, demo
        self.console_url = _console_url(console_url)

    def tasks(self):
        return [_summary(row) for row in self.ledger.tasks()]

    def detail(self, key):
        row = self.ledger.read(key)
        payload = plan_payload(row)
        result = {'mode': 'demo' if self.demo else 'live', 'business': _summary(row),
                  'read_at': datetime.now(timezone.utc).isoformat(), 'console_url': self.console_url,
                  'plan': [{'cargo_id': move['cargo_id'], 'source': move['source'],
                            'destination': move['destination'], 'arms': payload['move_arms'][index]}
                           for index, move in enumerate(payload['moves'])],
                  'fleet': {'status': 'NOT_SUBMITTED'}, 'receipt': {'status': 'NOT_VERIFIED'}}
        if self.demo:
            result['fleet'] = {'status': 'DEMO', 'reason': '합성 시안 · ROSY에 연결하지 않았습니다.'}
            return result
        if not row['mission_id']:
            if row['state'] in {'COMPLETED', 'AWAITING_FINAL_EVIDENCE'}:
                result['fleet'] = {'status': 'NO_CURRENT_JOB'}
            return result
        if payload.get('execution_kind') != 'ARM_TRANSFER':
            result['fleet'] = {'status': 'UNSUPPORTED', 'reason': 'Cell 호환 작업은 기존 도구에서 대조하세요.'}
            return result
        if self.client is None:
            result['fleet'] = {'status': 'NOT_CONFIGURED'}
            return result
        try:
            binding = ArmTransferBinding(**payload['moves'][row['step_index']])
            result['fleet'], result['receipt'] = arm_execution_view(
                self.client, binding, f"{key}:{row['step_index']}", row['mission_id'])
        except (ValueError, KeyError, TypeError, IndexError):
            result['fleet'] = {'status': 'MISMATCH'}
        current = self.ledger.read(key)
        if any(current[field] != row[field] for field in ('row_version', 'digest', 'mission_id', 'step_index', 'state')):
            result['fleet'], result['receipt'] = {'status': 'STALE'}, {'status': 'NOT_VERIFIED'}
        return result


class DemoLedger:
    """Explicit, memory-only illustrative data; never substitutes for failed live reads."""
    def tasks(self):
        rows = []
        cases = [('DEMO-1024', 'WAITING', 0, 'demo-arm-01', 'C1', 'C2'),
                 ('DEMO-1025', 'HOLD', 1, 'demo-arm-02', 'C4', 'C5'),
                 ('DEMO-1026', 'PLANNED', 0, None, 'C7', 'C8'),
                 ('DEMO-1027', 'SUBMITTING', 0, None, 'C9', 'C10'),
                 ('DEMO-1028', 'AWAITING_FINAL_EVIDENCE', 3, None, 'C11', 'C12'),
                 ('DEMO-1029', 'COMPLETED', 3, None, 'C13', 'C14')]
        for key, state, step, mission, cargo, blocker in cases:
            moves = [dict(cargo_id=blocker, source='yard-01', destination='temp-01'),
                     dict(cargo_id=cargo, source='yard-01', destination='outbound-01'),
                     dict(cargo_id=blocker, source='temp-01', destination='yard-01')]
            rows.append(dict(request_key=key, state=state, row_version=4, step_index=step, mission_id=mission,
                             attempt_id=None, goal_confirmed=0, payload=json.dumps(dict(
                                 outbound_id=cargo, revision=3, execution_kind='ARM_TRANSFER', moves=moves,
                                 move_arms=[['A', 'B'], ['B'], ['A', 'B']]))))
        return rows

    def read(self, key):
        for row in self.tasks():
            if row['request_key'] == key:
                return row
        raise KeyError(key)


class Application:
    def __init__(self, workbench):
        self.workbench = workbench

    def __call__(self, env, start_response):
        status, mime, body = 200, 'application/json; charset=utf-8', b''
        try:
            port = env.get('SERVER_PORT', '')
            if (env.get('HTTP_HOST') not in {f'localhost:{port}', f'127.0.0.1:{port}'}
                    or env.get('REMOTE_ADDR') not in {'127.0.0.1', '::1'}):
                status, data = 403, {'error': 'LOCAL_ONLY', 'message': '이 단계는 같은 PC에서만 조회할 수 있습니다.'}
            elif env['REQUEST_METHOD'] != 'GET':
                status, data = 405, {'error': 'READ_ONLY', 'message': '현재 웹 앱은 조회만 지원합니다.'}
            elif env['PATH_INFO'] == '/api/planning':
                if not self.workbench.demo:
                    status, data = 503, {'error': 'INVENTORY_NOT_CONFIGURED',
                                        'message': '실제 재고 관측과 작업 지시가 연결되지 않았습니다. 데모로 대체하지 않습니다.'}
                else:
                    query = parse_qs(env.get('QUERY_STRING', ''), keep_blank_values=True)
                    try:
                        if set(query) - {'cargo', 'scenario'} or any(len(v) != 1 for v in query.values()):
                            raise ValueError('Invalid query')
                        data = preview(query.get('cargo', ['C1'])[0], query.get('scenario', ['normal'])[0])
                    except ValueError:
                        status, data = 400, {'error': 'INVALID_PREVIEW', 'message': '반출 대상과 합성 조건을 확인하세요.'}
            elif env['PATH_INFO'] == '/api/work':
                query = parse_qs(env.get('QUERY_STRING', ''), keep_blank_values=True)
                if set(query) - {'key'} or any(len(v) != 1 for v in query.values()):
                    raise ValueError('Invalid query')
                data = (self.workbench.detail(query['key'][0]) if 'key' in query else
                        {'mode': 'demo' if self.workbench.demo else 'live', 'tasks': self.workbench.tasks()})
            else:
                path = env['PATH_INFO']
                page = (path in {'/', '/overview', '/work', '/planning', '/help'}
                        or re.fullmatch(r'/work/[^/\x00-\x1f]+(?:/(?:plan|execution))?', path))
                files = {'/': (ASSETS / 'index.html', 'text/html; charset=utf-8'),
                         '/assets/tokens.css': (ASSETS / 'tokens.css', 'text/css; charset=utf-8'),
                         '/assets/app.css': (ASSETS / 'app.css', 'text/css; charset=utf-8'),
                         '/assets/app.js': (ASSETS / 'app.js', 'text/javascript; charset=utf-8'),
                         '/assets/icon.png': (_brand_file('harbor-app-icon-20261023.png'), 'image/png'),
                         '/assets/banner.png': (_brand_file('harborlogi-github-banner-20261023.png'), 'image/png')}
                if page:
                    path = '/'
                if path not in files:
                    raise KeyError('Not found')
                path, mime = files[path]
                body = path.read_bytes()
                data = None
        except KeyError:
            status, data = 404, {'error': 'NOT_FOUND', 'message': '대상을 찾지 못했습니다. 업무 목록을 다시 여세요.'}
        except (ValueError, TypeError, IndexError, sqlite3.Error, OSError):
            status, data = 503, {'error': 'READ_UNAVAILABLE', 'message': '장부를 읽지 못했습니다. 서버의 경로와 장부 형식을 확인하세요.'}
        if data is not None:
            body = json.dumps(data, ensure_ascii=False, allow_nan=False).encode()
        headers = [('Content-Type', mime), ('Content-Length', str(len(body))), ('Cache-Control', 'no-store'),
                   ('X-Content-Type-Options', 'nosniff'), ('Referrer-Policy', 'no-referrer'),
                   ('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; "
                    "connect-src 'self'; img-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")]
        if status == 405:
            headers.append(('Allow', 'GET'))
        start_response(f'{status} {HTTPStatus(status).phrase}', headers)
        return [body]


class QuietHandler(WSGIRequestHandler):
    def log_message(self, format, *args):
        pass  # Do not retain business identifiers from query strings in access logs.


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--demo', action='store_true', help='Explicit synthetic visual preview; no Fleet connection')
    mode.add_argument('--ledger', type=Path, help='Existing absolute business ledger path; opened read-only')
    parser.add_argument('--port', type=int, default=8040)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535 or args.ledger and not args.ledger.is_absolute():
        parser.error('Use a valid port and absolute ledger path')
    try:
        client = None
        if not args.demo:
            origin, token = os.environ.get('HARBOR_FLEET_ORIGIN'), os.environ.get('HARBOR_FLEET_TOKEN')
            if origin or token:
                client = RosyClient(origin, token, timeout=3)
        view = Workbench(DemoLedger() if args.demo else ReadOnlyLedger(args.ledger), client,
                         demo=args.demo, console_url=None if args.demo else os.environ.get('HARBOR_FLEET_CONSOLE_URL'))
        view.tasks()  # Fail startup without creating/recovering an invalid ledger.
    except (ValueError, sqlite3.Error, OSError, KeyError):
        parser.error('Invalid read-only ledger or Fleet configuration; check server settings')
    with make_server('127.0.0.1', args.port, Application(view), handler_class=QuietHandler) as server:
        print(f'Harbor: http://127.0.0.1:{args.port} ({"DEMO" if args.demo else "read-only"})', flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == '__main__':
    main()
