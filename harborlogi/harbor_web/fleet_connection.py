"""Harbor-owned connection settings and explicit read-only public-contract checks."""
import hashlib
import json
import os
import socket
import ssl
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit

from django.core.exceptions import ValidationError
from django.db import transaction

from harbor.rosy_http import RosyClient
from harbor.fleet_discovery import DiscoveryError, HOSTNAME
from harbor_web.coordination import queue_lock, serialized
from harbor_web.models import Audit, Command, FleetConnection
from harbor_web.services import _role


def allowed_origins():
    values = os.environ.get('HARBOR_FLEET_ALLOWED_ORIGINS', '').split(',')
    origins = []
    for value in values:
        value = value.strip().rstrip('/')
        if value:
            try:
                RosyClient(value, 'validation-only')
                if any(c.isspace() or ord(c) < 32 for c in value):
                    continue
                origins.append(value)
            except ValueError:
                continue
    return list(dict.fromkeys(origins))


def connection_settings():
    origin = os.environ.get('HARBOR_FLEET_ORIGIN', '').rstrip('/')
    if origin:
        return dict(origin=origin, principal=os.environ.get('HARBOR_FLEET_PRINCIPAL', ''),
            arm_console_enabled=os.environ.get('HARBOR_FLEET_ARM_CONSOLE_ENABLED') == '1',
            version=0, source='deployment', allowed=True, ca_file=os.environ.get('HARBOR_FLEET_CA_FILE', ''))
    profile = FleetConnection.objects.first()
    return dict(origin=profile.origin if profile else '', principal=profile.principal if profile else '',
        arm_console_enabled=profile.arm_console_enabled if profile else os.environ.get('HARBOR_FLEET_ARM_CONSOLE_ENABLED') == '1',
        version=profile.version if profile else 0, source='web',
        allowed=bool(profile and profile.origin in allowed_origins()), ca_file=os.environ.get('HARBOR_FLEET_CA_FILE', ''))


def fingerprint(config):
    ca_digest = ''
    if config.get('ca_file'):
        try:
            ca_digest = hashlib.sha256(Path(config['ca_file']).read_bytes()).hexdigest()
        except OSError:
            ca_digest = 'unreadable'
    return hashlib.sha256(json.dumps(dict(config, ca_digest=ca_digest, token=os.environ.get('HARBOR_FLEET_TOKEN', '')),
                                    sort_keys=True).encode()).hexdigest()


def fleet_client(config, token, *, timeout=10):
    return RosyClient(config['origin'], token, timeout=timeout, ca_file=config.get('ca_file') or None)


def _network_reason(error):
    cause = error.reason if isinstance(error, URLError) else error
    return ('FLEET_DNS_UNRESOLVED' if isinstance(cause, socket.gaierror) else
        'FLEET_TLS_UNTRUSTED' if isinstance(cause, ssl.SSLCertVerificationError) else
        'FLEET_TLS_FAILED' if isinstance(cause, ssl.SSLError) else
        'FLEET_TIMEOUT' if isinstance(cause, TimeoutError) else 'FLEET_UNREACHABLE')


def _discovery_origins(config):
    installed = [config['origin']] if config['source'] == 'deployment' else allowed_origins()
    origins = []
    for origin in installed:
        try:
            parsed = urlsplit(origin)
            if parsed.scheme == 'https' and HOSTNAME.fullmatch(parsed.hostname or '') and parsed.port != 0:
                origins.append(origin)
        except ValueError:
            continue
    return origins


def last_discovery():
    config = connection_settings()
    current = fingerprint(dict(config, installed=_discovery_origins(config)))
    return Audit.objects.filter(action='fleet.discover', detail__fingerprint=current).order_by('-pk').first()


