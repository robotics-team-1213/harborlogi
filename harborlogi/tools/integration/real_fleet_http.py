"""Run Harbor over HTTP against a pinned platform through Harbor's own simulation harness.

Only this development process imports the harness and platform. The child Harbor
test process uses the public HTTP adapter and a temporary synthetic credential.
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile


FULL_FLOW_KEY = 'harbor-full-flow'
# (cargo, source, destination): rehandle the blocker, outbound to the stand, restore the blocker.
# shortcut: every move uses the one synthetic installed source->target binding; per-move bindings come with S4.
FULL_FLOW_MOVES = (('blocker-1', 'yard-top', 'temp-1'), ('outbound-1', 'yard-below', 'stand'),
                   ('blocker-1', 'temp-1', 'yard-top'))


def verify_checkout(root, expected_sha):
    root = Path(root).resolve()
    if not re.fullmatch(r'[0-9a-f]{40}', expected_sha):
        raise ValueError('A full pinned commit SHA is required')
    def git(*args):
        return subprocess.check_output(['git', '-C', str(root), *args], text=True).strip()
    actual = git('rev-parse', 'HEAD')
    if actual != expected_sha or Path(git('rev-parse', '--show-toplevel')).resolve() != root:
        raise ValueError('Checkout differs from the requested source root or revision')
    if git('status', '--porcelain'):
        raise ValueError('Integration source checkout must be clean')
    return actual


def app_checkout(app_root):
    """Harbor lives in a folder of the team repo, so verify the whole containing checkout."""
    def git(*args):
        return subprocess.check_output(['git', '-C', str(app_root), *args], text=True).strip()
    return verify_checkout(git('rev-parse', '--show-toplevel'), git('rev-parse', 'HEAD'))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--platform-root', type=Path, required=True)
    parser.add_argument('--platform-sha', required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--transport-only', action='store_true', help='Verify neutral TRIP without the Cell compiler')
    mode.add_argument('--arm-only', action='store_true', help='Verify neutral ARM with an independent observer and real UDS')
    mode.add_argument('--evidence-only', action='store_true', help='Verify immutable physical evidence and single-move business confirmation')
    mode.add_argument('--web-arm-only', action='store_true', help='Verify authenticated Django over actual Fleet HTTP')
    mode.add_argument('--full-flow', action='store_true', help='Run the first flow stages on one Fleet serving TRIP and ARM (D-927)')
    mode.add_argument('--arm-via-r', action='store_true',
                      help='Run the neutral ARM test through integrated control R relaying to a separate cell Fleet (D-965)')
    parser.add_argument('--via-r', action='store_true',
                        help='With --full-flow: Harbor talks only to integrated control R; a separate cell Fleet owns the arms (D-965)')
    parser.add_argument('--report', type=Path, help='With --full-flow: write per-stage results as JSON here')
    parser.add_argument('--sim-arm', action='store_true',
                        help='With --arm-only: the platform MuJoCo cell owner instead of the synthetic owners (D-927 C4)')
    parser.add_argument('--app-python', type=Path, help='Isolated Harbor/web Python interpreter')
    args = parser.parse_args(argv)
    if args.web_arm_only and args.app_python is None:
        parser.error('--web-arm-only requires --app-python')
    if args.via_r and not args.full_flow:
        parser.error('--via-r requires --full-flow')
    if args.sim_arm and not args.arm_only:
        parser.error('--sim-arm requires --arm-only')
    app_root = Path(__file__).resolve().parents[2]
    app_sha = app_checkout(app_root)
    platform_sha = verify_checkout(args.platform_root, args.platform_sha)
    arm_mode = args.arm_only or args.web_arm_only or args.evidence_only
    sys.path.insert(0, str(app_root))
    # Loaded by path only here: the harness needs the platform's test dependencies.
    arm = args.arm_only or args.web_arm_only or args.full_flow or args.arm_via_r
    source = Path(__file__).resolve().parent / ('arm_fleet_harness.py' if arm else 'fleet_harness.py')
    spec = importlib.util.spec_from_file_location('harbor_fleet_harness', source)
    harness = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(harness)
    # Windows keeps the in-process Fleet SQLite file open after stop; a leftover temp dir must not mask the result.
    with tempfile.TemporaryDirectory(prefix='harbor-http-integration-', ignore_cleanup_errors=True) as temporary:
        request_key_path = Path(temporary) / 'request-key' if args.web_arm_only else None
        fleet = (harness.RelayedArmFleet(root=args.platform_root, workdir=Path(temporary) / 'fleet') if args.arm_via_r else
                 harness.RelayedFullFlowFleet(root=args.platform_root, workdir=Path(temporary) / 'fleet',
                     request_keys=[f'{FULL_FLOW_KEY}:{move}' for move in range(len(FULL_FLOW_MOVES))]) if args.via_r else
                 harness.FullFlowFleet(root=args.platform_root, workdir=Path(temporary),
                     request_keys=[f'{FULL_FLOW_KEY}:{move}' for move in range(len(FULL_FLOW_MOVES))]) if args.full_flow else
                 harness.ArmFleet(root=args.platform_root, workdir=Path(temporary) / 'fleet', request_key_path=request_key_path,
                                  sim=args.sim_arm) if (args.arm_only or args.web_arm_only) else
                 harness.RealFleet(root=args.platform_root, workdir=Path(temporary) / 'fleet', transport_only=args.transport_only))
        if args.evidence_only:
            fleet.include_joint_evidence = True
        try:
            fleet.start()
            ref, artifact = {}, None
            if not args.transport_only and not arm:
                ref = next(iter(fleet.transfers.values()))
                with fleet.client() as client:
                    response = client.post('/api/fleet/cell-app/compile', json=ref)
                    response.raise_for_status()
                    artifact = response.json()['process_artifact_digest']
            config = {
                'schema': 'harbor.transport-http-test/1' if args.transport_only else 'harbor.http-test/1', 'simulation': True,
                'origin': fleet.url, 'operator': fleet.tokens['operator'], 'viewer': fleet.tokens['viewer'],
                'approver': fleet.tokens.get('approver'),  # R relays; its cell alone approves
                'platform_sha': platform_sha, 'app_sha': app_sha,
                'binding': {**ref, 'cargo_id': 'synthetic-cargo', 'source': 'source', 'destination': 'destination',
                            'workcell_id': harness.WORKCELL, 'instance_id': harness.INSTANCE,
                            'process_artifact_digest': artifact},
                'candidates': [harness.ROBOT], 'steps': [{'to': 'inbound'}],
                'alternate_steps': [{'to': 'outbound'}],
            }
            if args.arm_only or args.web_arm_only or args.arm_via_r:
                config = {'schema': 'harbor.arm-http-test/1', 'simulation': True, 'origin': fleet.url,
                    'operator': fleet.tokens['operator'], 'viewer': fleet.tokens['viewer'],
                    'platform_sha': platform_sha, 'app_sha': app_sha,
                    'binding': {'cargo_id': 'synthetic-outbound', 'source': 'yard', 'destination': 'stand',
                        'workcell_id': fleet.identity[0], 'instance_id': fleet.identity[1], 'object_ref': 'object-1',
                        'source_ref': 'source', 'target_ref': 'target'}}
                if args.sim_arm:
                    config['goal_timeout_s'] = 600
            if args.full_flow:
                from tools.integration.fleet_harness import ROBOT
                arm_ref = {'workcell_id': harness.WORKCELL, 'instance_id': harness.INSTANCE, 'object_ref': 'object-1',
                           'source_ref': 'source', 'target_ref': 'target'}
                config = {'schema': 'harbor.full-flow-http-test/1', 'simulation': True, 'origin': fleet.url,
                    'operator': fleet.tokens['operator'], 'platform_sha': platform_sha, 'app_sha': app_sha,
                    'report_path': str(args.report.resolve()) if args.report else None,
                    'moves': [{**arm_ref, 'cargo_id': c, 'source': s, 'destination': d} for c, s, d in FULL_FLOW_MOVES],
                    'outbound_id': 'outbound-1', 'outbound_stand': 'stand',
                    'trip_candidates': [ROBOT], 'trip_destination': 'inbound',
                    'arms': {harness.INSTANCE: 'A', 'arm-2': 'B'}}
            if args.web_arm_only:
                config['schema'] = 'harbor.web-arm-http-test/1'
                config['request_key_path'] = str(request_key_path)
                config.pop('viewer')
            path = Path(temporary) / 'http-test.json'
            path.write_text(json.dumps(config), encoding='utf-8')
            path.chmod(0o600)
            env = dict(os.environ, HARBOR_INTEGRATION='1', HARBOR_INTEGRATION_CONFIG=str(path))
            env.pop('PYTHONPATH', None)
            print(f'Platform {platform_sha}; Harbor (with harness) {app_sha}', flush=True)
            pattern = 'full_flow_http.py' if args.full_flow else 'product_web_http.py' if args.web_arm_only else 'arm_http.py' if arm else 'transport_http.py' if args.transport_only else 'test_real_fleet_http.py'
            return subprocess.run([str(args.app_python) if args.app_python else sys.executable, '-m', 'unittest', 'discover', '-s', 'tests/integration', '-p', pattern, '-v'],
                                  cwd=app_root, env=env, timeout=900 if args.sim_arm else 180).returncode
        finally:
            fleet.stop()


if __name__ == '__main__':
    raise SystemExit(main())
