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
    parser.add_argument('--app-python', type=Path, help='Isolated Harbor/web Python interpreter')
    args = parser.parse_args(argv)
    if args.web_arm_only and args.app_python is None:
        parser.error('--web-arm-only requires --app-python')
    app_root = Path(__file__).resolve().parents[2]
    app_sha = app_checkout(app_root)
    platform_sha = verify_checkout(args.platform_root, args.platform_sha)
    arm_mode = args.arm_only or args.web_arm_only or args.evidence_only
    sys.path.insert(0, str(app_root))
    # Loaded by path only here: the harness needs the platform's test dependencies.
    source = Path(__file__).resolve().parent / ('arm_fleet_harness.py' if arm_mode else 'fleet_harness.py')
    spec = importlib.util.spec_from_file_location('harbor_fleet_harness', source)
    harness = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(harness)
    # Windows keeps the in-process Fleet SQLite file open after stop; a leftover temp dir must not mask the result.
    with tempfile.TemporaryDirectory(prefix='harbor-http-integration-', ignore_cleanup_errors=True) as temporary:
        request_key_path = Path(temporary) / 'request-key' if args.web_arm_only else None
        fleet = (harness.ArmFleet(root=args.platform_root, workdir=Path(temporary) / 'fleet', request_key_path=request_key_path) if arm_mode else
                 harness.RealFleet(root=args.platform_root, workdir=Path(temporary) / 'fleet', transport_only=args.transport_only))
        if args.evidence_only:
            fleet.include_joint_evidence = True
        try:
            fleet.start()
            ref, artifact = {}, None
            if not args.transport_only and not arm_mode:
                ref = next(iter(fleet.transfers.values()))
                with fleet.client() as client:
                    response = client.post('/api/fleet/cell-app/compile', json=ref)
                    response.raise_for_status()
                    artifact = response.json()['process_artifact_digest']
            config = {
                'schema': 'harbor.transport-http-test/1' if args.transport_only else 'harbor.http-test/1', 'simulation': True,
                'origin': fleet.url, 'operator': fleet.tokens['operator'], 'viewer': fleet.tokens['viewer'],
                'approver': fleet.tokens['approver'],
                'platform_sha': platform_sha, 'app_sha': app_sha,
                'binding': {**ref, 'cargo_id': 'synthetic-cargo', 'source': 'source', 'destination': 'destination',
                            'workcell_id': harness.WORKCELL, 'instance_id': harness.INSTANCE,
                            'process_artifact_digest': artifact},
                'candidates': [harness.ROBOT], 'steps': [{'to': 'inbound'}],
                'alternate_steps': [{'to': 'outbound'}],
            }
            if arm_mode:
                config = {'schema': 'harbor.arm-http-test/1', 'simulation': True, 'origin': fleet.url,
                    'operator': fleet.tokens['operator'], 'viewer': fleet.tokens['viewer'],
                    'platform_sha': platform_sha, 'app_sha': app_sha,
                    'binding': {'cargo_id': 'synthetic-outbound', 'source': 'yard', 'destination': 'stand',
                        'workcell_id': harness.WORKCELL, 'instance_id': harness.INSTANCE, 'object_ref': 'object-1',
                        'source_ref': 'source', 'target_ref': 'target'}}
            if args.evidence_only:
                policy = fleet.app.state.arm_transfer_job_store.installation.policy
                config['schema'] = 'harbor.physical-evidence-http-test/1'
                config['evidence'] = dict(placement_source=policy.producer_id, retreat_source='fleet-closure',
                    policy_digest=policy.digest, arms=[dict(arm=arm, workcell_id=participant.workcell_id,
                        instance_id=participant.instance_id, zone_id=policy.zone_resource_ids[0],
                        joint_names=['joint1', 'joint2']) for arm, participant in zip(('A', 'B'), policy.participants)])
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
            pattern = ('physical_evidence_http.py' if args.evidence_only else 'product_web_http.py' if args.web_arm_only
                       else 'arm_http.py' if arm_mode else 'transport_http.py' if args.transport_only else 'test_real_fleet_http.py')
            return subprocess.run([str(args.app_python) if args.app_python else sys.executable, '-m', 'unittest', 'discover', '-s', 'tests/integration', '-p', pattern, '-v'],
                                  cwd=app_root, env=env, timeout=180).returncode
        finally:
            fleet.stop()


if __name__ == '__main__':
    raise SystemExit(main())
