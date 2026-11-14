"""Read-only check of the connection rules (docs/platform-contract.md 「연결 규약」) on an installed Harbor.

Run inside the running Harbor container so it uses the installed settings, trust store and credential:
    docker exec -i <harbor container> python - < tools/integration/connection_probe.py
Every request is a GET (or an introspect of a made-up token); nothing is created. Prints no address or secret.
Exit code 1 when a rule fails.
"""
import json
import os
import socket
import ssl
import sys
import time
import uuid
from urllib.error import HTTPError
from urllib.parse import urlsplit

from harbor.rosy_http import RosyClient

failed = []


def report(rule, ok, text):
    print(f'{"PASS" if ok else "FAIL"} 규칙 {rule}: {text}')
    if not ok:
        failed.append(rule)


def answer(read):
    """(status, detail.code) of a read that is expected to be refused."""
    try:
        read()
    except HTTPError as error:
        detail = json.loads(error.read(4096) or b'{}').get('detail')
        return error.code, detail.get('code') if isinstance(detail, dict) else None
    return 200, None


origin, token = os.environ['HARBOR_FLEET_ORIGIN'], os.environ['HARBOR_FLEET_TOKEN']
parts = urlsplit(origin)
report(1, parts.scheme == 'https', f'Fleet origin scheme {parts.scheme}')
with socket.create_connection((parts.hostname, parts.port or 443), timeout=5) as raw, \
        ssl.create_default_context().wrap_socket(raw, server_hostname=parts.hostname) as tls:
    days = int((ssl.cert_time_to_seconds(tls.getpeercert()['notAfter']) - time.time()) // 86400)
report(2, days > 30, f'Fleet 인증서가 이름으로 검증됨, 만료까지 {days}일')
client = RosyClient(origin, token)
session = client.session()
report(4, (session.get('principal_id'), session.get('role')) == (os.environ['HARBOR_FLEET_PRINCIPAL'], 'service'), 'session이 설정된 service 신원과 일치')
report(3, answer(RosyClient(origin, 'x' * 40).session)[0] == 401, '틀린 토큰은 401')
key = 'harbor-probe-' + uuid.uuid4().hex
report(9, answer(lambda: client.arm_request(key)) == (404, 'ARM_TRANSFER_JOB_NOT_FOUND'), '없는 키는 code가 든 404')
events = answer(lambda: client.port_events(after=key, limit=1))
report(11, events in {(400, 'PORT_EVENT_CURSOR_INVALID'), (404, None)},
       'port-events ' + ('경로 있음' if events[0] == 400 else '경로 없음: 이 R에서는 입고 사건이 자동으로 들어오지 않음' if events == (404, None) else f'미확인 {events}'))
print('INFO execution-profile:', {k: v for k, v in client.execution_profile().items() if k in ('profile', 'arm_execution')})

if os.environ.get('HARBOR_ROSY_ACCOUNT_FILE'):
    import django
    django.setup()
    from django.conf import settings
    from harbor_web.rosy_account import _call
    config = settings.ROSY_ACCOUNT
    report(12, all(urlsplit(config[name]).scheme == 'https' for name in ('issuer', 'callback')), 'Account issuer·callback이 https')
    report(13, _call('introspect', {'token': 'not-a-token'}) == (200, {'active': False}), 'Account가 모르는 토큰을 active:false로 답함')
else:
    print('INFO Rosy 계정 로그인 미설정: 로컬 로그인만 있음(규칙 12·13 해당 없음)')
sys.exit(1 if failed else 0)
