"""P2 evidence intake acceptance: submissions bind, everything else waits."""
from datetime import timedelta
from itertools import count
import json
import os
from pathlib import Path
import tempfile
from unittest.mock import patch

from django.core.exceptions import PermissionDenied, ValidationError
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from harbor.rosy_http import RosyClient
from harbor_web import commands, evidence, executor, services
from harbor_web.command_models import EvidenceSubmission
from harbor_web.tests import test_executor
from harbor_web.tests.test_executor import Fleet, PLACEMENT_SOURCE, RETREAT_SOURCE, PRINCIPAL


def observation_pair(mission, attempt, revision, cargo, location, arms, serial, *, age=0.0):
    when = (timezone.now() - timedelta(seconds=age)).isoformat()
    placement = dict(evidence_id=f'obs-{serial}-p', mission_id=mission, attempt_id=attempt,
                     revision=revision, source_id=PLACEMENT_SOURCE, observed_at=when,
                     cargo_id=cargo, location=location)
    retreat = dict(evidence_id=f'obs-{serial}-r', mission_id=mission, attempt_id=attempt,
                   revision=revision, source_id=RETREAT_SOURCE, observed_at=when,
                   arms=[dict(arm=arm, zone_id=f'zone-{arm}', claim_released=True,
                              pose_observed_at=when,
                              pose_revision=revision, outside_zone=True) for arm in arms])
    return placement, retreat


