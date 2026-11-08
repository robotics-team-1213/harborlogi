"""ADR 0029: notification links open on the protected business screen, not the workbench only."""
import json
from pathlib import Path
import sqlite3
import tempfile
import os
from unittest.mock import patch

from django.contrib.auth.models import Group, User
from django.test import TestCase

from harbor_web import commands
from harbor_web.tests import test_services as fixtures


class NotificationLinkTests(TestCase):
    def setUp(self):
        fixtures.ServicesTests.setUp(self)
        from django.conf import settings
        from harbor_web.runtime_models import DeploymentState
        DeploymentState.objects.get_or_create(pk=1, defaults={'mode': settings.MODE})
        self.viewer = User.objects.create_user('viewer')
        self.viewer.groups.add(Group.objects.get_or_create(name='viewer')[0])
        self.outsider = User.objects.create_user('outsider')  # Logged in, no screen role.
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(commands.registry_close)
        root = Path(self.directory.name)
        bindings = root / 'bindings.json'
        bindings.write_text(json.dumps([dict(cargo_id='C1', source='bay', destination='out', workcell_id='cell',
            instance_id='instance', object_ref='cargo', source_ref='bay-ref', target_ref='out-ref')]))
        self.environment = patch.dict(os.environ, dict(HARBOR_LEDGER_PATH=str(root / 'ledger.db'),
            HARBOR_FLEET_ORIGIN='http://127.0.0.1:9999', HARBOR_FLEET_TOKEN='test',
            HARBOR_FLEET_PRINCIPAL='operator-1', HARBOR_BINDINGS_PATH=str(bindings),
            HARBOR_WEB_ALLOW_SIMULATION='1'))
        self.environment.start()
        self.addCleanup(self.environment.stop)
        plan = self.plan()
        self.command = commands.prepare_command(self.operator, plan_id=plan.pk, expected_version=1)
        registry = commands.acquire_registry()
        commands._prepare_ledger(self.command, registry)  # The ledger row exists from submission on.
        registry.ledger.hold('harbor:' + str(self.command.key))  # One HOLD occurrence.
        from contextlib import closing
        with closing(sqlite3.connect(os.environ['HARBOR_LEDGER_PATH'])) as direct:
            self.notification = direct.execute(
                "SELECT notification_id FROM hold_outbox").fetchone()[0]
        self.url = f'/work/{self.notification}/'

    def plan(self):
        from harbor_web import services
        order = self.order()
        observation = self.observation(provenance='SYNTHETIC')
        return services.make_plan(self.operator, order_id=order.pk, expected_version=1,
                                  observation_id=observation.pk)

    cargo = fixtures.ServicesTests.cargo
    condition = fixtures.ServicesTests.condition
    observation = fixtures.ServicesTests.observation
    order = fixtures.ServicesTests.order

    def test_anonymous_gets_the_login_redirect_without_id_disclosure(self):
        for url in (self.url, f"/work/n{'0' * 32}/"):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 302)
                self.assertTrue(response['Location'].startswith('/accounts/login/'))

    def test_roleless_user_is_forbidden(self):
        self.client.force_login(self.outsider)
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_viewer_lands_on_the_existing_work_screen(self):
        self.client.force_login(self.viewer)
        response = self.client.get(self.url)
        self.assertRedirects(response, f'/work/{self.command.pk}/', fetch_redirect_response=False)

    def test_field_staff_follow_the_link_like_the_work_screen(self):
        # Same roles as /work/<pk>/: the work screen itself decides what field staff may see.
        self.client.force_login(self.field)
        response = self.client.get(self.url)
        self.assertRedirects(response, f'/work/{self.command.pk}/', fetch_redirect_response=False)

    def test_unknown_or_malformed_ids_are_not_found_after_login(self):
        self.client.force_login(self.viewer)
        for url in (f"/work/n{'0' * 32}/", '/work/not-an-id/', '/work/12345/'):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 404)

    def test_missing_ledger_setting_is_reported_not_folded_into_not_found(self):
        self.client.force_login(self.viewer)
        with patch.dict(os.environ, {'HARBOR_LEDGER_PATH': ''}):
            self.assertEqual(self.client.get(self.url).status_code, 503)

    def test_pre_prefix_hex_ids_are_not_links(self):
        self.client.force_login(self.viewer)
        self.assertEqual(self.client.get(f"/work/{self.notification[1:]}/").status_code, 404)