def discover_connections(actor, *, expected_version):
    from harbor.fleet_discovery import browse_fleets, endpoint_opener, select_endpoint, site_ssl_context, public_api_status
    _role(actor, 'admin')
    config = connection_settings()
    if config['version'] != expected_version:
        raise ValidationError('CONNECTION_CHANGED')
    if not config['ca_file']:
        raise ValidationError('FLEET_SITE_CA_REQUIRED')
    installed = _discovery_origins(config)
    if not installed:
        raise ValidationError('FLEET_DISCOVERY_CONFIGURATION')
    original = fingerprint(dict(config, installed=installed))
    try:
        site_ssl_context(config['ca_file'])
        candidates = browse_fleets()
    except DiscoveryError as error:
        raise ValidationError(str(error)) from error
    found, failures, sites = [], [], []
    for origin in installed:
        try:
            candidate = select_endpoint(candidates, origin)
            opener = endpoint_opener(candidate, origin, config['ca_file'], 5)
            found.append(origin)
            sites.append(dict(origin=origin, api=public_api_status(opener, origin, 5)))
        except DiscoveryError as error:
            failures.append(dict(origin=origin, reason=str(error)))
        except (URLError, OSError) as error:
            failures.append(dict(origin=origin, reason=_network_reason(error)))
    with queue_lock:
        current = connection_settings()
        if original != fingerprint(dict(current, installed=_discovery_origins(current))):
            raise ValidationError('CONNECTION_CHANGED')
        result = dict(origins=found, sites=sites, failures=failures)
        Audit.objects.create(actor=actor, action='fleet.discover', object_type='FleetConnection', object_id='1',
                             detail=dict(result, fingerprint=original))
    return result


@serialized
def save_connection(actor, *, origin, principal, arm_console_enabled, expected_version):
    _role(actor, 'admin')
    config = connection_settings()
    if config['source'] == 'deployment':
        raise ValidationError('DEPLOYMENT_MANAGED_CONNECTION')
    if config['version'] != expected_version:
        raise ValidationError('CONNECTION_CHANGED')
    origin = origin.rstrip('/')
    if origin not in allowed_origins():
        raise ValidationError('ORIGIN_NOT_ALLOWED')
    if not isinstance(principal, str) or not principal.strip() or len(principal) > 192 or any(ord(c) < 32 for c in principal):
        raise ValidationError('INVALID_FLEET_PRINCIPAL')
    if Command.objects.exists() and (origin, principal) != (config['origin'], config['principal']):
        raise ValidationError('EXISTING_EXECUTION_IDENTITY_LOCKED')
    from harbor_web.commands import registry_close
    with transaction.atomic():
        profile, _ = FleetConnection.objects.update_or_create(pk=1, defaults=dict(origin=origin, principal=principal,
            arm_console_enabled=arm_console_enabled, version=config['version'] + 1))
        Audit.objects.create(actor=actor, action='fleet.configure', object_type='FleetConnection', object_id='1',
            detail=dict(version=profile.version, source='web'))
    registry_close()
    return profile


@serialized
def set_human_confirmation(actor, *, enabled, expected_version):
    _role(actor, 'admin')
    from harbor_web.models import EvidenceProducerSetting
    current = EvidenceProducerSetting.objects.first()
    if (current.version if current else 0) != expected_version:
        raise ValidationError('CONNECTION_CHANGED')
    with transaction.atomic():
        row, _ = EvidenceProducerSetting.objects.update_or_create(pk=1, defaults=dict(
            human_confirmation=bool(enabled), version=expected_version + 1))
        Audit.objects.create(actor=actor, action='evidence_producer.configure', object_type='EvidenceProducerSetting',
                             object_id='1', detail=dict(human_confirmation=row.human_confirmation, version=row.version))
    return row


def last_check():
    return Audit.objects.filter(action='fleet.check', detail__fingerprint=fingerprint(connection_settings())).order_by('-pk').first()


def _error_code(error):
    status = error.code
    try:
        with error:
            raw = error.read(4097)
        detail = json.loads(raw).get('detail', {}) if len(raw) <= 4096 else {}
        return status, detail.get('code') if isinstance(detail, dict) else None
    except (OSError, ValueError, TypeError, AttributeError):
        return status, None


def _probe(read, found_code):
    """MET: Fleet answers a key that cannot exist with its own coded 404. UNMET: route absent or not installed (generic 404, 503)."""
    try:
        read()
    except HTTPError as error:
        status, code = _error_code(error)
        return ('MET' if (status, code) == (404, found_code) else
                'UNMET' if status == 503 or status == 404 and code is None else 'UNKNOWN')
    except (URLError, TimeoutError, OSError, ValueError, TypeError, AttributeError):
        return 'UNKNOWN'
    return 'UNKNOWN'  # a 200 for a key Harbor invented is not the installed-route answer


