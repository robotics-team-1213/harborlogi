"""ROSY site-v1 LAN discovery. Advertisements supply routes, never trust."""
import http.client
import ipaddress
import json
from itertools import count
import re
import socket
import ssl
import threading
import time
from urllib.parse import urlsplit
from urllib.error import HTTPError, URLError
from urllib.request import HTTPSHandler, ProxyHandler, build_opener


SERVICE_TYPE = '_rosy-fleet._tcp.local.'
HOSTNAME = re.compile(r'^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.local$')
TXT = {'product': 'rosy', 'role': 'fleet', 'proto': 'site-v1', 'tls': 'required'}


class DiscoveryError(ValueError):
    """A safe diagnostic code for a failed discovery or health check."""


def site_ssl_context(ca_file):
    try:
        return ssl.create_default_context(cafile=str(ca_file))
    except FileNotFoundError as error:
        raise DiscoveryError('FLEET_SITE_CA_REQUIRED') from error
    except ssl.SSLError as error:
        raise DiscoveryError('FLEET_SITE_CA_INVALID') from error
    except OSError as error:
        raise DiscoveryError('FLEET_SITE_CA_UNREADABLE') from error


def candidates_from_info(info):
    if info is None:
        return []
    hostname = (info.server or '').lower().rstrip('.')
    if not HOSTNAME.fullmatch(hostname) or type(info.port) is not int or not 1 <= info.port <= 65535:
        return []
    # ServiceInfo.properties collapses duplicate TXT keys; inspect the wire bytes first.
    raw, values = info.text, {}
    if not isinstance(raw, bytes) or len(raw) > 4096:
        return []
    try:
        while raw:
            size, raw = raw[0], raw[1:]
            if not size or len(raw) < size:
                return []
            key, sep, value = raw[:size].partition(b'=')
            raw = raw[size:]
            key = key.decode('ascii').lower()
            if not key or key in values:
                return []
            values[key] = value if sep else b''
        if any(values.get(key) != value.encode('ascii') for key, value in TXT.items()):
            return []
    except UnicodeError:
        return []
    candidates = []
    for address in info.parsed_addresses():
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            continue
        if (ip.version == 4 and ip.is_private and not any((ip.is_loopback, ip.is_link_local,
                ip.is_multicast, ip.is_unspecified, ip.is_reserved))):
            candidates.append(dict(hostname=hostname, address=str(ip), port=info.port))
    return candidates


def browse_fleets(timeout=3):
    try:
        from zeroconf import IPVersion, ServiceBrowser, ServiceListener, Zeroconf
    except ImportError as error:
        raise DiscoveryError('FLEET_DISCOVERY_UNAVAILABLE') from error
    names, lock, overflow = {}, threading.Lock(), threading.Event()
    versions = count(1)
    class Listener(ServiceListener):
        def add_service(self, zc, type_, name):
            with lock:
                if name not in names and len(names) >= 64:
                    overflow.set()
                else:
                    names[name] = next(versions)
        update_service = add_service
        def remove_service(self, zc, type_, name):
            with lock:
                names.pop(name, None)
    deadline = time.monotonic() + timeout
    try:
        with Zeroconf(ip_version=IPVersion.V4Only) as zc:
            browser = ServiceBrowser(zc, SERVICE_TYPE, Listener())
            resolved, generations = {}, {}
            try:
                while time.monotonic() < deadline:
                    if overflow.is_set():
                        raise DiscoveryError('FLEET_DISCOVERY_CONFLICT')
                    with lock:
                        found = dict(names)
                    for name, generation in found.items():
                        if generations.get(name) == generation:
                            continue
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            break
                        info = zc.get_service_info(SERVICE_TYPE, name, timeout=max(1, min(200, int(remaining * 1000))))
                        if info is not None:
                            resolved[name] = candidates_from_info(info)
                            generations[name] = generation
                    threading.Event().wait(max(0, min(.05, deadline - time.monotonic())))
            finally:
                browser.cancel()
            with lock:
                if overflow.is_set():
                    raise DiscoveryError('FLEET_DISCOVERY_CONFLICT')
                if any(generations.get(name) != generation for name, generation in names.items()):
                    raise DiscoveryError('FLEET_DISCOVERY_TIMEOUT')
                return [row for name in names for row in resolved[name]]
    except OSError as error:
        raise DiscoveryError('FLEET_DISCOVERY_UNAVAILABLE') from error


