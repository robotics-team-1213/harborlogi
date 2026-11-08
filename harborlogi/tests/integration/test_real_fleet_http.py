import json
import os
from pathlib import Path
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import ipaddress
import tempfile
import time
import unittest
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from uuid import uuid4

from harbor.process import ProcessBinding
from harbor.rosy_http import RosyClient
from harbor.workflow import ArmRetreat, BusinessLedger, MoveEvidence, PlacementEvidence, RetreatEvidence


class RealFleetHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = os.environ.get('HARBOR_INTEGRATION_CONFIG')
        if not path:
            if os.environ.get('HARBOR_INTEGRATION') == '1':
                raise RuntimeError('Explicit integration mode requires HARBOR_INTEGRATION_CONFIG')
            raise unittest.SkipTest('Real Fleet integration was not configured')
        cls.config = json.loads(Path(path).read_text(encoding='utf-8'))
        if cls.config['schema'] != 'harbor.http-test/1' or cls.config['simulation'] is not True:
            raise ValueError('An isolated simulation server is required')
        origin = urlsplit(cls.config['origin'])
        if origin.scheme != 'http' or not ipaddress.ip_address(origin.hostname).is_loopback:
            raise ValueError('Integration requires the local simulation harness')
        cls.client = RosyClient(cls.config['origin'], cls.config['operator'])
        cls.binding = ProcessBinding(**cls.config['binding'])

    def test_installed_compile_proposal_and_job_are_consumed_over_http(self):
        result = self.client.compile(self.binding)
        self.assertEqual(result['summary']['transfer_count'], 1)
        proposal = self.client.propose(self.binding, request_key='harbor-cell-' + uuid4().hex)
        mission = proposal['mission']['mission_id']
        job = self.client.job(mission)['job']
        self.assertEqual(job['mission_id'], mission)
        self.assertEqual(job['status'], 'PROPOSED')

    def test_candidate_replay_conflict_busy_and_viewer_rejection(self):
        key = 'harbor-trip-' + uuid4().hex
        candidates, steps = self.config['candidates'], self.config['steps']
        viewer = RosyClient(self.config['origin'], self.config['viewer'])
        with self.assertRaises(HTTPError) as denied:
            viewer.trip(candidates, steps, request_key=key)
        self.assertEqual(denied.exception.code, 403)
        first = self.client.trip(candidates, steps, request_key=key)['job']
        replay = self.client.trip(candidates, steps, request_key=key)['job']
        self.assertEqual(first['mission_id'], replay['mission_id'])
        for request_key, body, code in [
            (key, self.config['alternate_steps'], 'TRIP_REQUEST_CONFLICT'),
            (key + '-busy', steps, 'TRIP_CANDIDATES_BUSY'),
        ]:
            with self.subTest(code=code), self.assertRaises(HTTPError) as refused:
                self.client.trip(candidates, body, request_key=request_key)
            self.assertEqual(refused.exception.code, 409)
            self.assertEqual(json.load(refused.exception)['detail']['code'], code)

    def test_business_completion_requires_reconciled_independent_observations(self):
        with tempfile.TemporaryDirectory(prefix='harbor-business-test-') as temporary:
            path = Path(temporary) / 'business.sqlite3'
            sources = {'placement_sources': ('synthetic-camera',), 'retreat_sources': ('synthetic-retreat',)}
            ledger = BusinessLedger(path, **sources)
            key = 'harbor-business-' + uuid4().hex
            ledger.prepare(key, 7, (self.binding,), outbound_id=self.binding.cargo_id,
                           outbound_stand=self.binding.destination, move_arms=(('A',),))
            mission = ledger.propose(key, self.client)
            ledger.refresh(key, self.client, now=datetime.now(timezone.utc))
            waiting = ledger.read(key)
            self.assertEqual(waiting['state'], 'WAITING')
            self.assertFalse(waiting['goal_confirmed'])

            # This separate test actor represents the console operator, outside Harbor.
            def supervisor(method, endpoint, body=None):
                payload = None if body is None else json.dumps(body).encode()
                request = Request(self.config['origin'] + endpoint, data=payload, method=method,
                                  headers={'Authorization': 'Bearer ' + self.config['approver'],
                                           'Content-Type': 'application/json'})
                with urlopen(request, timeout=10) as response:
                    return json.load(response)

            generation = supervisor('GET', '/api/fleet/dispatch-control')['generation']
            supervisor('POST', f'/api/fleet/missions/{mission}/admit', {'expected_generation': generation})
            deadline = time.monotonic() + 40
            while True:
                job = self.client.job(mission)['job']
                self.assertNotEqual(job['status'], 'HOLD', job.get('reason'))
                if job['status'] == 'GOAL_CONFIRMED':
                    break
                if time.monotonic() >= deadline:
                    self.fail('Synthetic Fleet goal did not complete within 40 seconds')
                time.sleep(.1)

            now = datetime.now(timezone.utc)
            ledger.refresh(key, self.client, now=now)
            current = ledger.read(key)
            self.assertTrue(current['goal_confirmed'])
            self.assertEqual(current['state'], 'WAITING')
            # Synthetic stand-ins for a camera placement and a platform retreat record (no producer yet).
            def observed(evidence_id, when, *, placement=None, retreat=None, arm=None):
                fact = replace(ArmRetreat('A', 'synthetic-zone', True, when, 1, True), **(arm or {}))
                return MoveEvidence(
                    replace(PlacementEvidence(evidence_id + '-placement', mission, current['attempt_id'], 7,
                                              'synthetic-camera', when, self.binding.cargo_id,
                                              self.binding.destination), **(placement or {})),
                    replace(RetreatEvidence(evidence_id + '-retreat', mission, current['attempt_id'], 7,
                                            'synthetic-retreat', when, (fact,)), **(retreat or {})))
            evidence_id = 'synthetic-move-' + uuid4().hex
            evidence = observed(evidence_id, now)
            for bad in (observed(evidence_id, now, placement={'source_id': 'unknown-observer'}),
                        observed(evidence_id, now, placement={'attempt_id': 'wrong-attempt'}),
                        observed(evidence_id, now, placement={'cargo_id': 'wrong-cargo'}),
                        observed(evidence_id, now - timedelta(seconds=10)),
                        observed(evidence_id, now, retreat={'source_id': 'synthetic-camera'}),
                        observed(evidence_id, now, arm={'claim_released': False}),
                        observed(evidence_id, now, arm={'outside_zone': False})):
                with self.subTest(observation=bad), self.assertRaises(ValueError):
                    ledger.confirm(key, bad, now=now)
                self.assertEqual(ledger.read(key)['state'], 'WAITING')

            restarted = BusinessLedger(path, **sources)
            with self.assertRaisesRegex(ValueError, 'Fresh Fleet reconciliation'):
                restarted.confirm(key, evidence, now=now)
            now = datetime.now(timezone.utc)
            restarted.refresh(key, self.client, now=now)
            restarted.confirm(key, observed(evidence_id, now), now=now)
            self.assertEqual(restarted.read(key)['state'], 'AWAITING_FINAL_EVIDENCE')
            with self.assertRaises(ValueError):
                restarted.finish(key, evidence, now=now)
            placement = observed('synthetic-final-' + uuid4().hex, now)
            restarted.finish(key, placement, now=now)
            self.assertEqual(restarted.read(key)['state'], 'COMPLETED')


if __name__ == '__main__':
    unittest.main()