def _platform_signal(client):
    """The platform's public profile signal -> (arm_live, auto_producer). Anything but a strict answer is UNMET."""
    try:
        value = client.execution_profile()
    except HTTPError as error:
        state = 'UNMET' if _error_code(error)[0] in {404, 503} else 'UNKNOWN'
        return state, state
    except (URLError, TimeoutError, OSError, ValueError, TypeError, AttributeError):
        return 'UNKNOWN', 'UNKNOWN'
    # ROSY opens real arms only in device_trial (D-797, D-858 S4); production keeps arms closed for now.
    production = value.get('profile') in {'production', 'device_trial'}
    lists = [value.get(key) for key in ('observation_producers', 'evidence_producers')]
    return (('MET' if production and value.get('arm_execution') == 'physical' else 'UNMET'),
            'MET' if production and all(isinstance(rows, list) and rows and all(
                isinstance(row, dict) and row.get('authenticated') is True for row in rows) for rows in lists) else 'UNMET')


def probe_conditions(client, result):
    """Per-condition outcome of read-only GETs on keys that do not exist (ADR0022); nothing is created."""
    mission, key = 'harbor-probe-' + uuid.uuid4().hex, 'harbor-probe-' + uuid.uuid4().hex
    reason = result['reason']
    return dict(
        session='MET',
        arm='MET' if result['arm'] == 'READABLE' else 'UNMET' if reason == 'ARM_NOT_INSTALLED' else 'UNKNOWN',
        evidence=_probe(lambda: client.physical_evidence(mission), 'ARM_TRANSFER_JOB_NOT_FOUND'),
        recheck=_probe(lambda: client.recheck_read(mission, key), 'ARM_TRANSFER_JOB_NOT_FOUND'),
        proposal=_probe(lambda: client.trip_proposal_request(key), 'TRIP_PROPOSAL_NOT_FOUND'),
        **dict(zip(('arm_live', 'auto_producer'), _platform_signal(client))))


def check_connection(actor, *, expected_version):
    _role(actor, 'admin')
    config = connection_settings()
    if config['version'] != expected_version:
        raise ValidationError('CONNECTION_CHANGED')
    original = fingerprint(config)
    result = dict(status='FAILED', session='NOT_CHECKED', arm='NOT_CHECKED', reason='CONNECTION_INCOMPLETE')
    token = os.environ.get('HARBOR_FLEET_TOKEN', '')
    if config['origin'] and config['principal'] and token and config['allowed']:
        stage = 'session'
        try:
            client = fleet_client(config, token, timeout=5)
            session = client.session()
            if not isinstance(session.get('principal_id'), str) or not isinstance(session.get('role'), str):
                result['reason'] = 'SESSION_SCHEMA_UNCONFIRMED'
            elif session['principal_id'] != config['principal']:
                result['reason'] = 'PRINCIPAL_MISMATCH'
            elif session['role'] != 'service':
                result['reason'] = 'ROLE_NOT_SUITABLE'
            else:
                result['session'] = 'MATCHED'
                stage = 'arm'
                # A new scoped key cannot create or advance a job through this GET.
                client.arm_request('harbor-probe-' + uuid.uuid4().hex)
                result['reason'] = 'ARM_API_UNCONFIRMED'
        except HTTPError as error:
            status, code = _error_code(error)
            if stage == 'arm' and status == 404 and code == 'ARM_TRANSFER_JOB_NOT_FOUND':
                result.update(status='VERIFIED', arm='READABLE', reason='READ_API_VERIFIED')
            else:
                result['reason'] = ('AUTHENTICATION_FAILED' if status == 401 else
                    'AUTHORIZATION_FAILED' if status == 403 else
                    'ARM_NOT_INSTALLED' if stage == 'arm' and status == 503 and code == 'ARM_TRANSFER_JOB_UNAVAILABLE' else
                    'ARM_API_UNCONFIRMED' if stage == 'arm' else 'SESSION_API_UNCONFIRMED')
        except DiscoveryError as error:
            result['reason'] = str(error)
        except (URLError, TimeoutError, OSError) as error:
            result['reason'] = _network_reason(error)
        except (ValueError, TypeError, AttributeError):
            result['reason'] = 'INVALID_FLEET_RESPONSE'
        if result['session'] == 'MATCHED':
            result['conditions'] = probe_conditions(client, result)
    elif not config['allowed'] and config['origin']:
        result['reason'] = 'ORIGIN_NOT_ALLOWED'
    with queue_lock:
        if original != fingerprint(connection_settings()):
            raise ValidationError('CONNECTION_CHANGED')
        Audit.objects.create(actor=actor, action='fleet.check', object_type='FleetConnection', object_id='1',
                             detail=dict(result, fingerprint=original))
    return result
