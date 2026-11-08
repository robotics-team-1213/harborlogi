import hashlib
import json
from datetime import datetime, timezone
from tempfile import TemporaryDirectory
from pathlib import Path
import unittest
from unittest.mock import patch

from harbor.workflow import BusinessLedger
from harbor.process import ArmTransferBinding
from harbor.rosy_http import RosyClient


class RecoveryTests(unittest.TestCase):
    def test_original_key_get_only_recovery_keeps_hold(self):
        with TemporaryDirectory() as directory:
            ledger = BusinessLedger(Path(directory) / 'ledger.db', placement_sources=('camera',), retreat_sources=('owner',))
            binding = ArmTransferBinding('C', 'bay', 'out', 'cell', 'instance', 'object', 'source', 'target')
            ledger.prepare('request', 1, (binding,), outbound_id='C', outbound_stand='out', move_arms=(('A',),))
            ledger.hold('request')
            calls = []
            transfer = {k: v for k, v in binding.proposal_request('request:0').items() if k != 'request_key'}
            job = dict(action_kind='ARM_TRANSFER', request_key='request:0', transfer=transfer,
                request_digest=hashlib.sha256(json.dumps(transfer, sort_keys=True, separators=(',', ':')).encode()).hexdigest(),
                mission_id='mission-1', status='READY')
            def transport(method, path, body):
                calls.append((method, path))
                return {'job': job}
            client = RosyClient(transport=transport)
            ledger.recover_arm('request', client, now=datetime.now(timezone.utc))
            self.assertEqual(ledger.read('request')['state'], 'HOLD')
            self.assertEqual(ledger.read('request')['mission_id'], 'mission-1')
            self.assertEqual(calls, [('GET', '/api/fleet/arm-transfers/jobs?request_key=request%3A0')])
            job['mission_id'] = 'other-mission'
            with self.assertRaises(ValueError):
                ledger.recover_arm('request', client, now=datetime.now(timezone.utc))
            self.assertEqual(ledger.read('request')['mission_id'], 'mission-1')

    def prepared(self, directory):
        ledger = BusinessLedger(Path(directory) / 'ledger.db', placement_sources=('camera',), retreat_sources=('owner',))
        binding = ArmTransferBinding('C', 'bay', 'out', 'cell', 'instance', 'object', 'source', 'target')
        ledger.prepare('request', 1, (binding,), outbound_id='C', outbound_stand='out', move_arms=(('A',),))
        transfer = {k: v for k, v in binding.proposal_request('request:0').items() if k != 'request_key'}
        job = dict(action_kind='ARM_TRANSFER', request_key='request:0', transfer=transfer,
            request_digest=hashlib.sha256(json.dumps(transfer, sort_keys=True, separators=(',', ':')).encode()).hexdigest(),
            mission_id='mission-1', status='READY', resource_policy_digest='c' * 64, authority_epoch=2,
            dispatch_generation=3)
        return ledger, binding, job

    def receipt(self, binding, job, **changes):
        return {**dict(receipt_id='receipt-1', mission_id=job['mission_id'], action_id='action-1',
            attempt_id='attempt-1', request_digest='e' * 64, workcell_id=binding.workcell_id,
            instance_id=binding.instance_id, policy_digest=job['resource_policy_digest'],
            evidence_digest='d' * 64, object_ref=binding.object_ref, source=binding.source_ref,
            target=binding.target_ref, authority_epoch=job['authority_epoch'],
            dispatch_generation=job['dispatch_generation'], validated_at_ns=2_000_000_000,
            physical_status='AT_TARGET_AND_CLEAR'), **changes}

    def test_resume_reenters_waiting_from_bookkeeping_hold(self):
        with TemporaryDirectory() as directory:
            ledger, binding, job = self.prepared(directory)
            ledger.propose('request', RosyClient(transport=lambda method, path, body: {'job': dict(job)}))
            ledger.hold('request')
            calls = []

            def transport(method, path, body):
                calls.append((method, path))
                if path.endswith('/physical-receipt'):
                    return {'receipt': self.receipt(binding, job)}
                return {'job': dict(job)}
            ledger.resume_arm('request', RosyClient(transport=transport), now=datetime.now(timezone.utc))
            row = ledger.read('request')
            self.assertEqual(row['state'], 'WAITING')
            self.assertEqual(row['mission_id'], 'mission-1')
            self.assertEqual(row['goal_confirmed'], 0)
            self.assertTrue(all(method == 'GET' for method, _ in calls))

    def test_resume_adopts_mission_when_hold_lost_the_response(self):
        with TemporaryDirectory() as directory:
            ledger, binding, job = self.prepared(directory)
            ledger.hold('request')  # Crash before the proposal response arrived.
            self.assertIsNone(ledger.read('request')['mission_id'])
            ledger.resume_arm('request', RosyClient(transport=lambda m, p, b: {'job': dict(job)}),
                              now=datetime.now(timezone.utc))
            row = ledger.read('request')
            self.assertEqual(row['state'], 'WAITING')
            self.assertEqual(row['mission_id'], 'mission-1')

    def test_resume_with_confirmed_goal_sets_attempt_and_requires_matching_receipt(self):
        for status, receipt_changes, expected in (
                ('GOAL_CONFIRMED', {}, 1), ('GOAL_CONFIRMED', {'policy_digest': 'f' * 64}, None)):
            with self.subTest(receipt_changes=receipt_changes), TemporaryDirectory() as directory:
                ledger, binding, job = self.prepared(directory)
                job['status'] = status
                ledger.hold('request')

                def transport(method, path, body):
                    if path.endswith('/physical-receipt'):
                        return {'receipt': self.receipt(binding, job, **receipt_changes)}
                    return {'job': dict(job)}
                if expected is None:
                    with self.assertRaises(ValueError):
                        ledger.resume_arm('request', RosyClient(transport=transport),
                                          now=datetime.now(timezone.utc))
                    self.assertEqual(ledger.read('request')['state'], 'HOLD')
                else:
                    ledger.resume_arm('request', RosyClient(transport=transport),
                                      now=datetime.now(timezone.utc))
                    row = ledger.read('request')
                    self.assertEqual(row['state'], 'WAITING')
                    self.assertEqual(row['goal_confirmed'], expected)
                    self.assertEqual(row['attempt_id'], 'attempt-1')

    def test_resume_refuses_non_hold_rows_and_keeps_fleet_holds(self):
        with TemporaryDirectory() as directory:
            ledger, binding, job = self.prepared(directory)
            client = RosyClient(transport=lambda m, p, b: {'job': dict(job)})
            ledger.propose('request', client)
            with self.assertRaises(ValueError):
                ledger.resume_arm('request', client, now=datetime.now(timezone.utc))
            job['status'] = 'HOLD'
            ledger.hold('request')
            ledger.resume_arm('request', client, now=datetime.now(timezone.utc))
            self.assertEqual(ledger.read('request')['state'], 'HOLD')
            job['mission_id'] = 'other-mission'
            with self.assertRaises(ValueError):
                ledger.resume_arm('request', client, now=datetime.now(timezone.utc))

    def test_reconciliation_rejects_principal_before_receipt_or_ledger_write(self):
        with TemporaryDirectory() as directory:
            ledger, binding, job = self.prepared(directory)
            ledger.hold('request')
            job.update(status='GOAL_CONFIRMED', proposal_principal_id='other-principal')
            before, calls = ledger.read('request'), []

            def transport(method, path, body):
                calls.append((method, path))
                return {'job': dict(job)}

            with self.assertRaisesRegex(ValueError, 'principal'):
                ledger.reconcile_arm('request', RosyClient(transport=transport),
                    now=datetime.now(timezone.utc), resume=True, principal='harbor')
            self.assertEqual(ledger.read('request'), before)
            self.assertEqual(calls, [('GET', '/api/fleet/arm-transfers/jobs?request_key=request%3A0')])

    def test_reconciliation_cannot_overwrite_a_hold_changed_during_the_read(self):
        with TemporaryDirectory() as directory:
            ledger, binding, job = self.prepared(directory)
            ledger.hold('request')

            def transport(method, path, body):
                ledger.hold('request')
                return {'job': dict(job)}

            with self.assertRaisesRegex(ValueError, 'changed'):
                ledger.reconcile_arm('request', RosyClient(transport=transport),
                    now=datetime.now(timezone.utc), resume=True)
            row = ledger.read('request')
            self.assertEqual(row['state'], 'HOLD')
            self.assertIsNone(row['mission_id'])

    def test_reconciliation_returns_receipt_without_advancing_business_hold(self):
        with TemporaryDirectory() as directory:
            ledger, binding, job = self.prepared(directory)
            ledger.hold('request')
            job['status'] = 'GOAL_CONFIRMED'
            receipt = self.receipt(binding, job)
            calls = []

            def transport(method, path, body):
                calls.append((method, path))
                return {'receipt': receipt} if path.endswith('/physical-receipt') else {'job': dict(job)}

            execution = ledger.reconcile_arm('request', RosyClient(transport=transport),
                                              now=datetime.now(timezone.utc))
            self.assertTrue(execution.goal_confirmed)
            self.assertFalse(execution.held)
            self.assertEqual(execution.mission_id, 'mission-1')
            self.assertEqual(execution.attempt_id, receipt['attempt_id'])
            self.assertEqual(execution.receipt, receipt)
            self.assertEqual(ledger.read('request')['state'], 'HOLD')
            self.assertEqual(ledger.read('request')['goal_confirmed'], 0)
            self.assertEqual(calls, [('GET', '/api/fleet/arm-transfers/jobs?request_key=request%3A0'),
                                     ('GET', '/api/fleet/arm-transfers/jobs/mission-1/physical-receipt')])

    def test_compatibility_entry_points_keep_the_original_ledger_version(self):
        for operation in ('refresh', 'resume_arm', 'recover_arm'):
            with self.subTest(operation=operation), TemporaryDirectory() as directory:
                ledger, binding, job = self.prepared(directory)
                client = RosyClient(transport=lambda m, p, b: {'job': dict(job)})
                if operation == 'refresh':
                    ledger.propose('request', client)
                else:
                    ledger.hold('request')
                read = ledger.read
                changed = False

                def read_then_hold(key):
                    nonlocal changed
                    original = read(key)
                    if not changed:
                        changed = True
                        ledger.hold(key)
                    return original

                with patch.object(ledger, 'read', side_effect=read_then_hold):
                    with self.assertRaisesRegex(ValueError, 'changed'):
                        getattr(ledger, operation)('request', client, now=datetime.now(timezone.utc))
                self.assertEqual(ledger.read('request')['state'], 'HOLD')