def select_endpoint(candidates, origin):
    try:
        parsed = urlsplit(origin)
        port = parsed.port if parsed.port is not None else 443
    except ValueError as error:
        raise DiscoveryError('FLEET_DISCOVERY_CONFIGURATION') from error
    expected = parsed.hostname
    if (parsed.scheme != 'https' or not expected or not HOSTNAME.fullmatch(expected)
            or parsed.username or parsed.password or parsed.path not in {'', '/'} or parsed.query or parsed.fragment
            or not 1 <= port <= 65535):
        raise DiscoveryError('FLEET_DISCOVERY_CONFIGURATION')
    matches = {(row['address'], row['port']) for row in candidates if row['hostname'] == expected}
    if not matches:
        raise DiscoveryError('FLEET_NOT_DISCOVERED')
    if len(matches) != 1:
        raise DiscoveryError('FLEET_DISCOVERY_CONFLICT')
    address, advertised_port = matches.pop()
    if advertised_port != port:
        raise DiscoveryError('FLEET_DISCOVERY_PORT_MISMATCH')
    return dict(hostname=expected, address=address, port=port)


class _SiteHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, *, address, **kwargs):
        super().__init__(host, **kwargs)
        self.address = address

    def connect(self):
        raw = socket.create_connection((self.address, self.port), self.timeout, self.source_address)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


class _SiteHTTPSHandler(HTTPSHandler):
    def __init__(self, candidate, context):
        super().__init__(context=context)
        self.candidate = candidate

    def https_open(self, request):
        def connect(host, **kwargs):
            authority = urlsplit('https://' + host)
            if (authority.hostname != self.candidate['hostname'] or
                    (authority.port or 443) != self.candidate['port'] or authority.username or authority.password):
                raise DiscoveryError('FLEET_DISCOVERY_CONFIGURATION')
            return _SiteHTTPSConnection(f"{self.candidate['hostname']}:{self.candidate['port']}",
                address=self.candidate['address'], context=self._context, **kwargs)
        return self.do_open(connect, request)


def endpoint_opener(candidate, origin, ca_file, timeout):
    """Validate the installed name/CA and health before any bearer is sent."""
    from harbor.rosy_http import _NoRedirect
    selected = select_endpoint([candidate], origin)
    context = site_ssl_context(ca_file)
    opener = build_opener(ProxyHandler({}), _NoRedirect(), _SiteHTTPSHandler(selected, context))
    try:
        with opener.open(origin.rstrip('/') + '/healthz', timeout=timeout) as response:
            raw = response.read(1025)
            try:
                value = json.loads(raw) if len(raw) <= 1024 and response.status == 200 else None
            except ValueError:
                value = None
            if (not isinstance(value, dict) or value.get('status') != 'ok'
                    or value.get('role', 'fleet') != 'fleet'):
                raise DiscoveryError('FLEET_HEALTH_UNCONFIRMED')
    except HTTPError as error:
        error.close()
        raise DiscoveryError('FLEET_HEALTH_UNCONFIRMED') from error
    except http.client.HTTPException as error:
        raise DiscoveryError('FLEET_HEALTH_UNCONFIRMED') from error
    return opener


def discovery_opener(origin, ca_file, timeout):
    site_ssl_context(ca_file)
    candidate = select_endpoint(browse_fleets(), origin)
    return endpoint_opener(candidate, origin, ca_file, timeout)


def public_api_status(opener, origin, timeout):
    """A public route advertisement is a hint, never authenticated readiness."""
    try:
        with opener.open(origin.rstrip('/') + '/openapi.json', timeout=timeout) as response:
            raw = response.read(3 * 1024 * 1024 + 1)
            schema = json.loads(raw) if response.status == 200 and len(raw) <= 3 * 1024 * 1024 else None
        if (not isinstance(schema, dict) or not isinstance(schema.get('openapi'), str)
                or not schema['openapi'].startswith('3.') or not isinstance(schema.get('paths'), dict)):
            return 'PUBLIC_API_UNCONFIRMED'
        route = schema['paths'].get('/api/fleet/arm-transfers/jobs', {})
        if not isinstance(route, dict) or any(
                method in route and not isinstance(route[method], dict) for method in ('get', 'post')):
            return 'PUBLIC_API_UNCONFIRMED'
        return ('ARM_API_ADVERTISED' if all(
            isinstance(route.get(method), dict) for method in ('get', 'post')) else 'ARM_API_NOT_ADVERTISED')
    except HTTPError as error:
        error.close()
        return 'PUBLIC_API_UNCONFIRMED'
    except (URLError, OSError, ValueError, TypeError, http.client.HTTPException):
        return 'PUBLIC_API_UNCONFIRMED'
