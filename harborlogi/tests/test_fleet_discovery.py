import unittest
import io
import importlib.util
import json
import os
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
from types import SimpleNamespace
from unittest.mock import patch

from harbor import fleet_discovery as discovery

ZEROCONF_AVAILABLE = importlib.util.find_spec('zeroconf') is not None


@contextmanager
def tls_site():
    openssl = shutil.which('openssl') or ('C:/Program Files/Git/usr/bin/openssl.exe'
        if Path('C:/Program Files/Git/usr/bin/openssl.exe').is_file() else None)
    if not openssl:
        raise unittest.SkipTest('OpenSSL required for real TLS verification')
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self):
            calls.append((self.path, self.headers.get('Host'), self.headers.get('Authorization')))
            if self.path == getattr(self.server, 'broken_path', None):
                self.send_response(200)
                self.send_header('Transfer-Encoding', 'chunked')
                self.end_headers()
                self.wfile.write(b'10\r\n{')
                self.close_connection = True
                return
            if self.path == '/healthz':
                payload, status = getattr(self.server, 'health', {'status': 'ok', 'role': 'fleet'}), getattr(self.server, 'health_status', 200)
            elif self.path == '/openapi.json':
                payload, status = self.server.schema, getattr(self.server, 'schema_status', 200)
            else:
                payload, status = {'principal_id': 'harbor-service', 'role': 'service'}, 200
            raw = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        do_POST = do_GET
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        subprocess.run([openssl, 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
            '-keyout', str(root/'key.pem'), '-out', str(root/'ca.pem'), '-days', '1',
            '-subj', '/CN=site-pc.local', '-addext', 'subjectAltName=DNS:site-pc.local',
            '-addext', 'basicConstraints=critical,CA:TRUE'], check=True, capture_output=True,
            env=dict(os.environ, MSYS_NO_PATHCONV='1'))
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(root/'ca.pem', root/'key.pem')
        names = []
        context.set_servername_callback(lambda sock, name, ctx: names.append(name))
        server.socket = context.wrap_socket(server.socket, server_side=True)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            yield server, root/'ca.pem', calls, names
        finally:
            server.shutdown()
            server.server_close()
            worker.join()


def advertisement(host='site-pc.local.', address='192.168.50.2', port=8443, records=None):
    records = records if records is not None else [
        b'product=rosy', b'role=fleet', b'proto=site-v1', b'tls=required']
    return SimpleNamespace(server=host, port=port,
        text=b''.join(bytes([len(row)]) + row for row in records),
        parsed_addresses=lambda **kwargs: [address])


