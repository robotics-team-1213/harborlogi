"""Fleet physical proof (v1.330) becomes business evidence only when it binds to the verified receipt."""
from datetime import timedelta
from hashlib import sha256
import json
from pathlib import Path
import tempfile
import unittest

from harbor.physical_evidence import fleet_move_evidence, fleet_recheck_evidence
from harbor.rosy_http import RosyClient
from harbor.workflow import BusinessLedger
from test_arm_transfer_execution import Fleet, binding
from test_execution import SOURCES
from test_policy import NOW

NS = int(NOW.timestamp()) * 1_000_000_000


def owner(instance, sequence, **changes):
    return {'workcell_id': 'workcell', 'instance_id': instance, 'joints': {'sample_id': f'{instance}-{sequence}',
        'sequence': sequence, 'observed_at_ns': NS + sequence, 'joint_names': ['joint1', 'joint2'],
        'positions_rad': [0.1, -0.2]}, 'held_state': 'NONE', 'held_object_ref': None, **changes}


def document(phase='POST_ACTION', first=3, attempt='attempt-1', **owner_changes):
    samples = [{'sequence': s, 'object_observed_at_ns': NS + s,
                'owners': [owner('installed', s, **owner_changes), owner('partner', s)]} for s in (first, first + 1)]
    return json.dumps({'context': {'phase': phase, 'action_id': 'action-1', 'attempt_id': attempt},
                       'samples': samples}, sort_keys=True)


class ProofFleet(Fleet):
    def __init__(self, proof=None, zones=('workcell.shared',)):
        super().__init__()
        self.proof, self.zones = proof or document(), list(zones)
        self.receipt_changes = {'evidence_digest': sha256(self.proof.encode()).hexdigest()}

    def __call__(self, method, path, body):
        if path.endswith('/rechecks'):
            self.calls.append((method, path, body))
            proof = getattr(self, 'recheck_proof', None)
            return {'recheck': dict(mission_id='physical-1', recheck_key=body['recheck_key'], action_id='action-1',
                attempt_id=getattr(self, 'recheck_attempt', 'attempt-1'), requested_at_ns=NS,
                status='WAITING_OBSERVATIONS' if proof is None else 'CONFIRMED', evidence_document=proof,
                evidence_digest=None if proof is None else sha256(proof.encode()).hexdigest(),
                validated_at_ns=None if proof is None else NS + 9, zone_ids=self.zones)}
        if path.endswith('/physical-evidence'):
            receipt = super().__call__('GET', path.replace('physical-evidence', 'physical-receipt'), None)['receipt']
            return {'receipt': receipt, 'evidence_document': self.proof, 'released_zone_ids': self.zones}
        return super().__call__(method, path, body)


