"""ADR0022: production execution opens only when every readiness condition holds in a fresh stored check."""
import io
import os
from datetime import timedelta
from unittest.mock import patch
from urllib.error import HTTPError

from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied
from django.test import TestCase, override_settings

from harbor_web import commands, readiness, fleet_connection as bridge
from harbor_web.models import Audit, Command, EvidenceProducerSetting
from harbor_web.tests import test_rearrangement_simulation as sim

ALL_MET = dict(arm='MET', evidence='MET', recheck='MET', proposal='UNMET', arm_live='MET', auto_producer='MET')


class ReadinessGateTests(TestCase):
    setUp_base, plan, install = (sim.RearrangementSimulationTests.setUp, sim.RearrangementSimulationTests.plan,
                                 sim.RearrangementSimulationTests.install)

    def setUp(self):
        self.setUp_base()
        self.install(sim.TEMPLATE)
        self.target = self.plan()
        self.target.observation.provenance = 'MEASURED'  # in memory: stands for a future authenticated provenance value
        mode = override_settings(MODE='live')
        mode.enable()
        self.addCleanup(mode.disable)

    def check(self, session='MATCHED', **conditions):
        detail = dict(status='VERIFIED', session=session, arm='READABLE', reason='READ_API_VERIFIED',
                      conditions=dict(ALL_MET, **conditions), fingerprint=bridge.fingerprint(bridge.connection_settings()))
        Audit.objects.create(actor=self.admin, action='fleet.check', object_type='FleetConnection', object_id='1', detail=detail)

    def reason(self):
        return commands.capability(self.target)['reason']

    def test_all_conditions_fresh_open(self):
        self.check()
        self.assertEqual(self.reason(), '')

    def test_platform_arm_signal_false_blocks_even_if_everything_else_is_true(self):
        for state in ('UNMET', 'UNKNOWN'):
            self.check(arm_live=state, auto_producer='MET')
            self.assertEqual(self.reason(), 'READINESS_ARM_EXECUTION_UNANNOUNCED')

    def test_stale_or_missing_check_blocks(self):
        self.assertEqual(self.reason(), 'READINESS_CHECK_REQUIRED')
        self.check()
        with patch('harbor_web.readiness.MAX_AGE', timedelta(0)):  # audit rows are append-only, so shrink the window instead
            self.assertEqual(self.reason(), 'READINESS_CHECK_STALE')

    def test_empty_or_unmatching_binding_blocks(self):
        self.check()
        self.install()
        self.assertEqual(readiness.first_unmet(self.target), 'READINESS_BINDING_REQUIRED')
        self.assertNotEqual(self.reason(), '')  # an earlier spatial check may name it first, but it stays shut

    def test_each_unmet_condition_blocks_with_its_own_reason(self):
        for session, conditions, expected in [
                ('MISMATCH', {}, 'READINESS_SESSION_UNMET'), ('MATCHED', dict(arm='UNMET'), 'READINESS_ARM_UNMET'),
                ('MATCHED', dict(evidence='UNMET'), 'READINESS_EVIDENCE_UNMET'),
                ('MATCHED', dict(recheck='UNKNOWN'), 'READINESS_EVIDENCE_UNMET')]:
            self.check(session=session, **conditions)
            self.assertEqual(self.reason(), expected)

    def test_proposal_route_is_display_only(self):
        self.check(proposal='UNMET')
        self.assertEqual(self.reason(), '')

    def test_manual_observation_never_opens_production(self):
        self.check()
        self.target.observation.provenance = 'DECLARED'
        self.assertEqual(self.reason(), 'READINESS_OBSERVATION_UNAUTHENTICATED')

    def test_declared_observation_runs_only_under_human_confirmation(self):
        self.check()
        self.target.observation.provenance = 'DECLARED'
        bridge.set_human_confirmation(self.admin, enabled=True, expected_version=0)
        self.assertEqual(self.reason(), '')  # user decision 2026-11-04: person-declared + person-confirmed
        self.target.observation.provenance = 'SYNTHETIC'
        self.assertEqual(self.reason(), 'READINESS_OBSERVATION_UNAUTHENTICATED')  # never in production

    def test_human_confirmation_needs_admin_switch_and_a_field_user(self):
        self.check(auto_producer='UNMET')
        self.field.groups.clear()
        self.assertEqual(self.reason(), 'READINESS_EVIDENCE_PRODUCER_REQUIRED')
        row = bridge.set_human_confirmation(self.admin, enabled=True, expected_version=0)
        self.assertTrue(Audit.objects.filter(action='evidence_producer.configure').exists())
        self.assertEqual(self.reason(), 'READINESS_EVIDENCE_PRODUCER_REQUIRED')  # no field user yet
        self.field.groups.add(Group.objects.get(name='field'))
        self.assertEqual(self.reason(), '')
        with self.assertRaises(PermissionDenied):
            bridge.set_human_confirmation(self.operator, enabled=False, expected_version=row.version)
        self.assertTrue(EvidenceProducerSetting.objects.get().human_confirmation)

    def test_allow_simulation_flag_alone_does_not_open_production(self):
        self.check(arm_live='UNMET')
        with patch.dict(os.environ, HARBOR_WEB_ALLOW_SIMULATION='1'):
            self.assertEqual(self.reason(), 'READINESS_ARM_EXECUTION_UNANNOUNCED')

    def test_probe_only_gets_and_creates_nothing(self):
        with patch.dict(os.environ, HARBOR_FLEET_ORIGIN='https://fleet.example', HARBOR_FLEET_PRINCIPAL='harbor-service',
                        HARBOR_FLEET_ALLOWED_ORIGINS='https://fleet.example', HARBOR_FLEET_TOKEN='t'):
            calls = []

            def fake(method, path, body):
                calls.append((method, body))
                if path.endswith('/session'):
                    return {'principal_id': 'harbor-service', 'role': 'service'}
                raise HTTPError(path, 404, 'x', {}, io.BytesIO(b'{"detail":"Not Found"}'))

            with patch('harbor.rosy_http.RosyClient._http', side_effect=fake):
                result = bridge.check_connection(self.admin, expected_version=0)
            self.assertGreater(len(calls), 4)
            self.assertTrue(all(m == 'GET' and b is None for m, b in calls))
            self.assertEqual(Command.objects.count(), 0)
            self.assertEqual(result['conditions']['arm_live'], 'UNMET')
            self.assertEqual(result['conditions']['evidence'], 'UNMET')

    def test_platform_signal_parsing_is_strict(self):
        good = dict(profile='production', arm_execution='physical',
                    observation_producers=[dict(authenticated=True)], evidence_producers=[dict(authenticated=True)])
        for value, expected in [(good, ('MET', 'MET')), (dict(good, profile='simulation'), ('UNMET', 'UNMET')),
                                (dict(good, profile='device_trial'), ('MET', 'MET')),
                                (dict(good, profile='device_trial', arm_execution='closed'), ('UNMET', 'MET')),
                                (dict(good, arm_execution='simulated'), ('UNMET', 'MET')),
                                (dict(good, evidence_producers=[dict(authenticated=False)]), ('MET', 'UNMET')),
                                (dict(good, observation_producers=[]), ('MET', 'UNMET'))]:
            client = type('C', (), {'execution_profile': lambda self, v=value: v})()
            self.assertEqual(bridge._platform_signal(client), expected)

    def test_settings_page_shows_table_and_admin_toggles_producer(self):
        self.check()
        self.client.force_login(self.admin)
        with patch('harbor.rosy_http.RosyClient._http', side_effect=AssertionError('GET contacted Fleet')):
            page = self.client.get('/settings/integration/')
        self.assertContains(page, '운영 실행 조건 점검표')
        self.assertContains(page, '미충족')
        self.assertEqual(self.client.post('/settings/integration/', {'action': 'producer', 'expected_version': 0, 'enabled': '1'}).status_code, 302)
        self.assertTrue(EvidenceProducerSetting.objects.get().human_confirmation)
