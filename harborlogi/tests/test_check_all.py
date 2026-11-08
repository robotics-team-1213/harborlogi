import json
import os
from pathlib import Path
import socket
import signal
import subprocess
import sys
import time
import tempfile
import unittest
from unittest.mock import patch

from tools import check_all


class CheckAllTests(unittest.TestCase):
    def setUp(self):
        quiet = patch('builtins.print')
        quiet.start()
        self.addCleanup(quiet.stop)

    def test_failure_does_not_hide_later_checks_and_returns_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            with patch.object(check_all, 'checks', return_value=[
                ('first', ['missing-runtime']), ('second', ['python', 'test'])
            ]), patch.object(check_all, 'run_command', side_effect=[
                FileNotFoundError('missing-runtime'),
                (0, 'second check ran'),
            ]) as run:
                self.assertEqual(check_all.run(output), 1)
            self.assertEqual(run.call_count, 2)
            report = json.loads((output / 'report.json').read_text(encoding='utf-8'))
            self.assertEqual(report['status'], 'FAILED')
            self.assertFalse(report['actual_fleet'])
            self.assertFalse(report['physical_acceptance'])
            self.assertEqual([step['exit_code'] for step in report['checks']], [127, 0])
            self.assertIn('second check ran', (output / 'second.log').read_text(encoding='utf-8'))

    def test_test_environment_does_not_inherit_operator_configuration(self):
        with patch.dict(os.environ, {
            'HARBOR_WEB_DATABASE': '/operator/production.sqlite3',
            'HARBOR_FLEET_TOKEN': 'private-test-sentinel',
            'DJANGO_SETTINGS_MODULE': 'operator.settings',
        }):
            env = check_all.test_environment()
        self.assertEqual(env['HARBOR_WEB_DATABASE'], ':memory:')
        self.assertEqual(env['HARBOR_WEB_MODE'], 'test')
        self.assertNotIn('HARBOR_FLEET_TOKEN', env)
        self.assertEqual(env['DJANGO_SETTINGS_MODULE'], 'harbor_web.settings')

    def test_timeout_is_a_failure_with_diagnostic_log(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            with patch.object(check_all, 'checks', return_value=[('slow', ['python'])]), \
                    patch.object(check_all, 'run_command', return_value=(124, 'partial progress\nTimeoutExpired: slow')):
                self.assertEqual(check_all.run(output), 1)
            self.assertIn('TimeoutExpired', (output / 'slow.log').read_text(encoding='utf-8'))
            self.assertIn('partial progress', (output / 'slow.log').read_text(encoding='utf-8'))

    def test_timeout_reaps_descendants_and_keeps_partial_output(self):
        with tempfile.TemporaryDirectory() as directory:
            ready = Path(directory) / 'ready.json'
            child = (
                'import json,socket,time; from pathlib import Path; '
                's=socket.socket(); s.bind(("127.0.0.1",0)); s.listen(); '
                f'Path({str(ready)!r}).write_text(json.dumps(s.getsockname()[1])); '
                'print("child ready",flush=True); time.sleep(30)'
            )
            parent = (
                'import subprocess,sys,time; '
                f'subprocess.Popen([sys.executable,"-c",{child!r}]); '
                'time.sleep(30)'
            )
            code, log = check_all.run_command([sys.executable, '-c', parent], check_all.test_environment(), timeout=2)
            self.assertEqual(code, 124)
            self.assertIn('child ready', log)
            self.assertIn('TimeoutExpired', log)
            port = json.loads(ready.read_text())
            with socket.socket() as connection:
                connection.settimeout(.5)
                self.assertNotEqual(connection.connect_ex(('127.0.0.1', port)), 0)

    def test_command_keeps_failure_output_and_exit_status(self):
        code, log = check_all.run_command(
            [sys.executable, '-c', 'import sys; print("diagnostic",file=sys.stderr); sys.exit(3)'],
            check_all.test_environment(),
        )
        self.assertEqual(code, 3)
        self.assertIn('diagnostic', log)

    def test_exited_parent_does_not_wait_for_descendant_output_pipe(self):
        with tempfile.TemporaryDirectory() as directory:
            pid_file = Path(directory) / 'child.pid'
            child = 'import time; time.sleep(3)'
            parent = (
                'import subprocess,sys; from pathlib import Path; '
                f'child=subprocess.Popen([sys.executable,"-c",{child!r}]); '
                f'Path({str(pid_file)!r}).write_text(str(child.pid)); '
                'print("parent complete",flush=True)'
            )
            try:
                started = time.monotonic()
                code, log = check_all.run_command([sys.executable, '-c', parent], check_all.test_environment(), timeout=.5)
                self.assertEqual(code, 0)
                self.assertIn('parent complete', log)
                self.assertLess(time.monotonic() - started, 1.5)
            finally:
                if pid_file.exists():
                    pid = int(pid_file.read_text())
                    if os.name == 'nt':
                        subprocess.run(['taskkill', '/PID', str(pid), '/T', '/F'],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    else:
                        try:
                            os.kill(pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass

    @unittest.skipUnless(os.name == 'nt', 'Windows taskkill fallback')
    def test_failed_tree_cleanup_reaps_parent_and_reports_timeout(self):
        with patch.object(check_all.subprocess, 'run', return_value=subprocess.CompletedProcess([], 128)):
            code, log = check_all.run_command(
                [sys.executable, '-c', 'import time; print("partial",flush=True); time.sleep(3)'],
                check_all.test_environment(), timeout=.5,
            )
        self.assertEqual(code, 124)
        self.assertIn('partial', log)
        self.assertIn('Process-tree cleanup failed: taskkill exit 128', log)
        self.assertIn('TimeoutExpired', log)

    def test_catalog_includes_every_javascript_suite_and_browser_acceptance(self):
        with tempfile.TemporaryDirectory() as directory:
            stages = dict(check_all.checks(Path(directory)))
        for path in (check_all.ROOT / 'tests').glob('*.test.mjs'):
            self.assertIn(str(path.relative_to(check_all.ROOT)), stages[path.stem])
        self.assertIn('browser', stages)
        self.assertIn('harbor_web.tests', stages['web'])
        self.assertIn('makemigrations', stages['migrations'])
        for name in ('map_editor', 'multicell', 'fleet_bridge', 'automatic_planning'):
            self.assertIn(name, stages)

    def test_command_exit_status_controls_report_and_process_result(self):
        for code, status in [(0, 'PASSED'), (1, 'FAILED')]:
            with self.subTest(code=code), tempfile.TemporaryDirectory() as directory:
                output = Path(directory)
                with patch.object(check_all, 'checks', return_value=[('suite', ['python'])]), \
                        patch.object(check_all, 'run_command', return_value=(code, 'suite output')):
                    self.assertEqual(check_all.run(output), code)
                report = json.loads((output / 'report.json').read_text(encoding='utf-8'))
                self.assertEqual(report['status'], status)


if __name__ == '__main__':
    unittest.main()
