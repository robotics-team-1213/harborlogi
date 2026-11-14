from contextlib import closing
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from harbor.workflow import BusinessLedger


class SqlitePragmaTests(unittest.TestCase):
    def test_business_ledger_connection_pragmas(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = BusinessLedger(Path(tmp) / 'business.sqlite3', placement_sources=('camera',),
                                    retreat_sources=('fleet',))
            with closing(ledger._connect()) as connection:
                self.assertEqual(connection.execute('PRAGMA journal_mode').fetchone()[0], 'wal')
                self.assertEqual(connection.execute('PRAGMA foreign_keys').fetchone()[0], 1)
                self.assertEqual(connection.execute('PRAGMA busy_timeout').fetchone()[0], 10000)
                self.assertEqual(connection.execute('PRAGMA synchronous').fetchone()[0], 1)

    @unittest.skipIf(importlib.util.find_spec('django') is None, 'django not installed')
    def test_django_default_connection_pragmas(self):
        for database, expected_journal in ((':memory:', 'memory'), ('file', 'wal')):
            with self.subTest(database=database), tempfile.TemporaryDirectory() as tmp:
                path = str(Path(tmp) / 'web.sqlite3') if database == 'file' else ':memory:'
                env = dict(os.environ, DJANGO_SETTINGS_MODULE='harbor_web.settings', HARBOR_WEB_MODE='test',
                           HARBOR_WEB_SECRET_KEY='x' * 50, HARBOR_WEB_DATABASE=path)
                script = '''
import django
django.setup()
from django.db import connection, transaction
with connection.cursor() as cursor:
    result = {name: cursor.execute('PRAGMA ' + name).fetchone()[0]
              for name in ('journal_mode', 'foreign_keys', 'busy_timeout', 'synchronous')}
result['transaction_mode'] = connection.transaction_mode
statements = []
connection.connection.set_trace_callback(statements.append)
with transaction.atomic():
    with connection.cursor() as cursor:
        cursor.execute('SELECT 1')
result['began_immediate'] = any(statement == 'BEGIN IMMEDIATE' for statement in statements)
connection.close()
print(__import__('json').dumps(result))
'''
                run = subprocess.run([sys.executable, '-c', script], env=env, text=True,
                                     capture_output=True, check=True)
                result = json.loads(run.stdout)
                self.assertEqual(result, {'journal_mode': expected_journal, 'foreign_keys': 1,
                                          'busy_timeout': 20000, 'synchronous': 1,
                                          'transaction_mode': 'IMMEDIATE', 'began_immediate': True})
