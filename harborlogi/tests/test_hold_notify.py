"""D-997 Harbor row: business HOLD notification outbox identity, atomicity, and delivery gate."""
from contextlib import closing
import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from harbor.hold_notify import (DefiniteDeliveryFailure, HoldNotifier, UnknownDeliveryFailure,
                                occurrence_id, pre_send_check)
from harbor.ledger import ReadOnlyLedger
from harbor.process import ArmTransferBinding
from harbor.rosy_http import RosyClient
from harbor.workflow import BusinessLedger


FIXED = datetime(2026, 11, 8, 12, 0, 0, tzinfo=timezone.utc)


class HoldNotificationTests(unittest.TestCase):
    def ledger(self, directory, **changes):
        options = dict(placement_sources=('camera',), retreat_sources=('owner',),
                       clock=lambda: FIXED, **changes)
        return BusinessLedger(Path(directory) / 'ledger.db', **options)

    def prepared(self, directory, **changes):
        ledger = self.ledger(directory, **changes)
        binding = ArmTransferBinding('C', 'bay', 'out', 'cell', 'instance', 'object', 'source', 'target')
        ledger.prepare('request', 1, (binding,), outbound_id='C', outbound_stand='out', move_arms=(('A',),))
        transfer = {k: v for k, v in binding.proposal_request('request:0').items() if k != 'request_key'}
        job = dict(action_kind='ARM_TRANSFER', request_key='request:0', transfer=transfer,
            request_digest=hashlib.sha256(json.dumps(transfer, sort_keys=True, separators=(',', ':')).encode()).hexdigest(),
            mission_id='mission-1', status='READY', resource_policy_digest='c' * 64, authority_epoch=2,
            dispatch_generation=3)
        return ledger, binding, job

    def client(self, job):
        return RosyClient(transport=lambda method, path, body: {'job': dict(job)})

    def outbox(self, ledger):
        return HoldNotifier(ledger).outbox()

    def deliveries(self, ledger):
        return HoldNotifier(ledger).deliveries()

    def test_hold_records_one_occurrence_and_polling_keeps_it(self):
        with TemporaryDirectory() as directory:
            ledger, binding, job = self.prepared(directory)
            ledger.hold('request')
            self.assertEqual(ledger.read('request')['state'], 'HOLD')
            rows = self.outbox(ledger)
            self.assertEqual([r['occurrence_id'] for r in rows], ['request#hold-1'])
            self.assertEqual(ledger.read('request')['hold_sequence'], 1)
            ledger.hold('request')  # HOLD → HOLD bookkeeping is the same occurrence.
            ledger.recover_arm('request', self.client(job), now=FIXED)  # A poll that stays HOLD.
            self.assertEqual(ledger.read('request')['state'], 'HOLD')
            self.assertEqual([r['occurrence_id'] for r in self.outbox(ledger)], ['request#hold-1'])

    def test_hold_exit_then_reentry_creates_a_new_occurrence(self):
        with TemporaryDirectory() as directory:
            ledger, binding, job = self.prepared(directory)
            ledger.hold('request')
            ledger.resume_arm('request', self.client(job), now=FIXED)
            self.assertEqual(ledger.read('request')['state'], 'WAITING')
            self.assertEqual([r['occurrence_id'] for r in self.outbox(ledger)], ['request#hold-1'])
            ledger.hold('request')
            self.assertEqual(ledger.read('request')['hold_sequence'], 2)
            self.assertEqual([r['occurrence_id'] for r in self.outbox(ledger)],
                             ['request#hold-1', 'request#hold-2'])

    def test_refresh_and_reconcile_entering_hold_record_the_occurrence(self):
        with TemporaryDirectory() as directory:
            ledger, binding, job = self.prepared(directory)
            ledger.propose('request', self.client(job))
            self.assertEqual(ledger.read('request')['state'], 'WAITING')
            job['status'] = 'HOLD'
            ledger.refresh('request', self.client(job), now=FIXED)  # WAITING → HOLD through a read.
            self.assertEqual(ledger.read('request')['state'], 'HOLD')
            self.assertEqual([r['occurrence_id'] for r in self.outbox(ledger)], ['request#hold-1'])
            ledger.reconcile_arm('request', self.client(job), now=FIXED)  # HOLD → HOLD: nothing new.
            self.assertEqual([r['occurrence_id'] for r in self.outbox(ledger)], ['request#hold-1'])

    def test_failed_submission_records_a_hold_occurrence(self):
        with TemporaryDirectory() as directory:
            ledger, binding, job = self.prepared(directory)

            def transport(method, path, body):
                raise OSError('connection refused')
            with self.assertRaises(OSError):
                ledger.submit('request', RosyClient(transport=transport))
            self.assertEqual(ledger.read('request')['state'], 'HOLD')
            self.assertEqual([r['occurrence_id'] for r in self.outbox(ledger)], ['request#hold-1'])

    def test_outbox_write_is_atomic_with_the_hold_transition(self):
        with TemporaryDirectory() as directory:
            ledger, binding, job = self.prepared(directory)
            with closing(sqlite3.connect(Path(directory) / 'ledger.db')) as direct:  # A conflicting row.
                with direct:
                    direct.execute("INSERT INTO hold_outbox VALUES(?,?,?,?,?,?,?)",
                                   ('request#hold-1', '0' * 32, 'request', 1, '', '', '{}'))
            with self.assertRaises(sqlite3.IntegrityError):
                ledger.hold('request')
            self.assertEqual(ledger.read('request')['state'], 'PLANNED')
            self.assertEqual(ledger.read('request')['hold_sequence'], 0)

    def test_restart_recovery_records_a_new_occurrence_and_preserves_the_outbox(self):
        with TemporaryDirectory() as directory:
            ledger, binding, job = self.prepared(directory)
            ledger.hold('request')
            with closing(sqlite3.connect(Path(directory) / 'ledger.db')) as direct:  # Crash mid-submission.
                with direct:
                    direct.execute("UPDATE business SET state='SUBMITTING' WHERE request_key='request'")
            reopened = self.ledger(directory)
            self.assertEqual(reopened.read('request')['state'], 'HOLD')
            self.assertEqual(reopened.read('request')['hold_sequence'], 2)
            rows = self.outbox(reopened)
            self.assertEqual([r['occurrence_id'] for r in rows], ['request#hold-1', 'request#hold-2'])
            self.assertTrue(all(r['notification_id'] for r in rows))

    def test_old_ledger_schema_gains_hold_sequence_and_still_records_occurrences(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'ledger.db'
            with closing(sqlite3.connect(path)) as direct:  # A pre-ADR-0029 ledger shape.
                with direct:
                    direct.execute("CREATE TABLE business (request_key TEXT PRIMARY KEY, digest TEXT NOT NULL, "
                                   "payload TEXT NOT NULL, state TEXT NOT NULL, step_index INTEGER NOT NULL DEFAULT 0, "
                                   "row_version INTEGER NOT NULL DEFAULT 0, mission_id TEXT, attempt_id TEXT, "
                                   "goal_confirmed INTEGER NOT NULL DEFAULT 0, outbound_mission_id TEXT, "
                                   "outbound_attempt_id TEXT)")
                    direct.execute("INSERT INTO business(request_key,digest,payload,state) "
                                   "VALUES('request','d','{}','PLANNED')")
            ledger = self.ledger(directory)
            self.assertEqual(ledger.read('request')['hold_sequence'], 0)
            ledger.hold('request')
            self.assertEqual(ledger.read('request')['state'], 'HOLD')
            self.assertEqual([r['occurrence_id'] for r in self.outbox(ledger)], ['request#hold-1'])

    def test_body_carries_only_opaque_fields(self):
        with TemporaryDirectory() as directory:
            ledger, binding, job = self.prepared(directory, screen_base='https://harbor.invalid')
            ledger.hold('request')
            row = self.outbox(ledger)[0]
            payload = json.loads(row['payload'])
            self.assertEqual(payload, {
                'classification': 'INTERVENTION_REQUIRED', 'notification_id': row['notification_id'],
                'occurred_at': FIXED.isoformat(),
                'expires_at': (FIXED + timedelta(hours=1)).isoformat(),
                'screen_url': f'https://harbor.invalid/work/{row["notification_id"]}'})
            self.assertNotIn('?', payload['screen_url'])
            self.assertNotIn('request', row['payload'])  # The raw business key never reaches a channel.

    def test_notification_link_resolves_the_business_row(self):
        with TemporaryDirectory() as directory:
            ledger, binding, job = self.prepared(directory)
            ledger.hold('request')
            notification = self.outbox(ledger)[0]['notification_id']
            reader = ReadOnlyLedger(Path(directory) / 'ledger.db')
            self.assertEqual(reader.read(notification)['request_key'], 'request')
            self.assertEqual(reader.read('request')['state'], 'HOLD')  # The key path still works.
            with self.assertRaises(KeyError):
                reader.read('0' * 32)

    def test_screen_base_must_be_a_protected_https_origin(self):
        with TemporaryDirectory() as directory:
            for base in ('http://fleet.example/', 'http://localhost:8040/', 'https://harbor.invalid/ops?token=1',
                         'https://user:pw@harbor.invalid/', 'https://harbor.invalid/#frag'):
                with self.subTest(base=base):
                    with self.assertRaises(ValueError):
                        self.prepared(directory, screen_base=base)

    def test_delivery_stays_off_without_approved_channels(self):
        with TemporaryDirectory() as directory:
            ledger, binding, job = self.prepared(directory)
            ledger.hold('request')
            self.assertEqual(HoldNotifier(ledger).deliver_pending(now=FIXED + timedelta(minutes=5)), [])
            self.assertEqual(self.deliveries(ledger), [])
            self.assertEqual(ledger.read('request')['state'], 'HOLD')  # The local HOLD stays.
            with self.assertRaises(ValueError):
                HoldNotifier(ledger, client=lambda channel, body: None)

    def test_waiting_notification_is_rejected_after_the_hold_ends(self):
        with TemporaryDirectory() as directory:
            ledger, binding, job = self.prepared(directory)
            ledger.hold('request')
            ledger.resume_arm('request', self.client(job), now=FIXED)
            sent = []
            notifier = HoldNotifier(ledger, channels=('ops',),
                                    client=lambda channel, body: sent.append((channel, body)))
            self.assertEqual(notifier.deliver_pending(now=FIXED + timedelta(minutes=5)), [])
            self.assertEqual(sent, [])
            delivery = self.deliveries(ledger)[0]
            self.assertEqual((delivery['occurrence_id'], delivery['channel']), ('request#hold-1', 'ops'))
            self.assertEqual(delivery['state'], 'REJECTED')
            self.assertIn('left this HOLD', delivery['note'])

    def test_valid_occurrence_is_sent_once_per_approved_channel(self):
        with TemporaryDirectory() as directory:
            ledger, binding, job = self.prepared(directory)
            ledger.hold('request')
            sent = []
            notifier = HoldNotifier(ledger, channels=('ops',),
                                    client=lambda channel, body: sent.append((channel, body)))
            self.assertEqual(notifier.deliver_pending(now=FIXED + timedelta(minutes=5)), ['request#hold-1'])
            self.assertEqual([channel for channel, _ in sent], ['ops'])
            self.assertEqual(sent[0][1]['notification_id'], self.outbox(ledger)[0]['notification_id'])
            self.assertEqual(self.deliveries(ledger)[0]['state'], 'SENT')
            self.assertEqual(notifier.deliver_pending(now=FIXED + timedelta(minutes=6)), [])
            self.assertEqual(len(sent), 1)

    def test_final_channel_state_never_hides_or_overwrites_another_channel(self):
        with TemporaryDirectory() as directory:
            ledger, binding, job = self.prepared(directory)
            ledger.hold('request')
            first = HoldNotifier(ledger, channels=('a',), client=lambda channel, body: None)
            self.assertEqual(first.deliver_pending(now=FIXED + timedelta(minutes=5)), ['request#hold-1'])
            sent = []
            wider = HoldNotifier(ledger, channels=('a', 'b'),
                                 client=lambda channel, body: sent.append(channel))
            self.assertEqual(wider.deliver_pending(now=FIXED + timedelta(minutes=6)), ['request#hold-1'])
            self.assertEqual(sent, ['b'])  # The missing b attempt runs; settled a is not resent.
            ledger.resume_arm('request', self.client(job), now=FIXED)  # HOLD ends afterwards.
            sent.clear()
            after = HoldNotifier(ledger, channels=('a', 'c'),
                                 client=lambda channel, body: sent.append(channel))
            self.assertEqual(after.deliver_pending(now=FIXED + timedelta(minutes=7)), [])
            self.assertEqual(sent, [])  # a stays final; the late c only settles as rejected.
            states = {d['channel']: d['state'] for d in self.deliveries(ledger)}
            self.assertEqual(states, {'a': 'SENT', 'b': 'SENT', 'c': 'REJECTED'})

    def test_later_failure_does_not_resend_an_already_sent_occurrence(self):
        with TemporaryDirectory() as directory:
            ledger, binding, job = self.prepared(directory)
            binding2 = ArmTransferBinding('D', 'bay2', 'out', 'cell', 'instance', 'object', 'source', 'target')
            ledger.prepare('second', 1, (binding2,), outbound_id='D', outbound_stand='out',
                           move_arms=(('A',),))
            ledger.hold('request')
            ledger.hold('second')
            notifications = {r['request_key']: r['notification_id'] for r in self.outbox(ledger)}

            def client(channel, body):
                if body['notification_id'] == notifications['second']:
                    raise DefiniteDeliveryFailure('refused before the connection')
                sent.append(body['notification_id'])
            sent = []
            notifier = HoldNotifier(ledger, channels=('ops',), client=client)
            self.assertEqual(notifier.deliver_pending(now=FIXED + timedelta(minutes=5)), ['request#hold-1'])
            second = [d for d in self.deliveries(ledger) if d['channel'] == 'ops'
                      and d['occurrence_id'] == 'second#hold-1'][0]
            self.assertEqual(second['state'], 'PENDING')  # A definite failure may retry later.
            notifier.client = lambda channel, body: sent.append(body['notification_id'])
            self.assertEqual(notifier.deliver_pending(now=FIXED + timedelta(minutes=6)), ['second#hold-1'])
            self.assertEqual(sent, [notifications['request'], notifications['second']])  # No resend.

    def test_definite_failure_retries_only_after_the_interval(self):
        with TemporaryDirectory() as directory:
            ledger, binding, job = self.prepared(directory)
            ledger.hold('request')
            calls = []

            def client(channel, body):
                calls.append(1)
                if len(calls) == 1:
                    raise DefiniteDeliveryFailure('refused before the connection')
            notifier = HoldNotifier(ledger, channels=('ops',), client=client)
            start = FIXED + timedelta(minutes=5)
            self.assertEqual(notifier.deliver_pending(now=start), [])
            self.assertEqual(len(calls), 1)
            self.assertEqual(notifier.deliver_pending(now=start + timedelta(seconds=30)), [])
            self.assertEqual(len(calls), 1)  # Spacing: the retry waits out the interval.
            self.assertEqual(notifier.deliver_pending(now=start + timedelta(seconds=60)), ['request#hold-1'])
            self.assertEqual(len(calls), 2)
            self.assertEqual(self.deliveries(ledger)[0]['state'], 'SENT')

    def test_timeout_on_one_channel_is_unknown_while_the_other_is_sent(self):
        with TemporaryDirectory() as directory:
            ledger, binding, job = self.prepared(directory)
            ledger.hold('request')

            def client(channel, body):
                if channel == 'b':
                    raise UnknownDeliveryFailure('timeout')
                sent.append((channel, body['notification_id']))
            sent = []
            notifier = HoldNotifier(ledger, channels=('a', 'b'), client=client)
            self.assertEqual(notifier.deliver_pending(now=FIXED + timedelta(minutes=5)), ['request#hold-1'])
            states = {d['channel']: d['state'] for d in self.deliveries(ledger)}
            self.assertEqual(states, {'a': 'SENT', 'b': 'UNKNOWN'})
            self.assertEqual(notifier.deliver_pending(now=FIXED + timedelta(minutes=6)), [])
            self.assertEqual(len(sent), 1)  # Neither channel resends.
            self.assertEqual(sent[0][0], 'a')

    def test_unclassified_client_failure_is_unknown_not_resent(self):
        with TemporaryDirectory() as directory:
            ledger, binding, job = self.prepared(directory)
            ledger.hold('request')
            sent = []

            def client(channel, body):
                sent.append(body['notification_id'])
                raise RuntimeError('garbage response')
            notifier = HoldNotifier(ledger, channels=('ops',), client=client)
            self.assertEqual(notifier.deliver_pending(now=FIXED + timedelta(minutes=5)), [])
            self.assertEqual(self.deliveries(ledger)[0]['state'], 'UNKNOWN')
            self.assertEqual(notifier.deliver_pending(now=FIXED + timedelta(minutes=6)), [])
            self.assertEqual(len(sent), 1)

    def test_interrupted_sending_settles_to_unknown_without_resending(self):
        with TemporaryDirectory() as directory:
            ledger, binding, job = self.prepared(directory)
            ledger.hold('request')
            with closing(sqlite3.connect(Path(directory) / 'ledger.db')) as direct:  # A crashed send.
                with direct:
                    direct.execute("INSERT INTO hold_delivery VALUES(?,?,?,?,?)",
                                   ('request#hold-1', 'ops', 'SENDING', None, None))
            sent = []
            notifier = HoldNotifier(ledger, channels=('ops',),
                                    client=lambda channel, body: sent.append(body['notification_id']))
            self.assertEqual(notifier.interrupted(), 1)
            self.assertEqual(notifier.deliver_pending(now=FIXED + timedelta(minutes=5)), [])
            self.assertEqual(sent, [])  # SENDING is neither final nor pending: no blind resend.
            self.assertEqual(notifier.interrupted(), 1)  # Reads and delivery never settle it.
            notifier.settle_interrupted()  # The delivery loop start closes it explicitly.
            self.assertEqual(notifier.interrupted(), 0)
            delivery = self.deliveries(ledger)[0]
            self.assertEqual(delivery['state'], 'UNKNOWN')
            self.assertIn('Interrupted send', delivery['note'])
            self.assertEqual(notifier.deliver_pending(now=FIXED + timedelta(minutes=6)), [])
            self.assertEqual(sent, [])

    def test_second_notifier_during_a_send_leaves_it_unknown_and_unclaimed(self):
        with TemporaryDirectory() as directory:
            ledger, binding, job = self.prepared(directory)
            ledger.hold('request')
            late = []

            def client(channel, body):
                # A delivery-loop start racing the in-flight send settles SENDING first.
                HoldNotifier(ledger, channels=('ops',),
                             client=lambda c, b: late.append(c)).settle_interrupted()
                late.append('first-send-done')
            first = HoldNotifier(ledger, channels=('ops',), client=client)
            self.assertEqual(first.deliver_pending(now=FIXED + timedelta(minutes=5)), [])  # Not claimed.
            delivery = self.deliveries(ledger)[0]
            self.assertEqual(delivery['state'], 'UNKNOWN')
            self.assertIn('Interrupted send', delivery['note'])
            self.assertEqual(late, ['first-send-done'])  # The racing notifier sent nothing either.

    def test_expired_occurrence_is_rejected_without_sending(self):
        with TemporaryDirectory() as directory:
            ledger, binding, job = self.prepared(directory, notify_expiry=timedelta(seconds=30))
            ledger.hold('request')
            sent = []
            notifier = HoldNotifier(ledger, channels=('ops',),
                                    client=lambda channel, body: sent.append((channel, body)))
            self.assertEqual(notifier.deliver_pending(now=FIXED + timedelta(minutes=1)), [])
            self.assertEqual(sent, [])
            self.assertEqual(self.deliveries(ledger)[0]['state'], 'REJECTED')
            self.assertEqual(self.deliveries(ledger)[0]['note'], 'Occurrence expired')

    def test_pre_send_check_covers_channel_scope_and_unknown_occurrences(self):
        with TemporaryDirectory() as directory:
            ledger, binding, job = self.prepared(directory)
            ledger.hold('request')
            with self.assertRaisesRegex(ValueError, 'receive scope'):
                with closing(ledger._connect()) as c:
                    pre_send_check(c, 'request#hold-1', channel='ops', channels=('other',), now=FIXED)
            with self.assertRaisesRegex(ValueError, 'Unknown HOLD occurrence'):
                with closing(ledger._connect()) as c:
                    pre_send_check(c, 'request#hold-9', channel='ops', channels=('ops',), now=FIXED)
            with closing(ledger._connect()) as c:
                row = pre_send_check(c, 'request#hold-1', channel='ops', channels=('ops',), now=FIXED)
                self.assertEqual(row['request_key'], 'request')

    def test_occurrence_identity_does_not_follow_row_version(self):
        with TemporaryDirectory() as directory:
            ledger, binding, job = self.prepared(directory)
            ledger.hold('request')
            version = ledger.read('request')['row_version']
            ledger.hold('request')  # row_version moves, the occurrence does not.
            self.assertEqual(ledger.read('request')['row_version'], version + 1)
            self.assertEqual([r['occurrence_id'] for r in self.outbox(ledger)], ['request#hold-1'])
            self.assertEqual(occurrence_id('request', 2), 'request#hold-2')


if __name__ == '__main__':
    unittest.main()
