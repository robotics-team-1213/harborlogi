"""Named field-person confirmation as business evidence (ADR 0023)."""
import json

from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.utils import timezone

from harbor.workflow import HumanArmRetreat, MoveEvidence, PlacementEvidence, RetreatEvidence
from harbor_web import commands, executor, human_evidence
from harbor_web.command_models import HumanAttestation
from harbor_web.models import Audit
from harbor_web.tests import test_evidence, test_executor


class HumanEvidenceTests(TestCase):
    base = test_evidence.EvidenceIntakeTests
    cargo, condition, order = base.cargo, base.condition, base.order
    plan, confirmed_command = base.plan, base.confirmed_command

    def setUp(self):
        self.base.setUp(self)
        self.producer(True)
        self.viewer = User.objects.create_user('viewer')
        self.viewer.groups.add(Group.objects.get_or_create(name='viewer')[0])

    @staticmethod
    def producer(on):
        from harbor_web.models import EvidenceProducerSetting
        EvidenceProducerSetting.objects.update_or_create(pk=1, defaults=dict(human_confirmation=on))

    def waiting(self, status='GOAL_CONFIRMED', command=None):
        command = command or self.confirmed_command()
        self.fleet.status = status
        for _ in range(6):
            executor.tick(registry=self.registry, now=timezone.now())
            row = self.registry.ledger.read(commands._key(command))
            if row['state'] == 'WAITING' and row['goal_confirmed']:
                break
        command.refresh_from_db()
        return command, row

    def post(self, user, command, row, **changes):
        final = row['state'] == 'AWAITING_FINAL_EVIDENCE'
        payload = json.loads(row['payload'])
        arms = sorted({a for step in payload['move_arms'] for a in step}) if final else payload['move_arms'][row['step_index']]
        data = dict(action='attest', expected_version=command.version, step_index=row['step_index'],
                    mission_id=row['outbound_mission_id'] if final else row['mission_id'],
                    attempt_id=row['outbound_attempt_id'] if final else row['attempt_id'],
                    phase='final' if final else 'step', cargo_seen='on', arms=arms, note='확인', photo_ref='')
        data.update(changes)
        self.client.force_login(user)
        return self.client.post(f'/work/{command.pk}/', data)

    def test_form_shows_only_while_the_ledger_waits_and_only_to_named_roles(self):
        command, _ = self.waiting(status='PROPOSED')
        self.client.force_login(self.operator)
        self.assertNotContains(self.client.get(f'/work/{command.pk}/'), 'name="action" value="attest"')
        self.client.force_login(self.field)  # field staff only open a command that waits for them
        self.assertEqual(self.client.get(f'/work/{command.pk}/').status_code, 403)
        command, row = self.waiting(command=command)
        page = self.client.get(f'/work/{command.pk}/')
        self.assertContains(page, 'name="action" value="attest"')
        self.assertContains(page, 'csrfmiddlewaretoken')
        self.client.force_login(self.viewer)
        self.assertNotContains(self.client.get(f'/work/{command.pk}/'), 'name="action" value="attest"')
        self.producer(False)
        self.client.force_login(self.operator)
        self.assertNotContains(self.client.get(f'/work/{command.pk}/'), 'name="action" value="attest"')
        self.producer(True)

    def test_field_sees_a_command_only_to_confirm_and_cannot_use_other_actions(self):
        command, _ = self.waiting(status='PROPOSED')
        self.client.force_login(self.field)
        self.assertEqual(self.client.get(f'/work/{command.pk}/').status_code, 403)
        command, row = self.waiting(command=command)
        self.assertEqual(self.client.get(f'/work/{command.pk}/').status_code, 200)
        for action in ('submit', 'cancel', 'reconcile'):
            response = self.client.post(f'/work/{command.pk}/', dict(action=action, expected_version=command.version))
            self.assertEqual(response.status_code, 403, action)

    def test_provider_stops_offering_a_record_before_the_ledger_window_ends(self):
        from datetime import timedelta
        from unittest.mock import patch
        command, row = self.waiting()
        self.assertEqual(self.post(self.field, command, row).status_code, 302)
        late = timezone.now() + human_evidence.FRESHNESS - human_evidence.LEDGER_MARGIN + timedelta(seconds=1)
        with patch('django.utils.timezone.now', return_value=late):
            self.assertIsNone(human_evidence.human_provider()(command, row, json.loads(row['payload']), 'step'))

    def test_viewer_is_forbidden_and_nothing_is_stored(self):
        command, row = self.waiting()
        self.assertEqual(self.post(self.viewer, command, row).status_code, 403)
        self.assertEqual(HumanAttestation.objects.count(), 0)

    def test_other_attempt_stale_version_and_missing_arm_are_rejected(self):
        command, row = self.waiting()
        self.assertEqual(self.post(self.field, command, row, attempt_id='attempt-9').status_code, 409)
        self.assertEqual(self.post(self.field, command, row, mission_id='physical-9').status_code, 409)
        self.assertEqual(self.post(self.field, command, row, expected_version=command.version + 1).status_code, 409)
        self.assertEqual(self.post(self.field, command, row, arms=[]).status_code, 200)  # form error, no record
        self.assertEqual(self.post(self.field, command, row, cargo_seen='').status_code, 200)
        self.assertEqual(HumanAttestation.objects.count(), 0)

    def test_a_confirmation_is_consumed_once_and_cannot_be_recorded_twice(self):
        command, row = self.waiting()
        self.assertEqual(self.post(self.field, command, row).status_code, 302)
        self.assertEqual(self.post(self.field, command, row).status_code, 409)  # already recorded for this stage
        key = commands._key(command)
        executor.tick(registry=self.registry, evidence=human_evidence.human_provider(), now=timezone.now())
        after = self.registry.ledger.read(key)
        self.assertEqual(after['step_index'], 1)
        record = HumanAttestation.objects.get()
        payload = json.loads(row['payload'])
        # The consumed record never produces evidence again, not even for the same stage.
        self.assertIsNone(human_evidence.human_provider()(command, row, payload, 'step'))
        with self.assertRaises(ValueError):
            self.registry.ledger.confirm(key, MoveEvidence(
                PlacementEvidence(f'human-{record.pk}:placement', row['mission_id'], row['attempt_id'], payload['revision'],
                                  human_evidence.HUMAN_PLACEMENT_SOURCE, timezone.now(), record.cargo_id, record.location,
                                  attested_by=f'user-{self.field.pk}'),
                RetreatEvidence(f'human-{record.pk}:retreat', row['mission_id'], row['attempt_id'], payload['revision'],
                                human_evidence.HUMAN_RETREAT_SOURCE, timezone.now(),
                                (HumanArmRetreat('A', f'user-{self.field.pk}', True),))), now=timezone.now())

    def test_switching_the_producer_off_blocks_the_form_and_consumption(self):
        command, row = self.waiting()
        self.producer(False)
        self.assertEqual(self.post(self.field, command, row).status_code, 409)
        self.producer(True)
        self.assertEqual(self.post(self.field, command, row).status_code, 302)
        self.producer(False)
        executor.tick(registry=self.registry, evidence=human_evidence.human_provider(), now=timezone.now())
        self.producer(True)
        self.assertEqual(self.registry.ledger.read(commands._key(command))['step_index'], 0)

    def test_human_verdict_cannot_pass_as_an_automatic_source(self):
        command, row = self.waiting()
        payload = json.loads(row['payload'])
        now = timezone.now()
        move = payload['moves'][0]
        forged = MoveEvidence(
            PlacementEvidence('f-p', row['mission_id'], row['attempt_id'], payload['revision'],
                              test_executor.PLACEMENT_SOURCE, now, move['cargo_id'], move['destination'], attested_by='user-1'),
            RetreatEvidence('f-r', row['mission_id'], row['attempt_id'], payload['revision'],
                            test_executor.RETREAT_SOURCE, now, (HumanArmRetreat('A', 'user-1', True),)))
        with self.assertRaises(ValueError):
            self.registry.ledger.confirm(commands._key(command), forged, now=timezone.now())

    def test_full_business_completes_from_named_confirmations_and_shows_as_human(self):
        command, row = self.waiting()
        key = commands._key(command)
        provider = human_evidence.human_provider()
        for _ in range(30):
            row = self.registry.ledger.read(key)
            if row['state'] == 'COMPLETED':
                break
            command.refresh_from_db()
            if (row['state'] == 'WAITING' and row['goal_confirmed']) or row['state'] == 'AWAITING_FINAL_EVIDENCE':
                user = self.field if row['state'] == 'WAITING' else self.operator
                self.assertEqual(self.post(user, command, row).status_code, 302)
            executor.tick(registry=self.registry, evidence=provider, now=timezone.now())
        command.refresh_from_db()
        self.assertEqual(command.state, 'COMPLETED')
        kinds = list(HumanAttestation.objects.order_by('pk').values_list('phase', flat=True))
        self.assertEqual(kinds, ['step', 'step', 'step', 'final'])
        self.assertEqual(Audit.objects.filter(action='human_attestation.record').count(), 4)
        stored = json.loads(self.registry.ledger._connect().execute(
            "SELECT payload FROM evidence WHERE source_id=? LIMIT 1", (human_evidence.HUMAN_RETREAT_SOURCE,)).fetchone()[0])
        self.assertEqual(stored['arms'][0]['outside_zone_seen'], True)
        self.assertNotIn('joint_positions', stored['arms'][0])
        self.client.force_login(self.operator)
        home = self.client.get('/')
        self.assertEqual(home.context['operations']['actual']['human_attested'], 1)
        self.assertContains(home, '사람 확인 근거 1건')
        timeline = self.client.get(f'/requests/{command.order_id}/timeline/')
        self.assertContains(timeline, '사람 확인')
        self.assertContains(timeline, '측정값 아님')
