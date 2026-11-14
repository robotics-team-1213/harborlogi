"""Explicit loopback simulation contract test, launched separately from unit discovery."""
import json
import ipaddress
import os
from pathlib import Path
import tempfile
import time
import unittest
from urllib.error import HTTPError
from urllib.parse import urlsplit
from uuid import uuid4

from harbor.rosy_http import RosyClient
from harbor.transport import TripIntent, TransportLedger


class TransportHttpTests(unittest.TestCase):
    def test_lost_response_recovery_and_arrival_without_process_documents(self):
        config = json.loads(Path(os.environ['HARBOR_INTEGRATION_CONFIG']).read_text(encoding='utf-8'))
        self.assertEqual(config['schema'], 'harbor.transport-http-test/1')
        self.assertIs(config['simulation'], True)
        origin = urlsplit(config['origin'])
        self.assertEqual(origin.scheme, 'http')
        self.assertTrue(ipaddress.ip_address(origin.hostname).is_loopback)
        client = RosyClient(config['origin'], config['operator'])
        intent = TripIntent('harbor/이송?key=' + uuid4().hex, tuple(config['candidates']), config['steps'][0]['to'])
        class LostResponse:
            def trip(self, candidates, steps, *, request_key):
                client.trip(candidates, steps, request_key=request_key)
                raise TimeoutError('synthetic response loss after server commit')
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'business.sqlite3'
            ledger = TransportLedger(path)
            ledger.prepare(intent)
            with self.assertRaises(TimeoutError):
                ledger.submit(intent.request_key, LostResponse(), operator_confirmed=True)
            self.assertEqual(ledger.read(intent.request_key)['state'], 'UNCERTAIN')
            restarted = TransportLedger(path)
            with self.assertRaises(ValueError):
                restarted.submit(intent.request_key, client, operator_confirmed=True)
            job = restarted.reconcile(intent.request_key, client)
            by_id = client.trip_job(job['mission_id'])['job']
            for field in ('mission_id', 'request_key', 'request_digest', 'robot_id'):
                self.assertEqual(by_id[field], job[field])
            self.assertEqual(by_id['steps'][0]['step'], job['steps'][0]['step'])
            other = RosyClient(config['origin'], config['approver'])
            with self.assertRaises(HTTPError) as denied:
                other.trip_request(intent.request_key)
            self.assertEqual(denied.exception.code, 404)
            deadline = time.monotonic() + 30
            while job['status'] != 'GOAL_CONFIRMED':
                self.assertNotEqual(job['status'], 'HOLD', job.get('reason'))
                if time.monotonic() > deadline:
                    self.fail('The simulation trip did not arrive within 30 seconds')
                time.sleep(.1)
                job = restarted.reconcile(intent.request_key, client)
            self.assertIsNotNone(job['steps'][0]['attempt_id'])
            self.assertEqual(restarted.read(intent.request_key)['state'], 'ACKNOWLEDGED')
            self.assertFalse({'recipe_digest', 'cell_digest', 'process_artifact_digest'} & job.keys())

    def test_connection_rules_against_the_real_fleet(self):
        """docs/platform-contract.md 연결 규약 3·4·8·9·10 on a real Fleet: identity, coded errors, port-events probe."""
        config = json.loads(Path(os.environ['HARBOR_INTEGRATION_CONFIG']).read_text(encoding='utf-8'))
        client = RosyClient(config['origin'], config['operator'])
        session = client.session()
        self.assertTrue({'principal_id', 'role'} <= session.keys())
        with self.assertRaises(HTTPError) as refused:
            RosyClient(config['origin'], 'not-a-token').session()
        self.assertEqual(refused.exception.code, 401)
        with self.assertRaises(HTTPError) as unknown:
            client.trip_request('harbor-probe-' + uuid4().hex)
        self.assertEqual(unknown.exception.code, 404)
        with self.assertRaises(HTTPError) as probe:
            client.port_events(after='harbor-probe-' + uuid4().hex, limit=1)
        status, body = probe.exception.code, json.loads(probe.exception.read())
        if status == 404:  # a platform older than ROSY D-996 has no such route
            self.assertNotIsInstance(body.get('detail'), dict)
            return
        self.assertEqual((status, body), (400, {'detail': {'code': 'PORT_EVENT_CURSOR_INVALID'}}))
        page = client.port_events(limit=1)
        self.assertEqual(set(page), {'events', 'cursor', 'complete'})