class DiscoveryTests(unittest.TestCase):
    def test_ca_file_failures_are_classified_before_any_network(self):
        from harbor.rosy_http import RosyClient
        with tempfile.TemporaryDirectory() as folder:
            bad = Path(folder) / 'ca.crt'
            for content, expected in [(None, 'FLEET_SITE_CA_REQUIRED'), ('not a certificate', 'FLEET_SITE_CA_INVALID')]:
                if content is not None:
                    bad.write_text(content)
                with self.subTest(content=content), patch.object(discovery, 'browse_fleets', side_effect=AssertionError('CA failure must precede browse')):
                    client = RosyClient('https://site-pc.local:8443', 'private-service-token', ca_file=bad)
                    with self.assertRaisesRegex(discovery.DiscoveryError, expected):
                        client.session()

    def test_invalid_origin_ports_or_whitespace_are_rejected_by_shared_client(self):
        from harbor.rosy_http import RosyClient
        for origin in ['https://site-pc.local:0', 'https://site-pc.local:no-port',
                       'https://site-pc.local:65536', 'https://site-pc.local\n', 'https://bad host.local']:
            with self.subTest(origin=origin), self.assertRaises(ValueError):
                RosyClient(origin, 'private-service-token')

    def test_public_schema_is_a_credential_free_hint_after_verified_health(self):
        with tls_site() as (server, ca, calls, names):
            origin = f'https://site-pc.local:{server.server_port}'
            candidate = dict(hostname='site-pc.local', address='192.168.50.2', port=server.server_port)
            dial = socket.create_connection
            with patch('socket.create_connection', side_effect=lambda address, *a, **kw: dial(('127.0.0.1', server.server_port), *a, **kw)):
                opener = discovery.endpoint_opener(candidate, origin, ca, 5)
                server.schema = {'openapi': '3.1.0', 'paths': {'/api/fleet/arm-transfers/jobs': {'get': {}, 'post': {}}}}
                self.assertEqual(discovery.public_api_status(opener, origin, 5), 'ARM_API_ADVERTISED')
                server.schema['paths'] = {}
                self.assertEqual(discovery.public_api_status(opener, origin, 5), 'ARM_API_NOT_ADVERTISED')
                server.schema_status = 403
                self.assertEqual(discovery.public_api_status(opener, origin, 5), 'PUBLIC_API_UNCONFIRMED')
                server.broken_path = '/openapi.json'
                self.assertEqual(discovery.public_api_status(opener, origin, 5), 'PUBLIC_API_UNCONFIRMED')
            self.assertEqual([row[0] for row in calls], ['/healthz'] + ['/openapi.json'] * 4)
            self.assertTrue(all(row[2] is None for row in calls))

    def test_incomplete_http_response_is_a_safe_shared_client_validation_error(self):
        from harbor.rosy_http import RosyClient
        with tls_site() as (server, ca, calls, names):
            server.broken_path = '/api/fleet/session'
            dial = socket.create_connection
            candidate = dict(hostname='site-pc.local', address='192.168.50.2', port=server.server_port)
            with patch.object(discovery, 'browse_fleets', return_value=[candidate]), \
                 patch('socket.create_connection', side_effect=lambda address, *a, **kw: dial(('127.0.0.1', server.server_port), *a, **kw)):
                client = RosyClient(f'https://site-pc.local:{server.server_port}', 'private-service-token', ca_file=ca)
                with self.assertRaisesRegex(ValueError, 'Invalid Fleet HTTP response'):
                    client.session()

    def test_malformed_or_oversized_schema_does_not_claim_an_api_is_missing(self):
        for body in [b'not json', b'{"paths":{}}', b'{"openapi":"3.1.0","paths":[]}',
                     b'{"openapi":"3.1.0","paths":{"/api/fleet/arm-transfers/jobs":null}}',
                     b'x' * (3 * 1024 * 1024 + 1)]:
            with self.subTest(size=len(body)):
                response = io.BytesIO(body)
                response.status = 200
                opener = unittest.mock.Mock()
                opener.open.return_value = response
                self.assertEqual(discovery.public_api_status(opener, 'https://site-pc.local:8443', 5), 'PUBLIC_API_UNCONFIRMED')

    def test_unhealthy_http_status_is_not_reported_as_bearer_failure(self):
        from harbor.rosy_http import RosyClient
        with tls_site() as (server, ca, calls, names):
            server.health_status = 401
            dial = socket.create_connection
            candidate = dict(hostname='site-pc.local', address='192.168.50.2', port=server.server_port)
            with patch.object(discovery, 'browse_fleets', return_value=[candidate]), \
                 patch('socket.create_connection', side_effect=lambda address, *a, **kw: dial(('127.0.0.1', server.server_port), *a, **kw)):
                client = RosyClient(f'https://site-pc.local:{server.server_port}', 'private-service-token', ca_file=ca)
                with self.assertRaisesRegex(discovery.DiscoveryError, 'FLEET_HEALTH_UNCONFIRMED'):
                    client.session()
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][2], None)

    @unittest.skipUnless(ZEROCONF_AVAILABLE, 'Install .[web] for DNS-SD transport tests')
    def test_incomplete_or_overfull_browse_never_returns_a_partial_match(self):
        class Zc:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def get_service_info(self, type_, name, timeout):
                return advertisement() if name == 'resolved' else None
        class Browser:
            def __init__(self, zc, type_, listener, **kwargs):
                self.listener = listener
                listener.add_service(zc, type_, 'resolved')
                listener.add_service(zc, type_, 'unresolved')
            def cancel(self): pass
        with patch('zeroconf.Zeroconf', return_value=Zc()), patch('zeroconf.ServiceBrowser', Browser):
            with self.assertRaises(discovery.DiscoveryError):
                discovery.browse_fleets(timeout=.04)
        class OverfullBrowser(Browser):
            def __init__(self, zc, type_, listener, **kwargs):
                for i in range(65):
                    listener.add_service(zc, type_, str(i))
        with patch('zeroconf.Zeroconf', return_value=Zc()), patch('zeroconf.ServiceBrowser', OverfullBrowser):
            with self.assertRaises(discovery.DiscoveryError):
                discovery.browse_fleets(timeout=.04)

    @unittest.skipUnless(ZEROCONF_AVAILABLE, 'Install .[web] for DNS-SD transport tests')
    def test_real_dns_sd_advertisement_is_found_over_loopback_only(self):
        from zeroconf import IPVersion, ServiceInfo, Zeroconf
        from uuid import uuid4
        service = ServiceInfo(discovery.SERVICE_TYPE, f'harbor-test-{uuid4().hex}.{discovery.SERVICE_TYPE}',
            addresses=[socket.inet_aton('192.168.50.2')], port=8443, server='site-pc.local.',
            properties={b'product': b'rosy', b'role': b'fleet', b'proto': b'site-v1', b'tls': b'required'})
        with Zeroconf(interfaces=['127.0.0.1'], ip_version=IPVersion.V4Only) as advertiser, \
             Zeroconf(interfaces=['127.0.0.1'], ip_version=IPVersion.V4Only) as observer:
            advertiser.register_service(service)
            try:
                with patch('zeroconf.Zeroconf', return_value=observer):
                    rows = discovery.browse_fleets()
                self.assertEqual(discovery.select_endpoint(rows, 'https://site-pc.local:8443'),
                    dict(hostname='site-pc.local', address='192.168.50.2', port=8443))
            finally:
                advertiser.unregister_service(service)

    @unittest.skipUnless(ZEROCONF_AVAILABLE, 'Install .[web] for DNS-SD transport tests')
    def test_removed_and_readded_service_is_resolved_again(self):
        current = [advertisement()]
        class Zc:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def get_service_info(self, *args, **kwargs): return current[0]
        class Browser:
            def __init__(self, zc, type_, listener):
                listener.add_service(zc, type_, 'fleet')
                def changed():
                    listener.remove_service(zc, type_, 'fleet')
                    current[0] = advertisement(address='192.168.50.3')
                    listener.add_service(zc, type_, 'fleet')
                self.timer = threading.Timer(.02, changed)
                self.timer.start()
            def cancel(self): self.timer.join()
        with patch('zeroconf.Zeroconf', return_value=Zc()), patch('zeroconf.ServiceBrowser', Browser):
            rows = discovery.browse_fleets(timeout=.09)
        self.assertEqual(rows, [dict(hostname='site-pc.local', address='192.168.50.3', port=8443)])

    def test_real_tls_uses_discovered_ip_but_pinned_name_and_no_credentials_in_health(self):
        from harbor.rosy_http import RosyClient
        with tls_site() as (server, ca, calls, names):
            port = server.server_port
            old = dict(hostname='site-pc.local', address='192.168.50.2', port=port)
            new = dict(old, address='192.168.50.3')
            dial = socket.create_connection
            destinations = []
            def route(address, *args, **kwargs):
                destinations.append(address)
                return dial(('127.0.0.1', port), *args, **kwargs)
            with patch.object(discovery, 'browse_fleets', side_effect=[[old], [new]]), \
                 patch('socket.create_connection', side_effect=route):
                client = RosyClient(f'https://SITE-PC.local:{port}', 'private-service-token', ca_file=ca)
                self.assertEqual(client.session()['principal_id'], 'harbor-service')
                self.assertEqual(client.session()['role'], 'service')
            self.assertEqual(destinations, [('192.168.50.2', port)] * 2 + [('192.168.50.3', port)] * 2)
            self.assertEqual(names, ['site-pc.local'] * 4)
            self.assertEqual(calls, [('/healthz', f'SITE-PC.local:{port}', None),
                ('/api/fleet/session', f'SITE-PC.local:{port}', 'Bearer private-service-token')] * 2)

    def test_real_tls_wrong_hostname_stops_before_health_or_bearer(self):
        from harbor.rosy_http import RosyClient
        from urllib.error import URLError
        with tls_site() as (server, ca, calls, names):
            dial = socket.create_connection
            port = server.server_port
            candidate = dict(hostname='other.local', address='192.168.50.2', port=port)
            with patch.object(discovery, 'browse_fleets', return_value=[candidate]), \
                 patch('socket.create_connection', side_effect=lambda addr, *a, **kw: dial(('127.0.0.1', port), *a, **kw)):
                client = RosyClient(f'https://other.local:{port}', 'private-service-token', ca_file=ca)
                with self.assertRaises((ssl.SSLCertVerificationError, URLError)):
                    client.session()
            self.assertEqual(calls, [])

    def test_only_fleet_contract_with_unique_txt_and_lan_ipv4_is_accepted(self):
        good = advertisement()
        self.assertEqual(discovery.candidates_from_info(good), [
            {'hostname': 'site-pc.local', 'address': '192.168.50.2', 'port': 8443}])
        for bad in [advertisement(records=[b'product=rosy', b'role=fleet', b'proto=site-v1',
                                           b'tls=required', b'ROLE=robot']),
                    advertisement(records=[b'product=rosy', b'role=fleet']),
                    advertisement(host='outside.example.'), advertisement(address='127.0.0.1'),
                    advertisement(address='169.254.1.2'), advertisement(port=0)]:
            with self.subTest(bad=bad):
                self.assertEqual(discovery.candidates_from_info(bad), [])
        good.text = b'\x10short'
        self.assertEqual(discovery.candidates_from_info(good), [])

    def test_only_installed_host_and_port_are_selected_and_conflicts_stop(self):
        candidate = discovery.candidates_from_info(advertisement())[0]
        self.assertEqual(discovery.select_endpoint([candidate, candidate], 'https://site-pc.local:8443'), candidate)
        for rows in [[], [dict(candidate, hostname='other.local')],
                     [dict(candidate, port=443)], [candidate, dict(candidate, address='192.168.50.3')],
                     [candidate, dict(candidate, port=9443)]]:
            with self.subTest(rows=rows), self.assertRaises(discovery.DiscoveryError):
                discovery.select_endpoint(rows, 'https://site-pc.local:8443')

    def test_client_rediscovers_each_request_without_changing_logical_origin(self):
        from harbor.rosy_http import RosyClient
        seen = []
        class Reply:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, size): return b'{"principal_id":"harbor-service","role":"service"}'
        class Opener:
            def open(self, request, timeout):
                seen.append((request.full_url, request.get_header('Authorization')))
                return Reply()
        with patch('harbor.fleet_discovery.discovery_opener', return_value=Opener()) as resolve:
            client = RosyClient('https://site-pc.local:8443', 'private-service-token', ca_file='site-ca.pem')
            client.session()
            client.session()
        self.assertEqual(seen, [('https://site-pc.local:8443/api/fleet/session', 'Bearer private-service-token')] * 2)
        self.assertEqual(resolve.call_count, 2)
        self.assertEqual(client._origin, 'https://site-pc.local:8443')

    def test_discovery_failure_does_not_retry_a_write_or_fall_back_to_dns(self):
        from harbor.rosy_http import RosyClient
        with patch('harbor.fleet_discovery.discovery_opener', side_effect=discovery.DiscoveryError('FLEET_NOT_DISCOVERED')) as resolve:
            client = RosyClient('https://site-pc.local:8443', 'private-service-token', ca_file='site-ca.pem')
            with self.assertRaises(discovery.DiscoveryError):
                client.trip([], [], request_key='original-key')
        self.assertEqual(resolve.call_count, 1)
