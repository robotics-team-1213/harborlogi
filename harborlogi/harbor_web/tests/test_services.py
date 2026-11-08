from django.test import TestCase
from django.contrib.auth.models import User, Group
from django.core.exceptions import ValidationError, PermissionDenied
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.test import override_settings
from datetime import timedelta
from harbor_web import services
from harbor_web.models import Cargo, Condition, Observation, Audit, DepartureIntent


class ServicesTests(TestCase):
    def test_request_history_preserves_original_and_changed_snapshots(self):
        order = self.order(note='Original instruction')
        observation = self.observation()
        plan = services.make_plan(self.operator, order_id=order.pk, expected_version=1, observation_id=observation.pk)
        services.update_order(self.operator, order_id=order.pk, expected_version=1,
            priority_group=2, sequence=3, tie_policy='ANY', note='Revised instruction')
        created = Audit.objects.get(action='order.create', object_id=str(order.pk)).detail
        edited = Audit.objects.get(action='order.update', object_id=str(order.pk)).detail
        self.assertEqual(created['after']['note'], 'Original instruction')
        self.assertIsNone(created['before'])
        self.assertEqual(edited['before'], created['after'])
        self.assertEqual(edited['after']['note'], 'Revised instruction')
        self.assertEqual(edited['after']['version'], 2)
        plan.refresh_from_db()
        self.assertEqual(plan.result['request_snapshot'], created['after'])
        self.assertEqual(plan.result['queue_snapshot'], [created['after']])

    def test_audit_history_cannot_be_overwritten(self):
        from django.db import transaction
        self.order()
        record = Audit.objects.get(action='order.create')
        with self.assertRaises(ValidationError):
            Audit.objects.filter(pk=record.pk).update(detail={})
        record.detail = {}
        with self.assertRaises(ValidationError):
            record.save()
        with self.assertRaises(IntegrityError), transaction.atomic():
            Audit(pk=record.pk, actor=self.operator, action='replacement', object_type='Order', object_id='1').save()

    def setUp(self):
        self.operator = User.objects.create_user('operator')
        self.operator.groups.add(Group.objects.get_or_create(name='operator')[0])
        self.admin = User.objects.create_user('admin')
        self.admin.groups.add(Group.objects.get_or_create(name='admin')[0])
        self.field = User.objects.create_user('field')
        self.field.groups.add(Group.objects.get_or_create(name='field')[0])
        self.now = timezone.now()

    def cargo(self, code='C1'):
        return services.register_cargo(self.operator, code=code, name='Cargo', weight_kg='1', volume_m3='1', destination='yard')

    def condition(self):
        return services.register_condition(self.admin, name='Declared', calibration_ref='installer-reference', payload={
            'outbound_stand': 'out', 'temporary_bays': ['temp'], 'allowed_moves': [['bay', 'out'], ['bay', 'temp'], ['temp', 'bay']],
            'max_observation_age_seconds': 60, 'conveyor': 'belt', 'arm_reach': [['A', 'bay'], ['A', 'temp'], ['A', 'out']]})

    def observation(self, code='C1', **overrides):
        fields = dict(source_id='manual', source_revision='source-8', observed_at=self.now, provenance='DECLARED', condition=self.condition(),
                      payload={'bays': [{'bay_id': 'bay', 'cargo': [code], 'capacity': 2}, {'bay_id': 'temp', 'cargo': [], 'capacity': 2},
                                        {'bay_id': 'out', 'cargo': [], 'capacity': 1}], 'holdings': []})
        fields.update(overrides)
        return services.record_observation(self.admin, **fields)

    def order(self, cargo=None, **overrides):
        fields = dict(cargo=cargo or self.cargo(), kind='OUTBOUND', priority_group=1, sequence=1, tie_policy='FIXED')
        fields.update(overrides)
        return services.create_order(self.operator, **fields)

    def episode(self):
        order = self.order(kind='INBOUND')
        return services.assign_episode(self.operator, order_id=order.pk, vehicle_id='vehicle-1', assigned_to=self.field)

    def test_cargo_validates_and_is_immutable(self):
        cargo = services.register_cargo(self.operator, code='C1', name='Cargo', weight_kg='1', volume_m3='1', destination='yard')
        cargo.name = 'Changed'
        with self.assertRaises(ValidationError):
            cargo.save()
        with self.assertRaises(ValidationError):
            Cargo.objects.filter(pk=cargo.pk).update(name='Changed')
        with self.assertRaises(ValidationError):
            Cargo.objects.filter(pk=cargo.pk).delete()
        with self.assertRaises(ValidationError):
            services.register_cargo(self.operator, code=' C2', name='Cargo', weight_kg='-1', volume_m3='1', destination='yard')
        self.assertEqual(Cargo.objects.count(), 1)

    def test_role_is_explicit(self):
        user = User.objects.create_superuser('root', password='unused')
        with self.assertRaises(PermissionDenied):
            services.register_cargo(user, code='C', name='Cargo', weight_kg='1', volume_m3='1', destination='yard')

    def test_new_instance_cannot_overwrite_existing_history(self):
        original = self.condition()
        replacement = Condition(pk=original.pk, name='Replaced', calibration_ref='changed', payload=original.payload,
                                created_by=self.admin, created_at=original.created_at)
        with self.assertRaises(IntegrityError), transaction.atomic():
            replacement.save()
        original.refresh_from_db()
        self.assertEqual(original.name, 'Declared')
        self.assertEqual(original.calibration_ref, 'installer-reference')

    def test_bulk_upsert_cannot_overwrite_history(self):
        original = self.condition()
        replacement = Condition(pk=original.pk, name='Replaced', calibration_ref='changed', payload=original.payload,
                                created_by=self.admin, created_at=original.created_at)
        with self.assertRaises(ValidationError):
            Condition.objects.bulk_create([replacement], update_conflicts=True, update_fields=['name'], unique_fields=['id'])
        original.refresh_from_db()
        self.assertEqual(original.name, 'Declared')
        with self.assertRaises(ValidationError):
            Condition.objects.bulk_update([replacement], ['name'])

    def test_observation_identity_and_closed_schema(self):
        self.cargo()
        observed = self.observation()
        fields = {key: getattr(observed, key) for key in ('source_id', 'source_revision', 'observed_at', 'provenance', 'payload', 'condition')}
        self.assertEqual(services.record_observation(self.admin, **fields).pk, observed.pk)
        with self.assertRaises(services.Conflict):
            services.record_observation(self.admin, **dict(fields, observed_at=self.now - timedelta(seconds=1)))
        with self.assertRaises(ValidationError):
            services.record_observation(self.admin, **dict(fields, source_revision='new', payload={**observed.payload, 'authenticated': True}))
        self.assertEqual(Observation.objects.count(), 1)

    def test_observation_import_requires_admin(self):
        self.cargo()
        observed = self.observation()
        self.assertEqual(observed.created_by_id, self.admin.pk)
        fields = {key: getattr(observed, key) for key in ('source_id', 'observed_at', 'provenance', 'payload', 'condition')}
        with self.assertRaises(PermissionDenied):
            services.record_observation(self.operator, source_revision='operator-import', **fields)
        self.assertEqual(Observation.objects.count(), 1)

    def test_unknown_duplicate_cargo_and_naive_time_rejected(self):
        self.cargo()
        with self.assertRaises(ValidationError):
            self.observation(code='missing')
        with self.assertRaises(ValidationError):
            self.observation(observed_at=self.now.replace(tzinfo=None))
        payload = {'bays': [{'bay_id': 'bay', 'cargo': ['C1', 'C1'], 'capacity': 2}], 'holdings': []}
        with self.assertRaises(ValidationError):
            self.observation(payload=payload)

    def test_condition_rejects_unknown_keys_and_wrong_role(self):
        condition = self.condition()
        with self.assertRaises(ValidationError):
            services.register_condition(self.admin, name='Bad', calibration_ref='ref', payload={**condition.payload, 'verified': True})
        with self.assertRaises(PermissionDenied):
            services.register_condition(self.operator, name='Bad', calibration_ref='ref', payload=condition.payload)

    def test_fixed_policy_and_optimistic_update(self):
        order = self.order()
        with self.assertRaises(ValidationError):
            self.order(cargo=order.cargo)
        with self.assertRaises(ValidationError):
            self.order(cargo=order.cargo, sequence=2, tie_policy='ANY')
        digest = services.queue_digest()
        changed = services.update_order(self.operator, order_id=order.pk, expected_version=1, priority_group=1, sequence=2, tie_policy='FIXED', note='Reordered')
        self.assertEqual(changed.version, 2)
        self.assertNotEqual(digest, services.queue_digest())
        with self.assertRaises(services.Conflict):
            services.update_order(self.operator, order_id=order.pk, expected_version=1, priority_group=1, sequence=3, tie_policy='FIXED')

    def test_planning_preserves_observation_and_original_revision(self):
        order = self.order()
        observed = self.observation()
        plan = services.make_plan(self.operator, order_id=order.pk, expected_version=1, observation_id=observed.pk, now=self.now)
        self.assertEqual(plan.result['status'], 'READY')
        self.assertEqual(plan.result['source_revision'], observed.pk)
        self.assertEqual(plan.result['projected_bays'][2]['cargo'], ['C1'])
        observed.refresh_from_db()
        self.assertEqual(observed.payload['bays'][0]['cargo'], ['C1'])
        self.assertEqual(observed.source_revision, 'source-8')
        plan.result = {}
        with self.assertRaises(ValidationError):
            plan.save()

    def test_stale_plans_hold(self):
        order = self.order()
        observed = self.observation()
        plan = services.make_plan(self.operator, order_id=order.pk, expected_version=1, observation_id=observed.pk, now=self.now + timedelta(seconds=61))
        self.assertEqual(plan.result['reason'], 'OBSERVATION_NOT_CURRENT')
        self.assertIsNone(plan.result['projected_bays'])

    def test_inbound_does_not_conflict_with_or_block_outbound_priority(self):
        inbound = self.order(kind='INBOUND')
        outbound = self.order(cargo=inbound.cargo)
        observed = self.observation()
        plan = services.make_plan(self.operator, order_id=outbound.pk, expected_version=1, observation_id=observed.pk, now=self.now)
        self.assertEqual(plan.result['status'], 'READY')
        services.update_order(self.operator, order_id=outbound.pk, expected_version=1, priority_group=2, sequence=1, tie_policy='ANY')
        plan = services.make_plan(self.operator, order_id=outbound.pk, expected_version=2, observation_id=observed.pk, now=self.now)
        self.assertEqual(plan.result['status'], 'READY')

    def test_priority_fixed_and_any(self):
        first = self.order()
        second = self.order(cargo=first.cargo, sequence=2)
        observed = self.observation()
        plan = services.make_plan(self.operator, order_id=second.pk, expected_version=1, observation_id=observed.pk, now=self.now)
        self.assertEqual(plan.result['reason'], 'PRIORITY_ORDER_BLOCKED')
        third = self.order(cargo=first.cargo, priority_group=2, tie_policy='ANY')
        plan = services.make_plan(self.operator, order_id=third.pk, expected_version=1, observation_id=observed.pk, now=self.now)
        self.assertEqual(plan.result['reason'], 'PRIORITY_ORDER_BLOCKED')

    def test_completed_outbound_no_longer_blocks_later_priorities(self):
        from harbor_web.models import Command
        first = self.order()
        observed = self.observation()
        done = services.make_plan(self.operator, order_id=first.pk, expected_version=1, observation_id=observed.pk, now=self.now)
        later = self.order(cargo=first.cargo, priority_group=2)
        blocked = services.make_plan(self.operator, order_id=later.pk, expected_version=1, observation_id=observed.pk, now=self.now)
        self.assertEqual(blocked.result['reason'], 'PRIORITY_ORDER_BLOCKED')
        command = Command.objects.create(order=first, plan=done, actor=self.operator, plan_version=1, ledger_identity='l',
            fleet_origin='o', fleet_principal='p', body_digest='a' * 64, binding={}, state='RECONCILED')
        still = services.make_plan(self.operator, order_id=later.pk, expected_version=1, observation_id=observed.pk, now=self.now)
        self.assertEqual(still.result['reason'], 'PRIORITY_ORDER_BLOCKED')  # handed off is not finished
        before = services.queue_digest()
        Command.objects.filter(pk=command.pk).update(state='COMPLETED')
        self.assertNotEqual(services.queue_digest(), before)  # the blocked plan now reads as needing review
        from harbor_web.workspace import plan_changes
        self.assertTrue(plan_changes(later, still))
        freed = services.make_plan(self.operator, order_id=later.pk, expected_version=1, observation_id=observed.pk, now=self.now)
        self.assertNotEqual(freed.result['reason'], 'PRIORITY_ORDER_BLOCKED')

    def test_any_group_allows_either_member(self):
        first = self.order(tie_policy='ANY')
        second = self.order(cargo=first.cargo, tie_policy='ANY')
        observed = self.observation()
        for order in (first, second):
            self.assertEqual(services.make_plan(self.operator, order_id=order.pk, expected_version=1, observation_id=observed.pk, now=self.now).result['status'], 'READY')

    def test_inbound_does_not_manufacture_planning(self):
        order = self.order(kind='INBOUND')
        observed = self.observation()
        self.assertEqual(services.make_plan(self.operator, order_id=order.pk, expected_version=1, observation_id=observed.pk).result['reason'], 'INBOUND_PLANNER_NOT_AVAILABLE')

    def test_field_scope_stale_and_departure_exactly_once(self):
        episode = self.episode()
        other = User.objects.create_user('other')
        other.groups.add(Group.objects.get(name='field'))
        with self.assertRaises(PermissionDenied):
            services.confirm_episode(other, episode_id=episode.pk, expected_version=1, outcome='CLEAR', observed_at=self.now)
        confirmation = services.confirm_episode(self.field, episode_id=episode.pk, expected_version=1, outcome='CLEAR', observed_at=self.now)
        with self.assertRaises(services.Conflict):
            services.confirm_episode(self.field, episode_id=episode.pk, expected_version=1, outcome='CLEAR', observed_at=self.now)
        intent = services.prepare_departure(self.operator, episode_id=episode.pk, confirmation_id=confirmation.pk)
        self.assertEqual(services.prepare_departure(self.operator, episode_id=episode.pk, confirmation_id=confirmation.pk).pk, intent.pk)
        self.assertEqual(DepartureIntent.objects.count(), 1)

    def test_confirmation_freshness_and_cross_episode(self):
        episode = self.episode()
        for stamp in (self.now - timedelta(minutes=6), self.now + timedelta(seconds=1)):
            with self.assertRaises(ValidationError):
                services.confirm_episode(self.field, episode_id=episode.pk, expected_version=1, outcome='CLEAR', observed_at=stamp, now=self.now)
        confirmation = services.confirm_episode(self.field, episode_id=episode.pk, expected_version=1, outcome='MISMATCH', observed_at=self.now)
        with self.assertRaises(ValidationError):
            services.prepare_departure(self.operator, episode_id=episode.pk, confirmation_id=confirmation.pk)
        another = services.assign_episode(self.operator, order_id=episode.order_id, vehicle_id='vehicle-2', assigned_to=self.field)
        with self.assertRaises(services.Conflict):
            services.prepare_departure(self.operator, episode_id=another.pk, confirmation_id=confirmation.pk)

    def test_expired_confirmation_and_disabled_assignee_cannot_depart(self):
        episode = self.episode()
        confirmation = services.confirm_episode(self.field, episode_id=episode.pk, expected_version=1, outcome='CLEAR', observed_at=self.now, now=self.now)
        with self.assertRaises(ValidationError):
            services.prepare_departure(self.operator, episode_id=episode.pk, confirmation_id=confirmation.pk, now=self.now + timedelta(minutes=6))
        self.field.is_active = False
        self.field.save()
        with self.assertRaises(PermissionDenied):
            services.prepare_departure(self.operator, episode_id=episode.pk, confirmation_id=confirmation.pk, now=self.now)
        self.assertFalse(DepartureIntent.objects.exists())

    def test_stale_order_cannot_make_new_plan(self):
        order = self.order()
        observed = self.observation()
        services.update_order(self.operator, order_id=order.pk, expected_version=1, priority_group=1, sequence=1, tie_policy='FIXED')
        with self.assertRaises(services.Conflict):
            services.make_plan(self.operator, order_id=order.pk, expected_version=1, observation_id=observed.pk)

    def test_reassignment_retires_old_tab_and_old_confirmation(self):
        episode = self.episode()
        other = User.objects.create_user('replacement')
        other.groups.add(Group.objects.get(name='field'))
        old_confirmation = services.confirm_episode(self.field, episode_id=episode.pk, expected_version=1, outcome='CLEAR', observed_at=self.now)
        replacement = services.reassign_episode(self.operator, episode_id=episode.pk, expected_version=2, assigned_to=other)
        episode.refresh_from_db()
        self.assertEqual((episode.state, episode.version), ('REASSIGNED', 3))
        self.assertEqual((replacement.order_id, replacement.vehicle_id, replacement.assigned_to_id), (episode.order_id, episode.vehicle_id, other.pk))
        self.assertNotEqual(replacement.key, episode.key)
        self.assertEqual(replacement.version, 1)
        with self.assertRaises(services.Conflict):
            services.confirm_episode(self.field, episode_id=episode.pk, expected_version=2, outcome='CLEAR', observed_at=self.now)
        with self.assertRaises(PermissionDenied):
            services.confirm_episode(self.field, episode_id=replacement.pk, expected_version=1, outcome='CLEAR', observed_at=self.now)
        with self.assertRaises(ValidationError):
            services.prepare_departure(self.operator, episode_id=episode.pk, confirmation_id=old_confirmation.pk)
        with self.assertRaises(services.Conflict):
            services.prepare_departure(self.operator, episode_id=replacement.pk, confirmation_id=old_confirmation.pk)
        self.assertTrue(Audit.objects.filter(action='episode.reassign', object_id=str(episode.pk), detail={'replacement_episode_id': replacement.pk}).exists())
        confirmation = services.confirm_episode(other, episode_id=replacement.pk, expected_version=1, outcome='CLEAR', observed_at=self.now)
        services.prepare_departure(self.operator, episode_id=replacement.pk, confirmation_id=confirmation.pk)
        with self.assertRaises(services.Conflict):
            services.reassign_episode(self.operator, episode_id=replacement.pk, expected_version=2, assigned_to=self.field)

    def test_reassignment_requires_operator_active_field_and_current_version(self):
        episode = self.episode()
        with self.assertRaises(PermissionDenied):
            services.reassign_episode(self.field, episode_id=episode.pk, expected_version=1, assigned_to=self.field)
        with self.assertRaises(services.Conflict):
            services.reassign_episode(self.operator, episode_id=episode.pk, expected_version=2, assigned_to=self.field)
        self.field.is_active = False
        self.field.save()
        with self.assertRaises(PermissionDenied):
            services.reassign_episode(self.operator, episode_id=episode.pk, expected_version=1, assigned_to=self.field)
        episode.refresh_from_db()
        self.assertEqual((episode.version, episode.state), (1, 'OPEN'))

    def measurement(self, **overrides):
        fields = dict(label='Trial', scenario='S', conditions_ref='C', initial_layout_ref='L', information_ref='I', method='BASELINE',
                      baseline_reach_verified=True, provenance='MEASURED', source_ref='operator-trial-log', vehicle_transfers=None, human_interventions=2)
        fields.update(overrides)
        return services.record_measurement(self.operator, **fields)

    def test_measurement_comparison_preserves_missing_and_checks_matching(self):
        left, right = self.measurement(), self.measurement(method='COLLABORATIVE', vehicle_transfers=2, human_interventions=1)
        result = services.compare_measurements(left, right)
        self.assertTrue(result['comparable'])
        self.assertIsNone(result['primary'][0]['delta'])
        self.assertIsNone(result['primary'][0]['left'])
        self.assertEqual(result['primary'][1]['delta'], -1)
        right.information_ref = 'different'
        self.assertFalse(services.compare_measurements(left, right)['comparable'])

    @override_settings(MODE='live')
    def test_live_rejects_synthetic(self):
        self.cargo()
        with self.assertRaises(ValidationError):
            self.observation(provenance='SYNTHETIC')
        with self.assertRaises(ValidationError):
            self.measurement(provenance='SYNTHETIC')

    def test_failed_writes_do_not_leave_audit(self):
        count = Audit.objects.count()
        with self.assertRaises(ValidationError):
            self.cargo(' bad')
        self.assertEqual(Audit.objects.count(), count)
