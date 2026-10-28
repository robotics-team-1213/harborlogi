"""Real HTTP/UDS and synthetic source facts; no Gazebo or hardware acceptance."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from urllib.error import HTTPError

from harbor.physical_evidence import PhysicalEvidenceAdapter, RetreatBinding
from harbor.process import ArmTransferBinding
from harbor.rosy_http import RosyClient
from harbor.workflow import BusinessLedger


class PhysicalEvidenceHttpTests(unittest.TestCase):
    def test_original_authenticated_proof_confirms_one_move_but_never_finishes_the_business(self):
        config = json.loads(Path(os.environ['HARBOR_INTEGRATION_CONFIG']).read_text(encoding='utf-8'))
        self.assertEqual(config['schema'], 'harbor.physical-evidence-http-test/1')
        self.assertIs(config['simulation'], True)
        self.assertNotIn('observer_token', config)
        client = RosyClient(config['origin'], config['operator'])
        client.expected_principal, client.expected_role = 'operator', 'service'
        binding = ArmTransferBinding(**config['binding'])
        arms = tuple(RetreatBinding(**{**arm, 'joint_names': tuple(arm['joint_names'])})
                     for arm in config['evidence']['arms'])
        adapter = PhysicalEvidenceAdapter(config['evidence']['placement_source'],
            config['evidence']['retreat_source'], config['evidence']['policy_digest'], arms)
        key = 'neutral-arm-http'
        with tempfile.TemporaryDirectory() as folder:
            ledger = BusinessLedger(Path(folder) / 'business.sqlite3',
                placement_sources=(adapter.placement_source,), retreat_sources=(adapter.retreat_source,))
            ledger.prepare(key, 1, (binding,), outbound_id=binding.cargo_id,
                           outbound_stand=binding.destination, move_arms=(('A', 'B'),))
            mission = ledger.propose(key, client)
            deadline = time.monotonic() + 30
            while True:
                ledger.refresh(key, client, now=datetime.now(timezone.utc))
                row = ledger.read(key)
                self.assertEqual(row['state'], 'WAITING')
                if row['goal_confirmed']:
                    break
                if time.monotonic() >= deadline:
                    self.fail('Independent fixture observer did not close the neutral transfer')
                time.sleep(.1)
            with self.assertRaises(ValueError):
                ledger.confirm(key, None, now=datetime.now(timezone.utc))
            original = client.physical_evidence(mission)
            evidence = adapter.read(client, binding, request_key=key + ':0', mission_id=mission,
                attempt_id=row['attempt_id'], plan_revision=1, required_arms=('A', 'B'), now=datetime.now(timezone.utc))
            self.assertEqual(len(evidence.retreat.arms), 2)
            self.assertEqual(client.physical_evidence(mission), original)
            other = RosyClient(config['origin'], config['viewer'])
            with self.assertRaises(HTTPError) as denied:
                other.physical_evidence(mission)
            self.assertEqual(denied.exception.code, 403)
            ledger.confirm(key, evidence, now=datetime.now(timezone.utc))
            self.assertEqual(ledger.read(key)['state'], 'AWAITING_FINAL_EVIDENCE')
            with self.assertRaises(ValueError):
                ledger.finish(key, evidence, now=datetime.now(timezone.utc))
            self.assertEqual(ledger.read(key)['state'], 'AWAITING_FINAL_EVIDENCE')
        self.assertFalse(any(name == prefix or name.startswith(prefix + '.') for name in sys.modules
                             for prefix in ('fleet', 'core_common', 'omx_adapter', 'rosy')))


if __name__ == '__main__':
    unittest.main()
