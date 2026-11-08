"""ADR 0029: a departure submits a trip declaring the cargo; arrival and port events drive intake."""
from hashlib import sha256
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest import mock
from urllib.parse import unquote

from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.utils import timezone

from harbor.port_events import IntakeLedger
from harbor.rosy_http import RosyClient
from harbor.transport import TransportLedger, TripIntent
from harbor_web import executor, intake_loop, services

PRINCIPAL = 'harbor-service'


class Fleet:
    """Synthetic R: trip proposals a named operator admits elsewhere, and a port-event page."""
    def __init__(self):
        self.proposals, self.jobs, self.bodies, self.events = {}, {}, [], []

    def __call__(self, method, path, body):
        if path == '/api/fleet/session':
            return {'principal_id': PRINCIPAL, 'role': 'service'}
        if (method, path) == ('POST', '/api/fleet/trip-proposals'):
            self.bodies.append(body)
            key, step = body['request_key'], body['steps'][0]
            self.proposals.setdefault(key, dict(
                proposal_id='p-1', proposal_principal_id=PRINCIPAL, request_key=key, status='PROPOSED',
                request_digest=TripIntent(key, tuple(body['candidate_robot_ids']), step['to'], tuple(step['via']),
                                          body.get('object_ref', '')).digest,
                candidate_robot_ids=body['candidate_robot_ids'], steps=body['steps'], object_ref=body.get('object_ref'),
                dispatch_generation=None, approved_by=None, mission_id=None))
            return {'proposal': self.proposals[key]}
        if path.startswith('/api/fleet/trip-proposals?request_key='):
            return {'proposal': self.proposals[unquote(path.split('=', 1)[1])]}
        if path.startswith('/api/fleet/trip-jobs?request_key='):
            return {'job': self.jobs[unquote(path.split('=', 1)[1])]}
        if path.startswith('/api/fleet/port-events?'):
            events, self.events = self.events, []
            return {'events': events, 'cursor': 'c', 'complete': True}
        raise AssertionError(f'unexpected {method} {path}')

    def arrive(self, key):
        """Another named operator admitted, and the trip reached the intake point."""
        proposal = self.proposals[key]
        proposal.update(status='ADMITTED', dispatch_generation=1, approved_by='operator-2', mission_id='trip-1')
        self.jobs[key] = dict(mission_id='trip-1', request_key=key, request_digest=proposal['request_digest'],
                              robot_id=proposal['candidate_robot_ids'][0], status='GOAL_CONFIRMED', current_step_index=1,
                              steps=[dict(step_index=0, step=proposal['steps'][0], status='GOAL_CONFIRMED',
                                          action_id='a-1', attempt_id='a-1', reason=None)])


class IntakeLoopTests(TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = str(Path(tmp.name) / 'ledger.sqlite3')
        patcher = mock.patch.dict(os.environ, {'HARBOR_LEDGER_PATH': self.path,
                                               'HARBOR_TRIP_DESTINATION': 'intake-bench'})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.operator = User.objects.create_user('operator')
        self.operator.groups.add(Group.objects.get_or_create(name='operator')[0])
        self.field = User.objects.create_user('field')
        self.field.groups.add(Group.objects.get_or_create(name='field')[0])
        self.fleet = Fleet()
        client = RosyClient(transport=self.fleet)
        client.expected_principal, client.expected_role = PRINCIPAL, 'service'
        self.registry = SimpleNamespace(config={'path': self.path, 'origin': 'https://fleet-r.invalid'}, client=client)

    def depart(self):
        cargo = services.register_cargo(self.operator, code='C-1', name='Cargo', weight_kg='1', volume_m3='1',
                                        destination='yard')
        order = services.create_order(self.operator, cargo=cargo, kind='INBOUND', priority_group=1, sequence=1,
                                      tie_policy='FIXED')
        episode = services.assign_episode(self.operator, order_id=order.pk, vehicle_id='pinky-1', assigned_to=self.field)
        confirmation = services.confirm_episode(self.field, episode_id=episode.pk, expected_version=1, outcome='CLEAR',
                                                observed_at=timezone.now())
        return services.prepare_departure(self.operator, episode_id=episode.pk, confirmation_id=confirmation.pk)

    def test_departure_submits_a_trip_that_declares_the_cargo(self):
        departure = self.depart()
        key = intake_loop.trip_key(departure)
        row = TransportLedger(self.path).read(key)
        self.assertEqual(row['state'], 'PLANNED')  # the web request itself sends nothing to Fleet
        self.assertEqual(self.fleet.bodies, [])
        self.assertEqual(IntakeLedger(self.path).read('C-1')['state'], 'EXPECTED')

        intake_loop.tick(self.registry, now=timezone.now())
        self.assertEqual(self.fleet.bodies[0]['object_ref'], 'C-1')
        self.assertEqual(self.fleet.bodies[0]['candidate_robot_ids'], ['pinky-1'])
        self.assertEqual(TransportLedger(self.path).read(key)['state'], 'AWAITING_APPROVAL')

        self.fleet.arrive(key)
        self.fleet.events = [dict(event_id=sha256(b'cell|obs-1|received').hexdigest(),
                                  kind='container.received_at_intake', observation_ref='obs-1', object_ref='C-1',
                                  intake_point='intake-bench', identity='observed', observed_at='2026-11-08T03:00:00Z')]
        executor.tick(registry=self.registry, now=timezone.now())
        self.assertEqual(IntakeLedger(self.path).read('C-1')['state'], 'RECEIVED_AT_INTAKE')
        self.assertEqual(len(self.fleet.bodies), 1)  # proposed once, never re-sent

    def test_unconfigured_destination_keeps_the_departure_record_only(self):
        with mock.patch.dict(os.environ, {'HARBOR_TRIP_DESTINATION': ''}):
            departure = self.depart()
            self.assertIsNone(intake_loop.tick(self.registry, now=timezone.now()))
        with self.assertRaises(ValueError):
            TransportLedger(self.path).read(intake_loop.trip_key(departure))
