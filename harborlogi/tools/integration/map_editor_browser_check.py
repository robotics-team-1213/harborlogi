"""Exercise immutable map versions in an isolated database and actual Edge browser."""
import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time
import traceback
from urllib.request import urlopen
from urllib.parse import parse_qs, urlparse
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(os.environ.get('LOCALAPPDATA', '')) / 'Temp/harbor-web-browser'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    output = parser.parse_args().output.resolve() / ('run-' + uuid.uuid4().hex)
    output.mkdir(parents=True)
    for key in list(os.environ):
        if key.startswith('HARBOR_'):
            del os.environ[key]
    os.environ.update(DJANGO_SETTINGS_MODULE='harbor_web.settings', HARBOR_WEB_MODE='development',
        HARBOR_WEB_SECRET_KEY=secrets.token_urlsafe(64), HARBOR_WEB_DATABASE=str(output / 'product.sqlite3'))
    import django
    django.setup()
    from django.contrib.auth.models import Group, User
    from django.core.management import call_command
    from django.utils import timezone
    from harbor_web import services
    from harbor_web.models import CargoFootprint, Command, Condition, Observation
    from playwright.sync_api import sync_playwright

    call_command('migrate', verbosity=0)
    call_command('bootstrap_roles', verbosity=0)
    password = secrets.token_urlsafe(30)
    admin = User.objects.create_user('map-admin', password=password)
    admin.groups.add(Group.objects.get(name='admin'))
    operator = User.objects.create_user('map-operator')
    operator.groups.add(Group.objects.get(name='operator'))
    cargo = services.register_cargo(operator, code='MAP-C1', name='Synthetic cargo',
        weight_kg=1, volume_m3=1, destination='synthetic')
    policy = json.loads((ROOT / 'config/spatial-policy.example.json').read_text(encoding='utf-8'))
    policy['yard_layout'] = json.loads((ROOT / 'config/yard-layout.json').read_text(encoding='utf-8'))
    original = services.register_condition(admin, name='Synthetic original map', calibration_ref='synthetic-only', payload=policy)
    observed = services.record_observation(admin, source_id='map-synthetic', source_revision='one',
        observed_at=timezone.now(), provenance='SYNTHETIC', condition=original,
        payload=dict(schema='grid-placement-v1', observed_areas=['yard', 'temp_a', 'temp_b', 'stand'], holdings=[],
            placements=[dict(cargo_id=cargo.code, area_id='yard', row=1, column=1, level=1,
                orientation=0, footprint_revision=CargoFootprint.objects.get(cargo=cargo).pk)]))
    original_payload, observation_payload = deepcopy(original.payload), deepcopy(observed.payload)
    with socket.socket() as allocated:
        allocated.bind(('127.0.0.1', 0))
        port = allocated.getsockname()[1]
    origin = f'http://127.0.0.1:{port}'
    result = dict(status='FAILED', output=str(output), checks=[], viewports=[], actual_fleet=False)
    child = None
    log = (output / 'server.log').open('w', encoding='utf-8')
    try:
        child = subprocess.Popen([sys.executable, '-m', 'harbor_web', '--port', str(port)],
            cwd=ROOT, env=os.environ.copy(), stdout=log, stderr=log)
        deadline = time.monotonic() + 30
        while True:
            if child.poll() is not None:
                raise AssertionError('Isolated server exited')
            try:
                if urlopen(origin + '/accounts/login/', timeout=1).status == 200:
                    break
            except OSError:
                if time.monotonic() >= deadline:
                    raise AssertionError('Server readiness timeout')
                time.sleep(.2)
        os.environ['DJANGO_ALLOW_ASYNC_UNSAFE'] = 'true'
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel='msedge', headless=True)
            page = browser.new_page(viewport=dict(width=1440, height=1000))
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            # The invalid-overlap flow deliberately returns one HTTP400 validation response.
            page.on('console', lambda msg: errors.append(msg.text) if msg.type == 'error' and
                'status of 400' not in msg.text else None)
            page.on('requestfailed', lambda request: errors.append(request.url))
            page.goto(origin + '/accounts/login/')
            page.locator('[name=username]').fill(admin.username)
            page.locator('[name=password]').fill(password)
            page.locator('main button[type=submit]').click()
            page.wait_for_load_state('networkidle')
            page.goto(origin + f'/settings/conditions/{original.pk}/map/')
            page.locator('[data-map-canvas][data-ready=true]').wait_for()
            properties = page.locator('[data-map-properties]')
            save = page.locator('[data-map-save]')

            def apply(**fields):
                for name, value in fields.items():
                    control = properties.locator(f'[name={name}]')
                    if name == 'role':
                        control.select_option(str(value))
                    else:
                        control.fill(str(value))
                properties.locator('button[type=submit]').click()

            def validate(ok=True):
                with page.expect_response(lambda response: response.url.endswith('/validate/') and response.request.method == 'POST') as response:
                    page.locator('[data-map-validate]').click()
                assert response.value.status == (200 if ok else 400), response.value.text()
                page.wait_for_function('() => document.querySelector("[data-map-editor]").getAttribute("aria-busy") === "false"')
                assert save.is_enabled() == ok

            apply(columns=4)
            assert page.locator('[data-map-cells] input').count() == 12
            assert page.locator('[data-map-cells] input').first.input_value() == policy['yard_layout']['cells'][0]['bay_id']
            page.locator('[data-map-add]').click()
            apply(id='store', label='Overflow storage', role='storage', rows=2, columns=2, levels=2)
            validate()
            result['checks'].append('Native inputs grow yard to four columns, preserve existing IDs, and add named resized ordinary storage')
            properties.locator('[name=label]').fill('Overflow storage pending')
            assert save.is_disabled()
            page.locator('[data-map-validate]').click()
            assert page.locator('[data-map-errors] li').count() > 0
            assert save.is_disabled()
            properties.locator('button[type=submit]').click()
            validate()
            result['checks'].append('Pending property changes invalidate fingerprint and block publish/validate until applied')
            original_x, original_y = properties.locator('[name=x]').input_value(), properties.locator('[name=y]').input_value()
            apply(x=0, y=0)
            validate(False)
            assert page.locator('[data-map-errors]').inner_text()
            apply(x=original_x, y=original_y)
            validate()
            for width, height in [(1440, 1000), (768, 1024), (390, 844)]:
                page.set_viewport_size(dict(width=width, height=height))
                page.locator('[data-map-canvas][data-ready=true]').wait_for()
                page.locator('[data-map-view=fit]').click()
                assert not page.evaluate('document.documentElement.scrollWidth > innerWidth'), width
                page.evaluate('window.scrollTo(0, 0)')
                page.screenshot(path=str(output / f'{width}-editor.png'), full_page=True)
                result['viewports'].append(dict(width=width, height=height, overflow=False))
            publish = page.locator('[data-map-publish]')
            publish.locator('[name=name]').fill('Synthetic expanded version')
            publish.locator('[name=calibration_ref]').fill('synthetic-review-only')
            with page.expect_response(lambda response: response.request.method == 'POST' and response.url.endswith('/map/')) as response:
                save.click()
            assert response.value.status == 200, response.value.text()
            assert 'application/json' in response.value.headers['content-type']
            page.wait_for_url('**/inventory/?condition=*')
            condition_id = int(parse_qs(urlparse(page.url).query)['condition'][0])
            page.wait_for_load_state('networkidle')
            assert not Observation.objects.filter(condition_id=condition_id).exists()
            assert page.get_by_text('배치 미관측', exact=True).count() > 0
            assert page.locator('[data-model-editor]').count() == 0
            assert page.locator('form [name=observation]').count() == 0
            assert page.locator('[data-cargo-id]:not([data-cargo-id=""])').count() == 0
            assert page.locator('[data-yard-linked-flat]').get_attribute('data-source') == 'map-definition'
            page.screenshot(path=str(output / 'new-version-unobserved.png'), full_page=True)
            created = Condition.objects.get(pk=condition_id)
            assert created.payload['yard_layout']['columns'] == 4
            assert created.payload['storage_areas'] == ['yard', 'store']
            original.refresh_from_db()
            observed.refresh_from_db()
            assert original.payload == original_payload
            assert observed.condition_id == original.pk and observed.payload == observation_payload
            assert not Command.objects.exists()
            result['checks'].append('Validated JSON publish creates a new unobserved version; original condition and observation stay pinned; no cargo editing or commands')
            page.goto(origin + f'/settings/conditions/{condition_id}/map/')
            page.locator('[data-map-canvas][data-ready=true]').wait_for()
            assert page.locator('[data-map-areas] [data-area=store]').count() == 1
            page.locator('[data-map-areas] [data-area=yard]').click()
            assert properties.locator('[name=columns]').input_value() == '4'
            assert not errors, errors
            result.update(status='PASSED', errors=errors, source_condition=original.pk, created_condition=condition_id)
            browser.close()
    except Exception:
        result['traceback'] = traceback.format_exc()
    finally:
        if child is not None:
            child.terminate()
            child.wait(timeout=10)
        log.close()
        (output / 'report.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['status'] == 'PASSED' else 1


if __name__ == '__main__':
    raise SystemExit(main())
