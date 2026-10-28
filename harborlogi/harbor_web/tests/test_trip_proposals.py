"""D-908 executor wiring: one neutral trip proposal per fresh departure intent; never admit."""
import os
from pathlib import Path
import tempfile
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from harbor.rosy_http import RosyClient
from harbor.transport import TransportLedger
from harbor_web import commands, executor, services
from harbor_web.models import DepartureIntent
from harbor_web.tests import test_services as fixtures
from harbor_web.trip_proposals import trip_key
from tests.test_trip_proposal_transport import PRINCIPAL, ProposalFleet


class TripProposalWiringTests(TestCase):
    cargo = fixtures.ServicesTests.cargo
    condition = fixtures.ServicesTests.condition
    order = fixtures.ServicesTests.order
    episode = fixtures.ServicesTests.episode

    def setUp(self):
        fixtures.ServicesTests.setUp(self)
        from django.conf import settings
        from harbor_web.runtime_models import DeploymentState
        DeploymentState.objects.get_or_create(pk=1, defaults={'mode': settings.MODE})
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.addCleanup(commands.registry_close)
        root = Path(directory.name)
        (root / 'bindings.json').write_text('[]')
        environment = patch.dict(os.environ, dict(HARBOR_LEDGER_PATH=str(root / 'ledger.db'),
            HARBOR_FLEET_ORIGIN='http://127.0.0.1:9999', HARBOR_FLEET_TOKEN='test', HARBOR_FLEET_PRINCIPAL=PRINCIPAL,
            HARBOR_BINDINGS_PATH=str(root / 'bindings.json'), HARBOR_WEB_ALLOW_SIMULATION='1',
            HARBOR_TRIP_DESTINATION='yard-gate'))
        environment.start()
        self.addCleanup(environment.stop)
        self.fleet = ProposalFleet()
        self.registry = commands.acquire_registry()
        self.registry.client = RosyClient(transport=self.fleet)
        self.registry.client.expected_principal = PRINCIPAL
        self.registry.client.expected_role = 'service'

    def departure(self, observed_at=None):
        episode = self.episode()
        confirmation = services.confirm_episode(self.field, episode_id=episode.pk, expected_version=1, outcome='CLEAR',
                                                observed_at=observed_at or timezone.now())
        return services.prepare_departure(self.operator, episode_id=episode.pk, confirmation_id=confirmation.pk)

    def tick(self, now=None):
        return executor.tick(registry=self.registry, now=now or timezone.now())['results']

    def test_one_proposal_per_departure_and_idempotent_retick(self):
        departure = self.departure()
        key = trip_key(departure)
        self.assertEqual(self.tick()['trip:' + key], 'PROPOSED')
        self.assertEqual(self.tick()['trip:' + key], 'PROPOSED')
        self.assertEqual(len(self.fleet.posted()), 1)
        self.assertEqual(self.fleet.proposals[key]['candidate_robot_ids'], ['vehicle-1'])

    def test_operator_admission_then_arrival_fact_without_harbor_admitting(self):
        departure = self.departure()
        key = trip_key(departure)
        self.tick()
        self.fleet.admit(key)
        self.assertEqual(self.tick()['trip:' + key], 'ADMITTED')
        self.fleet.job_status = 'GOAL_CONFIRMED'
        self.fleet.jobs[key].update(status='GOAL_CONFIRMED', current_step_index=1)
        self.fleet.jobs[key]['steps'][0].update(status='GOAL_CONFIRMED', action_id='a', attempt_id='a')
        self.assertEqual(self.tick()['trip:' + key], 'GOAL_CONFIRMED')
        self.assertFalse(any('admit' in path for _, path in self.fleet.calls))
        self.assertEqual(len(self.fleet.posted()), 1)

    def test_lost_response_reconciles_by_the_same_key(self):
        departure = self.departure()
        key = trip_key(departure)
        self.fleet.lose_response = True
        self.assertTrue(self.tick()['trip:' + key].startswith('HOLD'))
        self.fleet.lose_response = False
        executor._failures.clear()
        self.assertEqual(self.tick()['trip:' + key], 'PROPOSED')
        self.assertEqual(len(self.fleet.posted()), 1)
        self.assertEqual(list(self.fleet.proposals), [key])

    def test_principal_mismatch_holds_without_any_post(self):
        departure = self.departure()
        self.fleet.principal = 'someone-else'
        self.assertTrue(self.tick()['trip:' + trip_key(departure)].startswith('HOLD'))
        self.assertEqual(self.fleet.posted(), [])

    def test_no_proposal_without_fresh_s_clearance(self):
        self.departure()
        self.assertEqual(self.tick(timezone.now() + timedelta(minutes=6)), {})
        self.assertEqual(self.fleet.calls, [])
        self.assertEqual(TransportLedger(self.registry.config['path']).open_proposal_keys(), [])

    def test_unconfigured_destination_does_nothing(self):
        self.departure()
        with patch.dict(os.environ, {'HARBOR_TRIP_DESTINATION': ''}):
            self.assertEqual(self.tick(), {})
        self.assertEqual(self.fleet.calls, [])

    def test_field_page_shows_proposal_state(self):
        departure = self.departure()
        self.tick()
        self.client.force_login(self.operator)
        self.assertContains(self.client.get(f'/field/{departure.episode_id}/'), '운영자 승인 대기')
        self.fleet.admit(trip_key(departure))
        self.tick()
        self.assertContains(self.client.get(f'/field/{departure.episode_id}/'), '운영자 승인됨')
