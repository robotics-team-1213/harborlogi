from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase

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
