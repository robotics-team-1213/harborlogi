from django.contrib.auth.models import Group, User
from django.core.management import call_command
from django.test import TestCase

from harbor_web import services
from harbor_web.models import FieldEpisode
from tools import product_preview


class PreviewPersonaTests(TestCase):
    def setUp(self):
        call_command('bootstrap_roles', verbosity=0)

    def test_creates_one_account_per_persona(self):
        result = product_preview.ensure_persona_accounts(None)
        self.assertEqual(set(result), {'operator', 'field', 'admin', 'viewer'})
        self.assertEqual(product_preview.PERSONA_ACCOUNTS['field'], 'harbor-field')
        for role, entry in result.items():
            user = User.objects.get(username=entry['username'])
            self.assertEqual(entry['username'], product_preview.PERSONA_ACCOUNTS[role])
            self.assertEqual(set(user.groups.values_list('name', flat=True)), {role})
            self.assertEqual(user.is_staff, role == 'admin')
            self.assertTrue(user.check_password(entry['password']))

    def test_keeps_saved_passwords(self):
        first = product_preview.ensure_persona_accounts(None)
        second = product_preview.ensure_persona_accounts(first)
        self.assertEqual(first, second)
        for entry in second.values():
            self.assertTrue(User.objects.get(username=entry['username']).check_password(entry['password']))

    def test_fills_missing_persona(self):
        first = product_preview.ensure_persona_accounts(None)
        saved = {'operator': first['operator']}
        second = product_preview.ensure_persona_accounts(saved)
        self.assertEqual(second['operator'], first['operator'])
        self.assertEqual(set(second), {'operator', 'field', 'admin', 'viewer'})
        for entry in second.values():
            self.assertTrue(User.objects.get(username=entry['username']).check_password(entry['password']))


class EnsureFieldEpisodeTests(TestCase):
    def setUp(self):
        call_command('bootstrap_roles', verbosity=0)
        self.accounts = product_preview.ensure_persona_accounts(None)
        self.actor = User.objects.create_user('preview-all')
        self.actor.groups.add(*Group.objects.filter(name__in=['operator', 'field']))
        self.field = User.objects.get(username='harbor-field')
        cargo = services.register_cargo(self.actor, code='C3', name='합성', weight_kg=2, volume_m3='0.012', destination='x', length_cells=3)
        self.inbound = services.create_order(self.actor, cargo=cargo, kind='INBOUND', priority_group=3, sequence=1, tie_policy='FIXED')

    def open_episodes(self):
        return FieldEpisode.objects.filter(order=self.inbound, state='OPEN')

    def test_assigns_when_no_open_episode(self):
        product_preview.ensure_field_episode(self.actor, self.inbound)
        self.assertEqual([e.assigned_to_id for e in self.open_episodes()], [self.field.pk])

    def test_moves_open_episode_from_another_user(self):
        services.assign_episode(self.actor, order_id=self.inbound.pk, vehicle_id='합성 차량 P1', assigned_to=self.actor)
        product_preview.ensure_field_episode(self.actor, self.inbound)
        self.assertEqual([e.assigned_to_id for e in self.open_episodes()], [self.field.pk])
        self.assertEqual(FieldEpisode.objects.filter(order=self.inbound, state='REASSIGNED').count(), 1)

    def test_second_call_is_noop(self):
        product_preview.ensure_field_episode(self.actor, self.inbound)
        before = list(FieldEpisode.objects.filter(order=self.inbound).values_list('pk', 'version', 'state'))
        product_preview.ensure_field_episode(self.actor, self.inbound)
        self.assertEqual(before, list(FieldEpisode.objects.filter(order=self.inbound).values_list('pk', 'version', 'state')))
