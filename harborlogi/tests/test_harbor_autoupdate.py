"""The host updater keeps ADR 0021's order and never restores or retries by itself."""
import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / 'deploy/compose/harbor_autoupdate.py'
OLD = 'ghcr.io/robotics-team-1213/harborlogi@sha256:aaaaaaaaaaaaaaaa'
NEW = 'ghcr.io/robotics-team-1213/harborlogi@sha256:bbbbbbbbbbbbbbbb'


def load():
    spec = importlib.util.spec_from_file_location('harbor_autoupdate', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Updater(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.module = load()
        self.module.ENV = self.root / 'compose.env'
        self.module.SITE = self.root / 'absent.yaml'
        self.module.HOLD = self.root / 'hold'
        self.module.FAILED = self.root / 'failed'
        self.module.RELEASES = self.root / 'releases'
        self.module.CURRENT = self.root / 'releases/old'
        self.module.RELEASES.mkdir()
        self.module.CURRENT.mkdir()
        self.module.ENV.write_text(f'HARBOR_IMAGE={OLD}\nHARBOR_BACKUP_DIR={self.root}/backup\n', encoding='utf-8')
        (self.root / 'backup').mkdir()
        self.calls = []
        self.fail = set()
        self.pending_migration = False
        self.latest = NEW
        patches = [mock.patch.object(self.module, 'run', self.fake_run),
                   mock.patch.object(self.module, 'unpack_release', self.fake_unpack),
                   mock.patch.object(self.module, 'switch_current', lambda release: self.calls.append('switch'))]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def fake_unpack(self, image):
        release = self.module.RELEASES / 'new'
        release.mkdir(exist_ok=True)
        return release

    def fake_run(self, *command, env=None, check=True):
        text = ' '.join(command)
        image = (env or {}).get('HARBOR_IMAGE', '')
        step = next((name for name in ('backup_product', 'migrate --check', 'migrate --noinput', ' stop ', ' up ')
                     if name in text), None)
        if step:
            self.calls.append((step.strip(), image))
            if (step.strip(), image) in self.fail and check:
                raise subprocess.CalledProcessError(1, command, stderr='boom')
        code = 1 if step == 'migrate --check' and self.pending_migration else 0
        output = self.latest if 'RepoDigests' in text else ''
        return subprocess.CompletedProcess(command, code, stdout=output, stderr='')

    def recorded(self):
        return self.module.read_env()['HARBOR_IMAGE']

    def test_same_digest_changes_nothing(self):
        self.latest = OLD
        self.assertEqual(self.module.update(), 0)
        self.assertEqual(self.calls, [])

    def test_hold_file_stops_before_pulling(self):
        self.module.HOLD.write_text('held', encoding='utf-8')
        with mock.patch.object(self.module, 'run', side_effect=AssertionError('docker reached')):
            self.assertEqual(self.module.update(), 0)

    def test_success_keeps_the_adr_order_and_records_after_start(self):
        with mock.patch.object(self.module, 'prune'):
            self.assertEqual(self.module.update(), 0)
        self.assertEqual(self.calls, [('stop', OLD), ('backup_product', OLD), ('migrate --check', NEW),
                                      ('migrate --noinput', NEW), ('up', NEW), 'switch'])
        self.assertEqual(self.recorded(), NEW)

    def test_failed_start_without_schema_change_returns_and_skips_that_digest(self):
        self.fail = {('up', NEW)}
        self.assertEqual(self.module.update(), 1)
        self.assertEqual(self.calls[-1], ('up', OLD))
        self.assertEqual(self.recorded(), OLD)
        self.assertFalse(self.module.HOLD.exists())
        self.calls.clear()
        self.assertEqual(self.module.update(), 0)
        self.assertEqual(self.calls, [])

    def test_failed_start_after_schema_change_stays_stopped_and_holds(self):
        self.pending_migration = True
        self.fail = {('up', NEW)}
        self.assertEqual(self.module.update(), 1)
        self.assertNotIn(('up', OLD), self.calls)
        self.assertEqual(self.calls[-1], ('stop', NEW))
        self.assertTrue(self.module.HOLD.exists())
        self.assertEqual(self.recorded(), OLD)

    def test_failed_rollback_holds(self):
        self.fail = {('up', NEW), ('up', OLD)}
        self.assertEqual(self.module.update(), 1)
        self.assertTrue(self.module.HOLD.exists())


if __name__ == '__main__':
    unittest.main()