class SourceConfigurationTests(SimpleTestCase):
    def test_unset_environment_keeps_unconfigured_placeholders(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(evidence.configured_sources())
            self.assertEqual(evidence.ledger_sources(),
                             ((evidence.UNCONFIGURED_PLACEMENT,), (evidence.UNCONFIGURED_RETREAT,)))

    def test_configured_sources_must_be_disjoint_and_complete(self):
        with patch.dict(os.environ, {'HARBOR_PLACEMENT_SOURCES': 'installed-camera',
                                     'HARBOR_RETREAT_SOURCES': 'fleet-retreat'}):
            self.assertEqual(evidence.configured_sources(), (('installed-camera',), ('fleet-retreat',)))
        with patch.dict(os.environ, {'HARBOR_PLACEMENT_SOURCES': 'shared',
                                     'HARBOR_RETREAT_SOURCES': 'shared'}):
            with self.assertRaises(ValueError):
                evidence.configured_sources()
        with patch.dict(os.environ, {'HARBOR_PLACEMENT_SOURCES': 'installed-camera',
                                     'HARBOR_RETREAT_SOURCES': ''}):
            with self.assertRaises(ValueError):
                evidence.configured_sources()


class EvidenceIntakeTests(TestCase):
    cargo = test_executor.ExecutorTests.cargo
    condition = test_executor.ExecutorTests.condition
    order = test_executor.ExecutorTests.order

    def setUp(self):
        test_executor.ExecutorTests.setUp(self)
        # The P2 difference: sources come from installer configuration, not test patching.
        ledger_path, bindings_path = os.environ['HARBOR_LEDGER_PATH'], os.environ['HARBOR_BINDINGS_PATH']
        self.environment.stop()
        self.environment = patch.dict(os.environ, dict(HARBOR_LEDGER_PATH=ledger_path,
            HARBOR_FLEET_ORIGIN='http://127.0.0.1:9999', HARBOR_FLEET_TOKEN='test',
            HARBOR_FLEET_PRINCIPAL=PRINCIPAL, HARBOR_BINDINGS_PATH=bindings_path,
            HARBOR_WEB_ALLOW_SIMULATION='1', HARBOR_PLACEMENT_SOURCES=PLACEMENT_SOURCE,
            HARBOR_RETREAT_SOURCES=RETREAT_SOURCE))
        self.environment.start()
        self.addCleanup(self.environment.stop)
        commands.registry_close()
        registry = commands.acquire_registry()
        registry.client = RosyClient(transport=self.fleet)
        registry.client.expected_principal = PRINCIPAL
        self.registry = registry

    def plan(self):
        services.register_cargo(self.operator, code='C2', name='Rehandle', weight_kg='1', volume_m3='1', destination='yard')
        cargo = services.register_cargo(self.operator, code='C1', name='Outbound', weight_kg='1', volume_m3='1', destination='yard')
        observation = test_executor.fixtures.ServicesTests.observation(self, payload={
            'bays': [{'bay_id': 'bay', 'cargo': ['C1', 'C2'], 'capacity': 2},
                     {'bay_id': 'temp', 'cargo': [], 'capacity': 2},
                     {'bay_id': 'out', 'cargo': [], 'capacity': 1}], 'holdings': []},
            provenance='SYNTHETIC')
        order = services.create_order(self.operator, cargo=cargo, kind='OUTBOUND', priority_group=1,
                                      sequence=1, tie_policy='FIXED')
        return services.make_plan(self.operator, order_id=order.pk, expected_version=1, observation_id=observation.pk)

    def confirmed_command(self):
        plan = self.plan()
        command = commands.prepare_command(self.operator, plan_id=plan.pk, expected_version=1)
        return commands.submit_command(self.operator, command_id=command.pk, expected_version=command.version)

    def test_configured_sources_reach_the_ledger(self):
        self.assertEqual(self.registry.ledger.placement_sources, (PLACEMENT_SOURCE,))
        self.assertEqual(self.registry.ledger.retreat_sources, (RETREAT_SOURCE,))

    def test_submit_requires_field_role_registered_source_and_valid_payload(self):
        command = self.confirmed_command()
        placement, retreat = observation_pair('physical-1', 'attempt-1', 1, 'C2', 'temp', ('A',), 0)
        with self.assertRaises(PermissionDenied):
            evidence.submit(self.operator, command_id=command.pk, placement=placement, retreat=retreat)
        unregistered = dict(placement, source_id='stranger')
        with self.assertRaises(ValidationError):
            evidence.submit(self.field, command_id=command.pk, placement=unregistered, retreat=retreat)
        malformed = dict(placement)
        del malformed['observed_at']
        with self.assertRaises(ValidationError):
            evidence.submit(self.field, command_id=command.pk, placement=malformed, retreat=retreat)
        record = evidence.submit(self.field, command_id=command.pk, placement=placement, retreat=retreat)
        self.assertEqual(record.placement, placement)
        self.assertEqual(EvidenceSubmission.objects.filter(command=command).count(), 1)

    def test_three_step_business_completes_from_submissions_only(self):
        command = self.confirmed_command()
        key = commands._key(command)
        self.fleet.status = 'GOAL_CONFIRMED'
        provider = evidence.intake_provider()
        serial = count()
        payload = json.loads(self.registry.ledger.read(key)['payload'])
        for _ in range(30):
            row = self.registry.ledger.read(key)
            if row['state'] == 'COMPLETED':
                break
            if row['state'] == 'WAITING' and row['goal_confirmed']:
                move = payload['moves'][row['step_index']]
                evidence.submit(self.field, command_id=command.pk,
                    **dict(zip(('placement', 'retreat'), observation_pair(row['mission_id'], row['attempt_id'],
                    payload['revision'], move['cargo_id'], move['destination'],
                    tuple(payload['move_arms'][row['step_index']]), next(serial)))))
            elif row['state'] == 'AWAITING_FINAL_EVIDENCE':
                arms = tuple(sorted({arm for step in payload['move_arms'] for arm in step}))
                evidence.submit(self.field, command_id=command.pk,
                    **dict(zip(('placement', 'retreat'), observation_pair(row['outbound_mission_id'],
                    row['outbound_attempt_id'], payload['revision'], payload['outbound_id'],
                    payload['outbound_stand'], arms, next(serial)))))
            executor.tick(registry=self.registry, evidence=provider, now=timezone.now())
        command.refresh_from_db()
        self.assertEqual(command.state, 'COMPLETED')
        self.assertEqual(sorted(self.fleet.posted_keys()), sorted([f'{key}:0', f'{key}:1', f'{key}:2']))

    def test_stale_or_foreign_submissions_never_confirm_a_stage(self):
        command = self.confirmed_command()
        key = commands._key(command)
        self.fleet.status = 'GOAL_CONFIRMED'
        provider = evidence.intake_provider()
        payload = json.loads(self.registry.ledger.read(key)['payload'])
        move = payload['moves'][0]
        for _ in range(10):
            executor.tick(registry=self.registry, evidence=provider, now=timezone.now())
            row = self.registry.ledger.read(key)
            if row['state'] == 'WAITING' and row['goal_confirmed']:
                break
        self.assertTrue(row['goal_confirmed'])
        stale = observation_pair('physical-1', 'attempt-1', payload['revision'], move['cargo_id'],
                                 move['destination'], ('A',), 0, age=30)
        evidence.submit(self.field, command_id=command.pk, placement=stale[0], retreat=stale[1])
        foreign = observation_pair('physical-9', 'attempt-9', payload['revision'], move['cargo_id'],
                                   move['destination'], ('A',), 1)
        evidence.submit(self.field, command_id=command.pk, placement=foreign[0], retreat=foreign[1])
        executor.tick(registry=self.registry, evidence=provider, now=timezone.now())
        row = self.registry.ledger.read(key)
        self.assertEqual((row['state'], row['goal_confirmed'], row['step_index']), ('WAITING', 1, 0))
