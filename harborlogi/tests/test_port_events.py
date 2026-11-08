"""ROSY port events drive per-cargo intake states forward only (ADR 0029)."""
from datetime import timedelta
from hashlib import sha256
from pathlib import Path
import tempfile
import unittest
from urllib.error import HTTPError

from harbor.departure import DepartureLedger
from harbor.port_events import IntakeLedger, parse_event
from harbor.rosy_http import RosyClient
from test_policy import NOW


def event(kind, observation, **fields):
    value = dict(event_id=sha256(f'cell|{observation}|{kind}'.encode()).hexdigest(), kind=kind,
                 observation_ref=observation, object_ref=None, intake_point=None, slot_ref=None,
                 vehicle_id=None, reason=None, identity=None, observed_at='2026-11-08T03:00:00Z')
    value.update(fields)
    return value


def received(cargo, observation='obs-1', identity='observed'):
    return event('container.received_at_intake', observation, object_ref=cargo, intake_point='intake',
                 identity=identity)


def stored(cargo, observation='obs-2'):
    return event('container.stored', observation, object_ref=cargo, slot_ref='yard-r1-c2-l1', identity='observed')


def failed(reason, cargo=None, observation='obs-3'):
    return event('container.handling_failed', observation, object_ref=cargo, reason=reason)


class R:
    """Synthetic R page source; records the cursor Harbor sends back."""
    def __init__(self, *pages):
        self.pages, self.afters = list(pages), []

    def __call__(self, method, path, body):
        assert method == 'GET' and path.startswith('/api/fleet/port-events?'), path
        self.afters.append(path)
        return self.pages.pop(0)


class PortEventTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / 'harbor.sqlite3'
        self.ledger = IntakeLedger(self.path, lost_after=timedelta(minutes=10))
        self.ledger.expect('C-1', now=NOW)
        self.ledger.arrive('C-1', now=NOW)

    def poll(self, *events, complete=True, cursor='opaque-1', now=NOW):
        fleet = R(dict(events=list(events), cursor=cursor, complete=complete))
        return self.ledger.poll(RosyClient(transport=fleet), connection='fleet-r', now=now), fleet

    def state(self, cargo='C-1'):
        row = self.ledger.read(cargo)
        return row['state'], row['hold_reason']

    def test_events_move_a_declared_cargo_to_completion(self):
        self.poll(received('C-1'))
        self.assertEqual(self.state(), ('RECEIVED_AT_INTAKE', None))
        self.ledger.storing('C-1', now=NOW)
        self.poll(stored('C-1'))
        row = self.ledger.read('C-1')
        self.assertEqual((row['state'], row['identity'], row['slot_ref']), ('COMPLETED', 'observed', 'yard-r1-c2-l1'))

    def test_replayed_event_id_is_consumed_once(self):
        first, _ = self.poll(received('C-1'))
        version = self.ledger.read('C-1')['version']
        again, _ = self.poll(received('C-1'))
        self.assertEqual(list(first['outcomes'].values()), ['APPLIED'])
        self.assertEqual(list(again['outcomes'].values()), ['REPLAYED'])
        self.assertEqual(self.ledger.read('C-1')['version'], version)

    def test_mismatch_holds_the_cargo(self):
        self.poll(received('C-1'), failed('MISMATCH', 'C-1'))
        self.assertEqual(self.state(), ('HOLD', 'MISMATCH'))
        self.poll(stored('C-1'))  # a held cargo is not completed by a later event
        self.assertEqual(self.state(), ('HOLD', 'MISMATCH'))

    def test_undeclared_or_unnamed_cargo_is_held_as_unknown(self):
        result, _ = self.poll(received('C-9'), failed('UNREADABLE', None, 'obs-9'))
        self.assertEqual(set(result['outcomes'].values()), {'HOLD_UNKNOWN'})
        self.assertEqual([e['object_ref'] for e in self.ledger.unknown_events()], ['C-9', None])
        with self.assertRaises(ValueError):
            self.ledger.read('C-9')  # an event never invents a manifest entry

    def test_late_event_cannot_move_state_backward(self):
        self.poll(received('C-1'), stored('C-1'))
        result, _ = self.poll(received('C-1', 'obs-late'))
        self.assertEqual(list(result['outcomes'].values()), ['IGNORED'])
        self.assertEqual(self.state(), ('COMPLETED', None))
        self.assertFalse(self.ledger.arrive('C-1', now=NOW))

    def test_incomplete_page_waits_then_times_out_as_lost(self):
        result, _ = self.poll(complete=False)
        self.assertEqual((result['complete'], result['lost']), (False, []))
        self.assertEqual(self.state(), ('TRIP_ARRIVED', None))
        result, _ = self.poll(complete=False, cursor='opaque-2', now=NOW + timedelta(minutes=11))
        self.assertEqual(result['lost'], ['C-1'])
        self.assertEqual(self.state(), ('HOLD', 'LOST'))

    def test_rosy_lost_report_holds_lost(self):
        self.poll(failed('LOST', 'C-1'))
        self.assertEqual(self.state(), ('HOLD', 'LOST'))

    def test_damaged_report_holds_for_a_person(self):
        self.poll(received('C-1'), failed('DAMAGED', 'C-1'))
        self.assertEqual(self.state(), ('HOLD', 'DAMAGED'))

    def test_silent_endpoint_never_ages_cargo_into_lost(self):
        def missing(method, path, body):
            raise HTTPError(path, 404, 'Not Found', {}, None)
        later = NOW + timedelta(hours=2)
        with self.assertRaises(HTTPError):
            self.ledger.poll(RosyClient(transport=missing), connection='fleet-r', now=later)
        self.assertEqual(self.ledger.expire(now=later, connection='fleet-r'), [])
        self.assertEqual(self.state(), ('TRIP_ARRIVED', None))
        # Once R has answered on this connection, silence counts toward LOST.
        result, _ = self.poll(complete=False, now=later)
        self.assertEqual(result['lost'], ['C-1'])
        self.assertEqual(self.state(), ('HOLD', 'LOST'))

    def test_unreadable_waits_for_a_named_person(self):
        self.poll(failed('UNREADABLE', 'C-1'))
        row = self.ledger.read('C-1')
        self.assertEqual((row['state'], row['attention']), ('TRIP_ARRIVED', 'UNREADABLE'))
        self.assertTrue(self.ledger.attest_received('C-1', attested_by='user-7', now=NOW))
        row = self.ledger.read('C-1')
        self.assertEqual((row['state'], row['identity'], row['attested_by'], row['attention']),
                         ('RECEIVED_AT_INTAKE', 'attested', 'user-7', None))
        self.poll(received('C-1', 'obs-after'))  # a later reading does not overwrite the attestation
        self.assertEqual(self.ledger.read('C-1')['identity'], 'attested')

    def test_cursor_is_opaque_and_kept_per_connection(self):
        _, fleet = self.poll(cursor='cell-a:3|cell-b:7&x=1')
        _, again = self.poll(cursor='next')
        self.assertEqual(fleet.afters, ['/api/fleet/port-events?limit=100'])
        self.assertEqual(again.afters, ['/api/fleet/port-events?after=cell-a%3A3%7Ccell-b%3A7%26x%3D1&limit=100'])

    def test_invalid_event_refuses_the_whole_page_and_keeps_the_cursor(self):
        self.poll(cursor='kept')
        bad = [dict(received('C-1'), marker_id=7), dict(received('C-1'), kind='container.teleported'),
               dict(received('C-1'), slot_ref='yard-r1-c1-l1'), dict(received('C-1'), identity='inferred'),
               dict(received('C-1'), event_id='x' * 64), dict(received('C-1'), observed_at='2026-11-08T12:00:00+09:00'),
               dict(stored('C-1'), identity=None)]
        for value in bad:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.poll(value, cursor='moved')
        _, fleet = self.poll(cursor='after')
        self.assertIn('after=kept', fleet.afters[0])
        self.assertEqual(self.state(), ('TRIP_ARRIVED', None))

    def test_release_permission_allows_one_departure_after_arrival(self):
        permit = event('vehicle.release_permitted', 'obs-r', vehicle_id='pinky-1')
        self.poll(permit, event('vehicle.release_permitted', 'obs-r', vehicle_id='pinky-1'))
        departures = DepartureLedger(self.path)
        arrived = parse_event(permit)['observed_at'] - timedelta(seconds=1)
        departures.authorize('pinky-1', since=arrived)
        with self.assertRaises(ValueError):
            departures.authorize('pinky-1', since=arrived)  # spent once, replay added nothing
        self.poll(event('vehicle.release_permitted', 'obs-r2', vehicle_id='pinky-1'))
        with self.assertRaises(ValueError):  # a permission older than the current arrival does not count
            departures.authorize('pinky-1', since=arrived + timedelta(hours=1))


if __name__ == '__main__':
    unittest.main()
