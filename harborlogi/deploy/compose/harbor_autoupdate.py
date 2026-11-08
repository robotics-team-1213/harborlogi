#!/usr/bin/env python3
"""Move this Harbor host to the newest image CI published from a green team main (ADR 0021).

Runs as root from harbor-autoupdate.timer: python3 /opt/harbor/current/harbor_autoupdate.py

Order (ADR 0021 §4): stop the executor, pair backup, migrate, start, then record the new image.
A failed start without a schema change returns to the previous image and skips that digest.
A failed start after a schema change stays stopped under a hold file: restoring a backup blocks
new submissions until a person reconciles execution, so it must not happen automatically.

shortcut: trusts the digest over TLS and does not verify the build attestation, because
`gh attestation verify` needs a token on the host; add that check if the host gains one.
"""
from __future__ import annotations

import datetime
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPOSITORY = 'ghcr.io/robotics-team-1213/harborlogi'
ETC = Path('/etc/harbor')
ENV = ETC / 'compose.env'
SITE = ETC / 'compose.site.yaml'
HOLD = ETC / 'autoupdate.hold'
FAILED = ETC / 'autoupdate.failed'
RELEASES = Path('/opt/harbor/releases')
CURRENT = Path('/opt/harbor/current')
KEEP_BACKUPS = 10
MANAGE = ('--profile', 'setup', 'run', '--rm', 'manage', 'python', '-m', 'harbor_web.manage')


def run(*command, env=None, check=True):
    return subprocess.run(command, check=check, text=True, capture_output=True, timeout=900,
                          env={**os.environ, **(env or {})})


def read_env():
    return dict(line.split('=', 1) for line in ENV.read_text(encoding='utf-8').splitlines()
                if '=' in line and not line.lstrip().startswith('#'))


def record_image(image):
    lines = [line for line in ENV.read_text(encoding='utf-8').splitlines() if not line.startswith('HARBOR_IMAGE=')]
    staged = ENV.with_suffix('.new')
    staged.write_text('\n'.join([*lines, f'HARBOR_IMAGE={image}']) + '\n', encoding='utf-8')
    staged.chmod(0o600)
    os.replace(staged, ENV)


def switch_current(release):
    staged = CURRENT.with_name('current.new')
    staged.unlink(missing_ok=True)
    staged.symlink_to(release)
    os.replace(staged, CURRENT)


def compose(release, image, *args, check=True):
    # The process environment outranks --env-file, so compose.env changes only after a good start.
    files = ['-f', str(release / 'compose.yaml')] + (['-f', str(SITE)] if SITE.exists() else [])
    return run('docker', 'compose', '--env-file', str(ENV), *files, *args, env={'HARBOR_IMAGE': image}, check=check)


def start(release, image):
    compose(release, image, 'up', '-d', '--no-build', '--wait', '--wait-timeout', '180', 'harbor', 'proxy')


def unpack_release(image):
    release = RELEASES / image.rsplit(':', 1)[1][:12]
    if release.exists():
        return release
    container = run('docker', 'create', image).stdout.strip()
    try:
        staged = release.with_name(release.name + '.new')
        shutil.rmtree(staged, ignore_errors=True)
        staged.mkdir(parents=True)
        run('docker', 'cp', f'{container}:/app/deploy/compose/.', str(staged))
        os.replace(staged, release)
    finally:
        run('docker', 'rm', container)
    return release


def prune(backup_root, keep_images, keep_releases):
    for old in sorted(backup_root.glob('auto-*'))[:-KEEP_BACKUPS]:
        shutil.rmtree(old)
    keep_ids = {run('docker', 'inspect', '--format', '{{.Id}}', image).stdout.strip() for image in keep_images}
    for image_id in set(run('docker', 'images', '--no-trunc', '--format', '{{.ID}}', REPOSITORY).stdout.split()):
        if image_id not in keep_ids:
            run('docker', 'rmi', '-f', image_id, check=False)
    for release in RELEASES.iterdir():
        if release not in keep_releases:
            shutil.rmtree(release)


def update():
    if HOLD.exists():
        print(f'held: {HOLD}')
        return 0
    run('docker', 'pull', f'{REPOSITORY}:main')
    image = run('docker', 'inspect', '--format', '{{index .RepoDigests 0}}', f'{REPOSITORY}:main').stdout.strip()
    settings = read_env()
    previous = settings.get('HARBOR_IMAGE', '')
    if image == previous:
        print(f'up to date: {image}')
        return 0
    if FAILED.exists() and FAILED.read_text(encoding='utf-8').strip() == image:
        print(f'skipping failed image: {image}')
        return 0

    release, old_release = unpack_release(image), CURRENT.resolve()
    backup_root = Path(settings.get('HARBOR_BACKUP_DIR', '/var/backups/harbor'))
    compose(old_release, previous, 'stop', 'proxy', 'harbor')
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%d-%H%M%S')
    compose(old_release, previous, *MANAGE, 'backup_product', '--output', f'/backup/auto-{stamp}')
    schema_changes = compose(release, image, *MANAGE, 'migrate', '--check', check=False).returncode != 0
    try:
        compose(release, image, *MANAGE, 'migrate', '--noinput')
        start(release, image)
    except subprocess.CalledProcessError as error:
        return recover(error, image, previous, old_release, release, schema_changes, stamp)

    record_image(image)
    switch_current(release)
    FAILED.unlink(missing_ok=True)
    prune(backup_root, [image, previous], {release, old_release})
    print(f'updated: {previous} -> {image}')
    return 0


def recover(error, image, previous, old_release, release, schema_changes, stamp):
    print(f'failed starting {image}: {error.stderr}', file=sys.stderr)
    if not schema_changes:
        try:
            start(old_release, previous)
            FAILED.write_text(image + '\n', encoding='utf-8')
            print(f'returned to {previous}', file=sys.stderr)
            return 1
        except subprocess.CalledProcessError as rollback_error:
            print(f'rollback failed: {rollback_error.stderr}', file=sys.stderr)
    compose(release, image, 'stop', 'proxy', 'harbor', check=False)
    HOLD.write_text(f'{image} failed; stopped for a person. Backup: auto-{stamp}. Remove this file to resume.\n',
                    encoding='utf-8')
    return 1


if __name__ == '__main__':
    sys.exit(update())
