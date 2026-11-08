"""Mandatory isolated authenticated product acceptance using installed Playwright/Edge."""
import argparse
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
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(os.environ.get('LOCALAPPDATA', '')) / 'Temp/harbor-web-browser'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve() / ('run-' + uuid.uuid4().hex)
    output.mkdir(parents=True)
    for key in list(os.environ):
        if key.startswith('HARBOR_'):
            del os.environ[key]
    os.environ.update(DJANGO_SETTINGS_MODULE='harbor_web.settings', HARBOR_WEB_MODE='development',
                      HARBOR_WEB_SECRET_KEY=secrets.token_urlsafe(64), HARBOR_WEB_DATABASE=str(output / 'product.sqlite3'))
    import django
    django.setup()
    from django.core.management import call_command
    from django.contrib.auth.models import User, Group
    from django.utils import timezone
    from harbor_web import services
    from harbor_web.models import Cargo, Plan, Observation, Order, FieldEpisode, FieldConfirmation, DepartureIntent, Measurement, Command
    from playwright.sync_api import sync_playwright
    call_command('migrate', verbosity=0)
    call_command('bootstrap_roles', verbosity=0)
    password = secrets.token_urlsafe(30)
    users = {}
    for name, role in [('operator', 'operator'), ('viewer', 'viewer'), ('admin', 'admin'), ('field1', 'field'), ('field2', 'field')]:
        users[name] = User.objects.create_user(name, password=password)
        users[name].groups.add(Group.objects.get_or_create(name=role)[0])
        if role == 'admin':
            users[name].is_staff = True
            users[name].save()
    op, admin = users['operator'], users['admin']
    for code in ['C1', 'C2'] + [f'PAG-{i:02}' for i in range(27)]:
        services.register_cargo(op, code=code, name='Browser synthetic cargo', weight_kg='1', volume_m3='1', destination='yard')
    policy = json.loads((ROOT / 'config/yard-grid.example.json').read_text(encoding='utf-8'))
    policy['yard_layout'] = json.loads((ROOT / 'config/yard-layout.json').read_text(encoding='utf-8'))
    condition = services.register_condition(admin, name='Browser synthetic installation', calibration_ref='synthetic-browser-only', payload=policy)
    bays = [dict(bay_id=c['bay_id'], cargo=['C1', 'C2'] if c['bay_id'] == 'Y11' else [], capacity=3) for c in policy['yard_layout']['cells']]
    bays += [dict(bay_id='temp', cargo=[], capacity=3), dict(bay_id='stand', cargo=[], capacity=1)]
    observed = services.record_observation(admin, source_id='browser-synthetic', source_revision='browser-1', observed_at=timezone.now(), provenance='SYNTHETIC', condition=condition,
        payload={'bays': bays, 'holdings': []})
    inbound = services.create_order(op, cargo=Cargo.objects.get(code='C2'), kind='INBOUND', priority_group=9, sequence=1, tie_policy='FIXED')
    measurement_fields = dict(scenario='browser', conditions_ref='same-condition', initial_layout_ref='same-layout', information_ref='same-information', baseline_reach_verified=True, provenance='MEASURED', source_ref='browser-fixture-reference', vehicle_transfers=None, human_interventions=None, elapsed_seconds=None)
    left = services.record_measurement(op, label='Browser baseline', method='BASELINE', **measurement_fields)
    with socket.socket() as allocated:
        allocated.bind(('127.0.0.1', 0))
        port = allocated.getsockname()[1]
    origin = f'http://127.0.0.1:{port}'
    result = {'status': 'FAILED', 'output': str(output), 'checks': [], 'viewports': [], 'actual_fleet': False}
    child = None
    log = (output / 'server.log').open('w', encoding='utf-8')
    try:
        child = subprocess.Popen([sys.executable, '-m', 'harbor_web', '--port', str(port)], cwd=ROOT, env=os.environ.copy(), stdout=log, stderr=log)
        deadline = time.monotonic() + 30
        while True:
            if child.poll() is not None:
                raise AssertionError('Isolated web server exited; inspect server.log')
            try:
                with urlopen(origin + '/accounts/login/', timeout=1) as response:
                    if response.status == 200:
                        break
            except OSError:
                if time.monotonic() >= deadline:
                    raise AssertionError('Isolated server readiness timeout')
                time.sleep(.2)
        # Playwright runs an internal event loop; this isolated harness performs ORM reads sequentially.
        os.environ['DJANGO_ALLOW_ASYNC_UNSAFE'] = 'true'
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel='msedge', headless=True)

            def visit(page, path):
                response = page.goto(origin + path)
                page.wait_for_load_state('networkidle')
                assert response.status == 200, (path, response.status)

            def login(page, username):
                visit(page, '/accounts/login/')
                assert page.locator('input[name=csrfmiddlewaretoken]').count() == 1
                page.locator('[name=username]').fill(username)
                page.locator('[name=password]').fill(password)
                page.locator('main button[type=submit]').click()
                page.wait_for_load_state('networkidle')
                assert '/accounts/login/' not in page.url

            def submit(page, values):
                for key, value in values.items():
                    control = page.locator('main [name="' + key + '"]')
                    if control.first.evaluate('(e)=>e.tagName') == 'SELECT':
                        control.select_option(str(value))
                    elif control.first.get_attribute('type') == 'checkbox':
                        control.set_checked(bool(value))
                    else:
                        control.fill('' if value is None else str(value))
                page.get_by_role('button', name='저장', exact=True).click()
                page.wait_for_load_state('networkidle')

            def post(page, path, data):
                token = page.locator('[name=csrfmiddlewaretoken]').first.input_value()
                return page.request.post(origin + path, form={**data, 'csrfmiddlewaretoken': token}, headers={'Referer': page.url})

            def capture(page, filename):
                assert not page.evaluate('document.documentElement.scrollWidth > innerWidth'), ('horizontal overflow', page.url)
                page.screenshot(path=str(output / filename), full_page=True)

            def plan_flat(page):
                figure = page.locator('[data-yard-3d][data-ready="true"]')
                figure.wait_for()
                page.locator('[data-scene-animate]').uncheck()
                grid = page.locator('[data-yard-linked-flat]')
                switch = page.locator('[data-yard-view="flat"]')
                if page.viewport_size['width'] >= 1200:
                    assert switch.is_hidden() and figure.is_visible() and grid.is_visible()
                else:
                    switch.click()
                    assert figure.is_hidden() and grid.is_visible()
                return grid

            page = browser.new_page(viewport={'width': 1440, 'height': 1000})
            login(page, 'operator')
            visit(page, '/cargo/new/')
            submit(page, dict(code='UI-CARGO', name='UI registered cargo', weight_kg='1', volume_m3='1', destination='yard'))
            assert Cargo.objects.filter(code='UI-CARGO').exists()
            visit(page, '/cargo/?q=PAG')
            page.get_by_role('link', name='다음', exact=True).click()
            assert 'q=PAG' in page.url and 'page=2' in page.url
            assert page.locator('[name=q]').input_value() == 'PAG'
            visit(page, '/requests/new/')
            submit(page, dict(cargo=Cargo.objects.get(code='C1').pk, kind='OUTBOUND', priority_group=1, sequence=1, tie_policy='FIXED', note='Browser outbound'))
            outbound = Order.objects.get(kind='OUTBOUND')
            page.locator('[name=observation]').select_option(str(observed.pk))
            page.get_by_role('button', name='반출 계획 만들기', exact=True).click()
            page.wait_for_load_state('networkidle')
            plan = Plan.objects.get(order=outbound)
            assert plan.result['status'] == 'READY' and len(plan.result['moves']) == 3, plan.result
            assert all(move.get('primary_arm') for move in plan.result['moves']), plan.result
            observed.refresh_from_db()
            assert observed.payload['bays'][0]['cargo'] == ['C1', 'C2']
            assert plan.result['projected_bays'][0]['cargo'] == ['C2']
            assert not Command.objects.exists()
            assert page.locator('main form[data-command]').count() == 0
            assert page.locator('.transfer-list > li').count() == 3
            assert page.locator('[data-yard-stage]').count() == 4
            assert all(page.locator('.transfer-list > li').nth(i).locator('.move-arm').inner_text().strip() for i in range(3))
            grid = plan_flat(page)
            page.locator('[data-yard-stage="3"]').focus()
            page.keyboard.press('Enter')
            assert page.locator('[data-yard-stage="3"]').get_attribute('aria-pressed') == 'true'
            assert grid.get_attribute('data-source') == 'predicted'
            assert grid.locator('[data-grid-area="stand"] [data-cargo-id="C1"]').is_visible()
            assert grid.locator('[data-grid-area="yard"] [data-cargo-id="C2"]').is_visible()
            position = json.loads(grid.locator('[data-grid-area="yard"] [data-cargo-id="C2"]').get_attribute('data-selection'))
            assert [position[key] for key in ('row', 'column', 'level')] == [1, 1, 1], position
            assert grid.locator('[data-grid-area="yard"] .grid-cell').count() == 9
            grid.locator('[data-grid-area="yard"] [data-linked-level="2"]').focus()
            page.keyboard.press('Enter')
            page.locator('[data-yard-stage="0"]').click()
            assert grid.locator('[data-grid-area="yard"] [data-linked-level="2"]').get_attribute('aria-pressed') == 'true'
            assert grid.locator('[data-grid-area="yard"] [data-cargo-id="C2"]').is_visible()
            position = json.loads(grid.locator('[data-grid-area="yard"] [data-cargo-id="C2"]').get_attribute('data-selection'))
            assert [position[key] for key in ('row', 'column', 'level')] == [1, 1, 2], position
            grid.locator('[data-grid-area="yard"] [data-linked-level="3"]').click()
            assert grid.locator('[data-grid-area="yard"] .grid-cell').count() == 9
            grid.locator('[data-grid-area="yard"] [data-linked-level="1"]').click()
            assert grid.get_attribute('data-source') == 'original'
            observed.refresh_from_db()
            assert observed.payload['bays'][0]['cargo'] == ['C1', 'C2'] and not Command.objects.exists()
            page.locator('[data-yard-stage="0"]').click()
            assert grid.is_visible()
            visit(page, '/outbound/?q=C1')
            assert page.locator('.action-table tbody tr').count() == 1
            page.get_by_role('link', name='야드·계획·근거 확인', exact=True).click()
            assert f'/requests/{outbound.pk}/' in page.url
            result['checks'].append('Configured 3x3x3 yard with27 positions, keyboard layer selection, retained level across plan stages, target outbound/blocker restoration, unchanged original observation, execution closed')
            no_js_context = browser.new_context(java_script_enabled=False)
            no_js = no_js_context.new_page()
            login(no_js, 'viewer')
            visit(no_js, f'/plans/{plan.pk}/')
            assert no_js.locator('[data-yard-panel]:visible').count() == 4
            assert no_js.locator('[data-yard-layer]:visible').count() == 12
            assert no_js.locator('[data-yard-stage]:visible').count() == 0
            assert no_js.locator('.transfer-list > li').count() == 3
            assert no_js.get_by_role('heading', name='실행과 업무 완료 근거', exact=True).is_visible()
            no_js_context.close()
            result['checks'].append('No-JavaScript viewer can read original and all expected layouts, movement order and evidence requirements')
            visit(page, '/field/new/')
            submit(page, dict(order=inbound.pk, vehicle_id='browser-vehicle', assigned_to=users['field1'].pk))
            episode = FieldEpisode.objects.get(vehicle_id='browser-vehicle')
            field = browser.new_page(viewport={'width': 768, 'height': 1024})
            login(field, 'field1')
            visit(field, f'/field/{episode.pk}/')
            field.locator('[name=outcome][value=CLEAR]').check()
            capture(field, 'tablet-field.png')
            field.get_by_role('button', name='이 회차의 확인 결과 제출').click()
            field.wait_for_load_state('networkidle')
            assert FieldConfirmation.objects.filter(episode=episode, outcome='CLEAR').count() == 1
            visit(page, f'/field/{episode.pk}/')
            departure_data = dict(action='departure', confirmation_id=FieldConfirmation.objects.get(episode=episode).pk)
            page.get_by_role('button', name='같은 차량의 출발 전제 기록').click()
            page.wait_for_load_state('networkidle')
            assert post(page, f'/field/{episode.pk}/', departure_data).status == 200
            assert DepartureIntent.objects.filter(episode=episode).count() == 1
            visit(page, '/field/new/')
            submit(page, dict(order=inbound.pk, vehicle_id='browser-reassign', assigned_to=users['field1'].pk))
            stale = FieldEpisode.objects.get(vehicle_id='browser-reassign')
            visit(field, f'/field/{stale.pk}/')
            visit(page, f'/field/{stale.pk}/')
            page.get_by_text('확인 담당 재배정', exact=True).click()
            page.locator('select[name=assigned_to]').select_option(str(users['field2'].pk))
            page.get_by_role('button', name='새 회차 배정', exact=True).click()
            page.wait_for_load_state('networkidle')
            field.locator('[name=outcome][value=CLEAR]').check()
            with field.expect_response(lambda response: response.request.method == 'POST') as denied:
                field.get_by_role('button', name='이 회차의 확인 결과 제출').click()
            assert denied.value.status == 409
            capture(field, 'tablet-stale-error.png')
            other = browser.new_page()
            login(other, 'field2')
            assert other.goto(origin + f'/field/{episode.pk}/').status == 403
            assert post(other, f'/field/{episode.pk}/', dict(action='confirm', expected_version=1, outcome='CLEAR', observed_at=timezone.localtime().isoformat())).status == 403
            viewer = browser.new_page()
            login(viewer, 'viewer')
            visit(viewer, f'/requests/{outbound.pk}/')
            assert post(viewer, f'/requests/{outbound.pk}/', dict(action='plan', expected_version=1, observation=observed.pk)).status == 403
            result['checks'].append('UI assignment/CLEAR/departure once, separate OPEN reassignment/stale-tab 409, field direct URL/POST 403, viewer CSRF-authenticated POST 403')
            visit(page, '/records/measurements/new/')
            page.screenshot(path=str(output / 'desktop-measurement-form.png'), full_page=True)
            submit(page, dict(label='Browser collaboration', method='COLLABORATIVE', **measurement_fields))
            right = Measurement.objects.get(label='Browser collaboration')
            visit(page, '/records/compare/')
            page.locator('[name=left]').select_option(str(left.pk))
            page.locator('[name=right]').select_option(str(right.pk))
            page.get_by_role('button', name='같은 조건인지 확인하고 비교').click()
            page.wait_for_load_state('networkidle')
            assert page.locator('tbody tr').count() == 3
            assert page.locator('tbody tr').first.locator('td.number').all_text_contents() == ['미측정'] * 3
            comparison = services.compare_measurements(left, right)
            assert comparison['comparable'] and all(row['delta'] is None for row in comparison['primary'])
            result['checks'].append('UI measured record, matching comparison form and null primary metrics displayed as unmeasured')
            for name, width, height in [('desktop', 1440, 1000), ('desktop-1280', 1280, 800), ('tablet', 768, 1024), ('mobile', 390, 844)]:
                viewport = browser.new_page(viewport={'width': width, 'height': height}, reduced_motion='reduce')
                errors, failed, console, bad_http = [], [], [], []
                viewport.on('pageerror', lambda error: errors.append(str(error)))
                viewport.on('requestfailed', lambda request: failed.append(request.url))
                viewport.on('console', lambda message: console.append(message.text) if message.type == 'error' else None)
                viewport.on('response', lambda response: bad_http.append([response.status, response.url]) if response.status >= 400 else None)
                login(viewport, 'operator')
                assert viewport.get_by_role('heading', name='지금 필요한 조치').is_visible()
                styles = viewport.evaluate("({body:parseFloat(getComputedStyle(document.body).fontSize), heading:parseFloat(getComputedStyle(document.querySelector('h1')).fontSize)})")
                assert styles['body'] >= 16 and styles['heading'] >= 28, styles
                viewport.keyboard.press('Tab')
                assert viewport.evaluate('document.activeElement !== document.body')
                capture(viewport, name + '-overview.png')
                toggle = viewport.locator('#nav-toggle')
                if toggle.is_visible():
                    toggle.click()
                    assert toggle.get_attribute('aria-expanded') == 'true'
                    viewport.keyboard.press('Escape')
                    assert toggle.get_attribute('aria-expanded') == 'false'
                    assert toggle.evaluate('(e)=>e===document.activeElement')
                links = viewport.locator('#navigation nav a').evaluate_all('(links)=>links.map(a=>a.getAttribute("href"))')
                for path in links:
                    visit(viewport, path)
                    assert viewport.locator('h1').count() >= 1
                    assert not viewport.evaluate('document.documentElement.scrollWidth > innerWidth'), (name, path)
                visit(viewport, f'/plans/{plan.pk}/')
                assert not viewport.evaluate('document.documentElement.scrollWidth > innerWidth'), (name, 'workspace')
                capture(viewport, name + '-plan.png')
                assert viewport.locator('[data-yard-3d]').is_visible()
                grid = plan_flat(viewport)
                assert grid.locator('[data-grid-area="yard"] .grid-cell').count() == 9
                grid.locator('[data-grid-area="yard"] [data-linked-level="3"]').click()
                assert not viewport.evaluate('document.documentElement.scrollWidth > innerWidth'), (name, 'grid-level-3')
                capture(viewport, name + '-layer-3.png')
                grid.locator('[data-grid-area="yard"] [data-linked-level="1"]').click()
                viewport.locator('[data-yard-stage="3"]').click()
                assert grid.get_attribute('data-source') == 'predicted'
                assert grid.locator('[data-grid-area="stand"] [data-cargo-id="C1"]').is_visible()
                capture(viewport, name + '-expected.png')
                viewport.get_by_role('link', name='실행·근거', exact=True).click()
                assert viewport.locator('#execution-title').is_visible()
                capture(viewport, name + '-evidence.png')
                viewport.reload()
                assert viewport.locator('.transfer-list > li').count() == 3
                # The renderer is a dynamic ESM import; document load does not finish its mount.
                result.setdefault('reload_mount_states', []).append(viewport.locator('[data-yard-3d]').evaluate(
                    '(figure) => ({ready:figure.dataset.ready || null, hidden:figure.hidden})'))
                viewport.locator('[data-yard-3d][data-ready="true"]').wait_for()
                assert viewport.locator('[data-yard-3d]').is_visible()
                assert viewport.locator('canvas').get_attribute('data-frame') == '0'
                visit(viewport, '/requests/')
                viewport.go_back()
                assert f'/plans/{plan.pk}/' in viewport.url
                visit(viewport, '/cargo/new/')
                submit(viewport, dict(code='C1', name='Retained invalid input', weight_kg='1', volume_m3='1', destination='yard'))
                assert viewport.locator('[name=name]').input_value() == 'Retained invalid input'
                assert viewport.locator('.errorlist').count() >= 1
                capture(viewport, name + '-error.png')
                for control in viewport.locator('main input:not([type=hidden]), main select, main textarea').all():
                    assert control.evaluate('(e)=>e.labels && e.labels.length > 0')
                assert viewport.locator('img').evaluate_all('(images)=>images.every(i=>i.complete&&i.naturalWidth>0)')
                icon = viewport.locator('link[rel=icon]').get_attribute('href')
                assert viewport.request.get(origin + icon).status == 200
                assert not errors and not failed and not console and not bad_http, [errors, failed, console, bad_http]
                result['viewports'].append(dict(name=name, width=width, height=height, styles=styles, errors=errors, failed_requests=failed, console_errors=console, bad_http=bad_http, navigation_targets=len(links), overflow=False))
                viewport.get_by_role('button', name='로그아웃', exact=True).click()
                viewport.wait_for_load_state('networkidle')
                assert '/accounts/login/' in viewport.url
                visit(viewport, '/accounts/login/')
                assert viewport.goto(origin + '/requests/').url.startswith(origin + '/accounts/login/')
                viewport.close()
            admin_page = browser.new_page()
            admin_errors = []
            admin_page.on('pageerror', lambda error: admin_errors.append(str(error)))
            admin_page.on('console', lambda message: admin_errors.append(message.text) if message.type == 'error' else None)
            admin_page.on('requestfailed', lambda request: admin_errors.append(request.url + ': ' + str(request.failure)))
            login(admin_page, 'admin')
            visit(admin_page, '/settings/')
            visit(admin_page, '/admin/auth/user/add/')
            admin_page.locator('#id_username').fill('browser-added-user')
            admin_page.locator('#id_password1').fill(password)
            admin_page.locator('#id_password2').fill(password)
            admin_page.locator('input[name="_save"]').click()
            admin_page.wait_for_load_state('networkidle')
            added = User.objects.get(username='browser-added-user')
            visit(admin_page, f'/admin/auth/user/{added.pk}/change/')
            admin_page.locator('#id_groups_from').select_option(str(Group.objects.get(name='viewer').pk))
            admin_page.locator('#id_groups_add').click()
            admin_page.locator('input[name="_save"]').click()
            admin_page.wait_for_load_state('networkidle')
            assert added.groups.filter(name='viewer').exists()
            assert not added.groups.filter(name='operator').exists()
            assert not admin_errors, admin_errors
            visit(admin_page, '/outbound/')
            from navigation_browser_checks import exercise_navigation
            exercise_navigation(admin_page)
            result['checks'].append('Icon navigation: hover/focus tooltip, hoverable content, Escape, density persistence, tablet/mobile labels')
            result['checks'].append('Staff plus explicit admin creates a team account and assigns viewer via real admin UI; no implicit operator; no admin CSP/console errors')
            assert page.request.get(origin + '/settings/').status == 403
            result['checks'].append('Four viewports, nav/Escape/focus, styles, labels, direct/reload/back, invalid retention, images/favicon, server role checks and POST logout')
            browser.close()
        result['status'] = 'PASSED'
    except Exception as error:
        result['failure'] = str(error)
        result['traceback'] = traceback.format_exc()
        try:
            page.screenshot(path=str(output / 'failure.png'), full_page=True)
        except Exception:
            pass
    finally:
        if child is not None and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)
        log.close()
        (output / 'report.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0 if result['status'] == 'PASSED' else 1


if __name__ == '__main__':
    raise SystemExit(main())