class PhysicalEvidenceTests(unittest.TestCase):
    def confirmed(self, tmp, fleet):
        ledger = BusinessLedger(Path(tmp) / 'business.sqlite3', **SOURCES)
        ledger.prepare('outbound', 3, (binding(),), outbound_id='outbound', outbound_stand='stand',
                       move_arms=(('A', 'B'),))
        client = RosyClient(transport=fleet)
        ledger.submit('outbound', client, principal='harbor')
        fleet.status = 'GOAL_CONFIRMED'
        ledger.refresh('outbound', client, now=NOW + timedelta(seconds=1))
        return ledger, client

    def evidence(self, client, **overrides):
        return fleet_move_evidence(client, binding(), 'outbound:0', mission_id='physical-1', revision=3,
            placement_source='installed-camera', retreat_source='fleet-retreat')

    def test_bound_proof_closes_the_business_step(self):
        fleet = ProofFleet()
        with tempfile.TemporaryDirectory() as tmp:
            ledger, client = self.confirmed(tmp, fleet)
            evidence = self.evidence(client)
            self.assertEqual((evidence.placement.cargo_id, evidence.placement.location), ('outbound', 'stand'))
            self.assertLess(abs(evidence.placement.observed_at - NOW), timedelta(milliseconds=1))
            # One opaque fact per acting owner; no Fleet instance id or joint value is kept (ADR 0029).
            self.assertEqual([a.arm for a in evidence.retreat.arms], ['fleet-owner-1', 'fleet-owner-2'])
            self.assertNotIn('installed', json.dumps([vars(a) for a in evidence.retreat.arms], default=str))
            self.assertTrue(all(a.claim_released and a.outside_zone and a.pose_revision == 4 for a in evidence.retreat.arms))
            ledger.confirm('outbound', evidence, now=NOW + timedelta(seconds=1))
            self.assertEqual(ledger.read('outbound')['state'], 'AWAITING_FINAL_EVIDENCE')

    def test_unbound_or_incomplete_proof_never_becomes_evidence(self):
        tampered = ProofFleet()
        tampered.proof = document().replace('0.1', '0.3')
        cases = {'digest': tampered, 'zones': ProofFleet(zones=()),
                 'joints': ProofFleet(document(joints=None)),
                 'attempt': ProofFleet(document().replace('attempt-1', 'attempt-0'))}
        for name, fleet in cases.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as tmp:
                _, client = self.confirmed(tmp, fleet)
                with self.assertRaises(ValueError):
                    self.evidence(client)
        # Fleet facts that do not show release or retreat leave the stage unconfirmed (HOLD), never True by default.
        for name, fleet in {'zones': ProofFleet(zones=('a', 'b')),
                            'held': ProofFleet(document(held_state='HELD', held_object_ref='outbound'))}.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as tmp:
                ledger, client = self.confirmed(tmp, fleet)
                with self.assertRaises(ValueError):
                    ledger.confirm('outbound', self.evidence(client), now=NOW + timedelta(seconds=1))
                self.assertEqual(ledger.read('outbound')['step_index'], 0)

    def recheck(self, client):
        return fleet_recheck_evidence(client, binding(), 'outbound:0', 'final-1', mission_id='physical-1', revision=3,
            placement_source='installed-camera', retreat_source='fleet-retreat')

    def test_fresh_recheck_closes_final_completion_that_the_step_proof_cannot(self):
        fleet = ProofFleet()
        with tempfile.TemporaryDirectory() as tmp:
            ledger, client = self.confirmed(tmp, fleet)
            ledger.confirm('outbound', self.evidence(client), now=NOW + timedelta(seconds=1))
            with self.assertRaises(ValueError):  # the step's own proof was already consumed
                ledger.finish('outbound', self.evidence(client), now=NOW + timedelta(seconds=1))
            self.assertIsNone(self.recheck(client))
            fleet.recheck_proof = document('RECHECK', first=7)
            final = self.recheck(client)
            self.assertEqual(final.retreat.arms[0].pose_revision, 8)
            ledger.finish('outbound', final, now=NOW + timedelta(seconds=1))
            self.assertEqual(ledger.read('outbound')['state'], 'COMPLETED')
            self.assertEqual(sum(path.endswith('/rechecks') for _, path, _ in fleet.calls), 2)

    def test_recheck_of_another_attempt_or_phase_is_refused(self):
        for changes in ({'recheck_proof': document('POST_ACTION', first=7)}, {'recheck_attempt': 'attempt-0'},
                        {'recheck_proof': document('RECHECK', first=7, attempt='attempt-0')}):
            with self.subTest(changes), tempfile.TemporaryDirectory() as tmp:
                fleet = ProofFleet()
                _, client = self.confirmed(tmp, fleet)
                vars(fleet).update(changes)
                with self.assertRaises(ValueError):
                    self.recheck(client)

    def test_unconfirmed_job_reads_no_proof(self):
        fleet = ProofFleet()
        client = RosyClient(transport=fleet)
        fleet('POST', '/api/fleet/arm-transfers/jobs', binding().proposal_request('outbound:0'))
        with self.assertRaises(ValueError):
            self.evidence(client)
        self.assertFalse(any(path.endswith('/physical-evidence') for _, path, _ in fleet.calls))


if __name__ == '__main__':
    unittest.main()
