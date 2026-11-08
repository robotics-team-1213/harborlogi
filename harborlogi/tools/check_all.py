"""Run Harbor's isolated automated checks, including browser acceptance.

Usage (from harborlogi): python -m tools.check_all
Requires the web extra, Node.js, Playwright and Microsoft Edge.
This does not exercise an installed Fleet or move real robots.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[1]


def test_environment():
    env = {key: value for key, value in os.environ.items() if not key.startswith('HARBOR_')}
    env.update(
        HARBOR_WEB_MODE='test', HARBOR_WEB_DATABASE=':memory:',
        HARBOR_WEB_SECRET_KEY=secrets.token_urlsafe(64),
        DJANGO_SETTINGS_MODULE='harbor_web.settings', PYTHONIOENCODING='utf-8',
    )
    return env


def checks(output):
    python = sys.executable
    yield 'domain', [python, '-m', 'unittest', 'discover', '-s', 'tests', '-v']
    yield 'web', [python, '-m', 'django', 'test', 'harbor_web.tests', '--settings=harbor_web.settings', '--verbosity', '2']
    yield 'migrations', [python, '-m', 'django', 'makemigrations', '--check', '--dry-run', '--settings=harbor_web.settings']
    for path in sorted((ROOT / 'tests').glob('*.test.mjs')):
        yield path.stem, ['node', str(path.relative_to(ROOT))]
    yield 'design', [python, 'tools/design_tokens.py', '--check']
    yield 'browser', [python, 'tools/integration/product_web_browser_check.py', '--output', str(output / 'browser')]
    for name in ('map_editor', 'multicell', 'fleet_bridge'):
        yield name, [python, f'tools/integration/{name}_browser_check.py', '--output', str(output / name)]
    yield 'automatic_planning', [python, 'tools/integration/automatic_planning_browser_check.py']


def run_command(command, env, timeout=600):
    group = ({'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == 'nt'
             else {'start_new_session': True})
    # A descendant holding stdout must not prevent an exited parent from completing.
    # shortcut: detached children of an exited Windows parent are not supervised; use a Job Object if harnesses daemonize.
    with tempfile.TemporaryFile() as capture, subprocess.Popen(
            command, cwd=ROOT, env=env, stdout=capture, stderr=subprocess.STDOUT, **group) as child:
        diagnostic = ''
        try:
            code = child.wait(timeout=timeout)
        except subprocess.TimeoutExpired as error:
            # Kill the tree while its parent still exists so its servers and browsers are included.
            if os.name == 'nt':
                try:
                    cleanup = subprocess.run(['taskkill', '/PID', str(child.pid), '/T', '/F'],
                                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
                    if cleanup.returncode:
                        diagnostic += f'\nProcess-tree cleanup failed: taskkill exit {cleanup.returncode}; descendants may remain.\n'
                except (OSError, subprocess.TimeoutExpired) as cleanup_error:
                    diagnostic += f'\nProcess-tree cleanup failed: {cleanup_error}; descendants may remain.\n'
            else:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if child.poll() is None:
                child.kill()
            child.wait()
            code = 124
            diagnostic += f'\nTimeoutExpired: {error}\n'
        capture.seek(0)
        return code, capture.read().decode('utf-8', errors='replace') + diagnostic


def run(output):
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    report = {
        'status': 'FAILED', 'started_at': datetime.now(timezone.utc).isoformat(),
        'actual_fleet': False, 'physical_acceptance': False,
        'limitations': [
            'Real Fleet contract tests are skipped by this runner; use the separate configured harness.',
            'Browser checks use synthetic data; physical and field acceptance are not tested.',
        ],
        'checks': [],
    }
    env = test_environment()
    for name, command in checks(output):
        print(f'Running {name}...', flush=True)
        try:
            code, log = run_command(command, env)
        except OSError as error:
            code, log = 127, f'{type(error).__name__}: {error}\n'
        (output / f'{name}.log').write_text(log, encoding='utf-8')
        report['checks'].append({'name': name, 'exit_code': code, 'log': f'{name}.log'})
        print(f'{name}: {"PASSED" if code == 0 else "FAILED"}', flush=True)
    failed = any(step['exit_code'] != 0 for step in report['checks'])
    report['status'] = 'FAILED' if failed else 'PASSED'
    report['finished_at'] = datetime.now(timezone.utc).isoformat()
    (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'{report["status"]}: {output / "report.json"}')
    return int(failed)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'runs' / 'check-all')
    args = parser.parse_args()
    return run(args.output / ('run-' + uuid.uuid4().hex))


if __name__ == '__main__':
    raise SystemExit(main())
