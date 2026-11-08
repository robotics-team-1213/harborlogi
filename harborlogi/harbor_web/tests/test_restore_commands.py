from django.test import TransactionTestCase
from django.db import connection
from harbor_web.tests import test_commands as command_fixtures
from harbor_web import commands
from harbor_web.models import Command, CommandFact
from harbor_web.runtime_models import DeploymentState
from harbor_web.runtime import backup_product, restore_product, ledger_identity_matches
from harbor_web.services import Conflict
from unittest.mock import patch
from pathlib import Path
from contextlib import closing
import sqlite3
import os
import json


class RestoreCommandsTests(TransactionTestCase):
    setUp = command_fixtures.CommandTests.setUp
    plan = command_fixtures.CommandTests.plan
    cargo = command_fixtures.CommandTests.cargo
    condition = command_fixtures.CommandTests.condition
    observation = command_fixtures.CommandTests.observation
    order = command_fixtures.CommandTests.order
    job = command_fixtures.CommandTests.job

    def test_relocated_pair_retains_identity_and_get_reconciliation(self):
        DeploymentState.objects.update_or_create(pk=1, defaults={'mode': 'test'})
        command = commands.prepare_command(self.operator, plan_id=self.plan().pk, expected_version=1)
        original = command.ledger_identity
        registry = commands.acquire_registry()
        with patch.object(registry.client, 'propose_arm', side_effect=TimeoutError):
            command = commands.submit_command(self.operator, command_id=command.pk, expected_version=command.version)
        with patch.object(registry.client, 'arm_request', return_value={'job': self.job(command)}):
            command = commands.reconcile_command(self.operator, command_id=command.pk, expected_version=command.version)
        commands.registry_close()
        root = Path(self.directory.name)
        web = root / 'source-web.sqlite3'
        with closing(sqlite3.connect(web)) as target:
            connection.connection.backup(target)
        backup = root / 'backup'
        backup_product(backup, web, os.environ['HARBOR_LEDGER_PATH'], 'test')
        restored_web, restored_ledger = root / 'restored-web', root / 'restored-ledger'
        restore_product(backup, restored_web, restored_ledger, 'test')
        # A second normal recovery must preserve original Command A identity through B to C.
        with self.assertRaises(ValueError):
            backup_product(root / 'wrong-pair-backup', restored_web, os.environ['HARBOR_LEDGER_PATH'], 'test')
        second_backup = root / 'second-backup'
        with patch.dict(os.environ, HARBOR_LEDGER_PATH=str(restored_ledger)):
            backup_product(second_backup, restored_web, restored_ledger, 'test')
        restored_web, restored_ledger = root / 'second-web', root / 'second-ledger'
        restore_product(second_backup, restored_web, restored_ledger, 'test')
        with closing(sqlite3.connect(restored_web)) as c:
            row = c.execute('SELECT relocation, restored_at FROM harbor_web_deploymentstate').fetchone()
            self.assertEqual(c.execute('SELECT ledger_identity FROM harbor_web_command').fetchone()[0], original)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM harbor_web_commandfact').fetchone()[0], 1)
        DeploymentState.objects.filter(pk=1).update(relocation=json.loads(row[0]), restored_at=row[1])
        with patch.dict(os.environ, HARBOR_LEDGER_PATH=str(restored_ledger)):
            self.assertTrue(ledger_identity_matches(original, commands._identity(commands._config())))
            self.assertFalse(ledger_identity_matches(original, '0' * 64))
            self.assertIsNotNone(commands.command_detail(command)['job'])
            registry = commands.acquire_registry()
            with patch.object(registry.client, 'arm_request', return_value={'job': self.job(command)}) as get, patch.object(registry.client, 'propose_arm') as post:
                Command.objects.filter(pk=command.pk).update(state='PREPARED')
                command.refresh_from_db()
                command = commands.submit_command(self.operator, command_id=command.pk, expected_version=command.version)
                self.assertEqual(command.reason, 'RESTORE_RECONCILIATION_REQUIRED')
                version = command.version
                before = registry.ledger.read(commands._key(command))
                command = commands.reconcile_command(self.operator, command_id=command.pk, expected_version=command.version)
                self.assertEqual(command.state, 'BLOCKED')
                self.assertEqual(command.version, version)
                self.assertEqual(command.reason, 'RESTORE_RECONCILIATION_REQUIRED')
                self.assertEqual(registry.ledger.read(commands._key(command)), before)
                get.assert_called()
                post.assert_not_called()
                facts = CommandFact.objects.filter(command=command).count()
                with patch.object(registry.client, 'arm_request', side_effect=TimeoutError):
                    with self.assertRaises(Conflict):
                        commands.reconcile_command(self.operator, command_id=command.pk,
                            expected_version=command.version, resume=True)
                command.refresh_from_db()
                self.assertEqual(command.state, 'BLOCKED')
                self.assertEqual(command.version, version)
                self.assertEqual(registry.ledger.read(commands._key(command)), before)
                self.assertEqual(CommandFact.objects.filter(command=command).count(), facts)
                post.assert_not_called()
            command.refresh_from_db()
            self.assertEqual(command.ledger_identity, original)
        commands.registry_close()
        with patch.dict(os.environ, HARBOR_LEDGER_PATH=str(root / 'wrong-ledger')):
            with self.assertRaises(Conflict):
                commands._validate(command, commands._config())
