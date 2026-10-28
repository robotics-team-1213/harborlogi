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

    def test_proposal_waits_for_a_different_operator_then_arrives(self):
        """D-908 / v1.329: Harbor proposes once, never admits; another named operator admits."""
        config = json.loads(Path(os.environ['HARBOR_INTEGRATION_CONFIG']).read_text(encoding='utf-8'))
        proposer = RosyClient(config['origin'], config['operator'])
        approver = RosyClient(config['origin'], config['approver'])
        intent = TripIntent('harbor-trip:' + uuid4().hex, tuple(config['candidates']), config['steps'][0]['to'])
        class LostResponse:
            def trip_propose(self, candidates, steps, *, request_key):
                proposer.trip_propose(candidates, steps, request_key=request_key)
                raise TimeoutError('synthetic response loss after server commit')
        with tempfile.TemporaryDirectory() as folder:
            ledger = TransportLedger(Path(folder) / 'business.sqlite3')
            ledger.prepare_proposal(intent)
            with self.assertRaises(TimeoutError):
                ledger.propose(intent.request_key, LostResponse())
            row = ledger.reconcile_proposal(intent.request_key, proposer)
            self.assertEqual(row['state'], 'PROPOSED')
            proposal = proposer.trip_proposal_request(intent.request_key)['proposal']
            path = '/api/fleet/trip-proposals/' + proposal['proposal_id'] + '/admit'
            # The operator console reads the open generation the same way before admitting.
            control = approver._call('GET', '/api/fleet/dispatch-control', None)
            body = {'expected_proposal_digest': proposal['proposal_digest'],
                    'expected_generation': control['generation']}
            with self.assertRaises(HTTPError) as own:
                proposer._call('POST', path, body)
            self.assertIn(own.exception.code, (403, 409))
            self.assertEqual(ledger.reconcile_proposal(intent.request_key, proposer)['state'], 'PROPOSED')
            approver._call('POST', path, body)
            deadline = time.monotonic() + 30
            row = ledger.reconcile_proposal(intent.request_key, proposer)
            while row['state'] != 'GOAL_CONFIRMED':
                self.assertIn(row['state'], ('ADMITTED', 'PROPOSED'))
                if time.monotonic() > deadline:
                    self.fail('The admitted trip did not arrive within 30 seconds')
                time.sleep(.1)
                row = ledger.reconcile_proposal(intent.request_key, proposer)
            self.assertEqual(approver.trip_proposal(proposal['proposal_id'])['proposal']['approved_by'] is not None, True)
