"""Isolated browser acceptance for atomic multi-cell cargo, without Fleet/device access."""
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
    from harbor_web.models import Cargo, CargoFootprint, Command, Plan
    from playwright.sync_api import sync_playwright

    call_command('migrate', verbosity=0)
    call_command('bootstrap_roles', verbosity=0)
    password = secrets.token_urlsafe(30)
    operator = User.objects.create_user('spatial-operator', password=password)
    admin = User.objects.create_user('spatial-admin', password=password)
    operator.groups.add(Group.objects.get(name='operator'))
    admin.groups.add(Group.objects.get(name='admin'))
    shapes = {}
    for code, length in [('C1', 2), ('C2', 1), ('C3', 1), ('C4', 1), ('C5', 3), ('C6', 1), ('C7', 1)]:
        cargo = services.register_cargo(operator, code=code, name='합성 점유 화물 ' + code,
            length_cells=length, weight_kg=1, volume_m3=1, destination='합성 반출대')
        shapes[code] = CargoFootprint.objects.get(cargo=cargo)
    policy = json.loads((ROOT / 'config/spatial-policy.example.json').read_text(encoding='utf-8'))
    policy['yard_layout'] = json.loads((ROOT / 'config/yard-layout.json').read_text(encoding='utf-8'))
    condition = services.register_condition(admin, name='합성 다칸 관측', calibration_ref='synthetic-only', payload=policy)
    placements = [dict(cargo_id=code, area_id='yard', row=row, column=column, level=level,
        orientation=0, footprint_revision=shapes[code].pk) for code, row, column, level in [
            ('C1', 1, 1, 1), ('C2', 1, 1, 2), ('C4', 2, 3, 1), ('C5', 3, 1, 1), ('C6', 3, 2, 2), ('C7', 3, 2, 3)]]
    observed = services.record_observation(admin, source_id='spatial-synthetic', source_revision='one',
        observed_at=timezone.now(), provenance='SYNTHETIC', condition=condition,
        payload=dict(schema='grid-placement-v1', observed_areas=['yard', 'temp_a', 'temp_b', 'stand'], placements=placements, holdings=[]))
    order = services.create_order(operator, cargo=Cargo.objects.get(code='C1'), kind='OUTBOUND',
        priority_group=1, sequence=1, tie_policy='FIXED', note='합성 다칸 반출 지시')
    with socket.socket() as allocated:
        allocated.bind(('127.0.0.1', 0))
        port = allocated.getsockname()[1]
    origin = f'http://127.0.0.1:{port}'
    result = dict(status='FAILED', output=str(output), checks=[], viewports=[], actual_fleet=False)
    child = None
    log = (output / 'server.log').open('w', encoding='utf-8')
    try:
        child = subprocess.Popen([sys.executable, '-m', 'harbor_web', '--port', str(port)], cwd=ROOT,
            env=os.environ.copy(), stdout=log, stderr=log)
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
            page = browser.new_page(viewport={'width': 1440, 'height': 1000})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.on('console', lambda msg: errors.append(msg.text) if msg.type == 'error' else None)
            page.on('requestfailed', lambda request: errors.append(request.url))
            page.on('response', lambda response: errors.append(f'{response.status}:{response.url}') if response.status >= 400 else None)
            page.goto(origin + '/accounts/login/')
            page.locator('[name=username]').fill(operator.username)
            page.locator('[name=password]').fill(password)
            page.locator('main button[type=submit]').click()
            page.wait_for_load_state('networkidle')
            page.goto(origin + f'/inventory/?observation={observed.pk}')
            page.locator(f'a[href="/requests/?kind=OUTBOUND&observation={observed.pk}"]').click()
            page.locator(f'a[href="/requests/{order.pk}/?observation={observed.pk}"]').click()
            assert page.locator('[name=observation]').input_value() == str(observed.pk)
            result['checks'].append('Selected inventory source survives request list and workspace; no silent latest-source fallback')
            page.locator('[name=observation]').select_option(str(observed.pk))
            page.get_by_role('button', name='반출 계획 만들기', exact=True).click()
            page.wait_for_load_state('networkidle')
            plan = Plan.objects.get(order=order)
            assert plan.result['status'] == 'READY' and len(plan.result['moves']) == 3, plan.result
            assert [m['cargo_id'] for m in plan.result['moves']] == ['C2', 'C1', 'C2']
            plan_path = f'/plans/{plan.pk}/'
            result['checks'].append('Actual browser creates complete atomic spatial plan: blocker, target, restore; three cargo moves, never per-cell moves')
            from tools.integration.candidate_browser_checks import exercise_candidate_preview
            result['checks'].extend(exercise_candidate_preview(page, origin, plan, output))
            from tools.integration.direct_placement_browser_checks import exercise_direct_placement
            result['checks'].extend(exercise_direct_placement(page, origin, plan, output))
            page.locator('[data-yard-3d][data-ready=true]').wait_for()
            assert page.locator('canvas[data-box-count="6"]').count() == 1
            page.locator('[data-scene-cargo]').select_option('C1')
            assert '2×1' in page.locator('[data-scene-info]').inner_text()
            assert 'Y12-L1' in page.locator('[data-scene-info]').inner_text()
            before_rotation = page.locator('canvas').evaluate('(c)=>c.toDataURL()')
            page.locator('canvas').focus()
            page.keyboard.press('ArrowLeft')
            page.wait_for_function('(before) => document.querySelector("canvas").toDataURL() !== before', arg=before_rotation)
            page.keyboard.press('Home')
            page.locator('[data-scene-cargo]').select_option('')
            canvas = page.locator('canvas')
            rect = canvas.bounding_box()
            for y in range(100, int(rect['height']) - 30, 30):
                for x in range(60, int(rect['width']) - 30, 35):
                    canvas.click(position=dict(x=x, y=y))
                    if page.locator('[data-scene-cargo]').input_value():
                        break
                if page.locator('[data-scene-cargo]').input_value():
                    break
            assert page.locator('[data-scene-cargo]').input_value(), 'Projected cuboid selection did not hit any cargo'
            page.locator('[data-scene-play]').click()
            page.wait_for_function('() => document.querySelector("canvas").dataset.frame === "3" && document.querySelector("canvas").dataset.moving === "false"')
            page.wait_for_function('() => document.querySelector("[data-scene-play]").getAttribute("aria-pressed") === "false"')
            assert page.locator('[data-yard-stage="3"]').get_attribute('aria-pressed') == 'true'
            assert page.locator('canvas').get_attribute('data-box-count') == '6'
            page.locator('[data-yard-stage="0"]').click()
            page.locator('[data-scene-play]').click()
            page.wait_for_function('() => document.querySelector("canvas").dataset.moving === "true"')
            page.locator('[data-scene-play]').click()
            assert page.locator('canvas').get_attribute('data-moving') == 'false'
            assert page.locator('[data-scene-play]').get_attribute('aria-pressed') == 'false'
            page.locator('[data-scene-play]').click()
            page.wait_for_function('() => document.querySelector("canvas").dataset.moving === "true"')
            page.locator('[data-yard-stage="2"]').click()
            assert page.locator('[data-scene-play]').get_attribute('aria-pressed') == 'true'
            page.wait_for_function('() => document.querySelector("canvas").dataset.frame === "2" && document.querySelector("canvas").dataset.moving === "false"')
            page.locator('[data-plan-sequence]').click()
            page.wait_for_function('() => document.querySelector("canvas").dataset.moving === "true"')
            assert page.locator('[data-scene-play]').get_attribute('aria-pressed') == 'true'
            page.locator('[data-scene-play]').click()
            assert page.locator('canvas').get_attribute('data-moving') == 'false'
            assert page.locator('[data-scene-play]').get_attribute('aria-pressed') == 'false'
            result['checks'].append('One sequence button replays the stored plan from the observed layout; manual motion can be stopped')
            result['checks'].append('3D Canvas renders six atomic cuboids; footprint selection, real projected hit selection, keyboard camera and complete explicit playback stay synchronized')
            result['checks'].append('Explicit stop snaps to selected endpoint; manual step interrupts automatic playback without another step')
            from tools.integration.linked_yard_browser_checks import exercise_linked_views
            result['checks'].extend(exercise_linked_views(page, output))
            unobserved = services.register_condition(admin, name='Synthetic unobserved selection',
                calibration_ref='synthetic-only', payload=policy)
            page.goto(origin + f'/inventory/?condition={unobserved.pk}')
            from tools.integration.linked_yard_browser_checks import exercise_unobserved_selection
            result['checks'].extend(exercise_unobserved_selection(page, output))
            page.goto(origin + plan_path)
            page.locator('[data-yard-3d][data-ready=true]').wait_for()
            from tools.integration.workbench_browser_checks import exercise_workbench
            result['checks'].extend(exercise_workbench(page, output))
            revised = services.make_plan(operator, order_id=order.pk, expected_version=1, observation_id=observed.pk)
            page.goto(origin + f'/plans/{revised.pk}/')
            page.locator('[data-changes-only]').click()
            assert page.locator('[data-plan-change]:visible').count() == 0
            assert page.locator('[data-no-changes]').is_visible()
            page.locator('[data-changes-only]').click()
            assert page.locator('[data-plan-change]:visible').count() == 2
            result['checks'].append('Unchanged cargo can be hidden and restored; empty changed-only result is explained')
            page.goto(origin + plan_path)
            for width, height in [(1440, 1000), (1280, 800), (768, 1024), (390, 844)]:
                page.set_viewport_size(dict(width=width, height=height))
                page.goto(origin + plan_path)
                page.wait_for_load_state('networkidle')
                page.locator('[data-yard-3d][data-ready=true]').wait_for()
                assert page.locator('canvas').is_visible()
                assert not page.evaluate('document.documentElement.scrollWidth > innerWidth'), width
                page.screenshot(path=str(output / f'{width}-3d.png'), full_page=True)
                if width < 1200:
                    page.locator('[data-yard-view="flat"]').click()
                source = page.locator('[data-yard-linked-flat]')
                yard = source.locator('[data-grid-area="yard"]')
                first = yard.locator('table')
                assert first.locator('[data-cargo-id="C1"]').count() == 1
                assert first.locator('[data-cargo-id="C1"]').locator('..').get_attribute('colspan') == '2'
                assert first.locator('[data-cargo-id="C5"]').count() == 1
                assert first.locator('[data-cargo-id="C5"]').locator('..').get_attribute('colspan') == '3'
                assert first.locator('.grid-cell').evaluate_all('(cells)=>cells.reduce((n,c)=>n+c.colSpan*c.rowSpan,0)') == 9
                assert page.locator('form[data-command]').count() == 0
                assert not page.evaluate('document.documentElement.scrollWidth > innerWidth'), width
                page.screenshot(path=str(output / f'{width}-source.png'), full_page=True)
                yard.locator('[data-linked-level="2"]').focus()
                page.keyboard.press('Enter')
                page.locator('[data-yard-stage="1"]').click()
                step = page.locator('[data-yard-linked-flat]')
                assert step.locator('[data-grid-area="yard"] [data-linked-level="2"]').get_attribute('aria-pressed') == 'true'
                assert step.locator('[data-grid-area^="temp_"] [data-cargo-id="C2"]').is_visible()
                page.locator('[data-yard-stage="2"]').click()
                step = page.locator('[data-yard-linked-flat]')
                assert step.locator('[data-grid-area="yard"] [data-cargo-id="C1"]').count() == 0
                assert step.locator('[data-grid-area="stand"] [data-cargo-id="C1"]').locator('..').get_attribute('colspan') == '2'
                page.locator('[data-yard-stage="3"]').click()
                step = page.locator('[data-yard-linked-flat]')
                step.locator('[data-grid-area="yard"] [data-linked-level="1"]').click()
                assert step.locator('[data-grid-area="yard"] [data-cargo-id="C2"]').count() == 1
                assert step.locator('[data-grid-area="stand"] [data-cargo-id="C1"]').count() == 1
                page.screenshot(path=str(output / f'{width}-expected.png'), full_page=True)
                assert not page.evaluate('document.documentElement.scrollWidth > innerWidth'), width
                result['viewports'].append(dict(width=width, height=height, overflow=False))
                page.goto(origin + '/workflow/')
                assert page.locator('.journey-list > li').count() == 5
                assert not page.evaluate('document.documentElement.scrollWidth > innerWidth'), width
                page.screenshot(path=str(output / f'{width}-workflow.png'), full_page=True)
            result['checks'].append('Four widths: merged 2x1 and3x1 identity, coverage9, keyboard layer retention, independent auxiliary layers, atomic outbound/restoration, no overflow')
            no_js_context = browser.new_context(java_script_enabled=False)
            no_js_context.add_cookies(page.context.cookies())
            no_js = no_js_context.new_page()
            no_js.goto(origin + plan_path)
            assert no_js.locator('[data-yard-panel]:visible').count() == 4
            assert no_js.locator('[data-cargo-id="C1"]').count() == 4
            no_js_context.close()
            reduced_context = browser.new_context(reduced_motion='reduce')
            reduced_context.add_cookies(page.context.cookies())
            reduced_page = reduced_context.new_page()
            reduced_page.goto(origin + plan_path)
            reduced_page.locator('[data-yard-3d][data-ready=true]').wait_for()
            reduced_page.locator('[data-yard-stage="1"]').click()
            assert reduced_page.locator('canvas').get_attribute('data-frame') == '1'
            assert reduced_page.locator('canvas').get_attribute('data-moving') == 'false'
            from tools.integration.workbench_browser_checks import exercise_reduced_playback
            result['checks'].extend(exercise_reduced_playback(reduced_page, output))
            reduced_context.close()
            incoming = services.create_order(operator, cargo=Cargo.objects.get(code='C3'), kind='INBOUND',
                priority_group=9, sequence=1, tie_policy='FIXED')
            inbound_plan = services.make_plan(operator, order_id=incoming.pk, expected_version=1,
                observation_id=observed.pk, automatic=True)
            assert inbound_plan.result['status'] == 'READY', inbound_plan.result
            page.goto(origin + f'/plans/{inbound_plan.pk}/')
            page.locator('[data-yard-3d][data-ready=true]').wait_for()
            from tools.integration.workbench_browser_checks import exercise_inbound_comparison
            result['checks'].extend(exercise_inbound_comparison(page, output))
            page.goto(origin + plan_path)
            unsupported = browser.new_context()
            unsupported.add_cookies(page.context.cookies())
            unsupported.add_init_script('HTMLCanvasElement.prototype.getContext = () => null;')
            fallback_page = unsupported.new_page()
            fallback_page.goto(origin + plan_path)
            fallback_page.locator('[data-yard-3d-fallback]').wait_for(state='visible')
            assert fallback_page.locator('[data-yard-panel="0"]').is_visible()
            unsupported.close()
            lost_context = browser.new_context()
            lost_context.add_cookies(page.context.cookies())
            lost_page = lost_context.new_page()
            lost_page.goto(origin + plan_path)
            lost_page.locator('[data-yard-3d][data-ready=true]').wait_for()
            if lost_page.locator('[data-scene-action="fullscreen"]').is_visible():
                lost_page.locator('[data-scene-action="fullscreen"]').click()
                lost_page.wait_for_function('() => Boolean(document.fullscreenElement)')
            assert lost_page.locator('canvas').evaluate('(c) => { const e=c.getContext("webgl2").getExtension("WEBGL_lose_context"); if(!e)return false; e.loseContext(); return true; }')
            lost_page.locator('[data-yard-3d-fallback]').wait_for(state='visible')
            lost_page.wait_for_function('() => !document.fullscreenElement')
            assert lost_page.locator('[data-yard-panel="0"]').is_visible()
            lost_context.close()
            result['checks'].append('Lost WebGL context exits fullscreen, releases viewer and restores original plane view')
            result['checks'].append('Reduced-motion stage changes are instant; Canvas-unavailable and no-JS preserve original readable plane view')
            page.goto(origin + '/inventory/')
            assert page.locator('[data-grid-area="yard"] [data-yard-layer="1"] [data-cargo-id="C1"]').get_attribute('colspan') == '2'
            page.goto(origin + '/cargo/new/')
            for name, value in dict(code='UI-LONG', name='화면 등록3칸 화물', weight_kg='1', volume_m3='1', destination='합성').items():
                page.locator(f'[name={name}]').fill(value)
            page.locator('[name=length_cells]').select_option('3')
            page.locator('main button[type=submit]').click()
            page.wait_for_load_state('networkidle')
            assert CargoFootprint.objects.get(cargo__code='UI-LONG').length_cells == 3
            page.goto(origin + f'/cargo/{order.cargo_id}/footprint/')
            page.locator('[name=length_cells]').select_option('3')
            page.locator('main button[type=submit]').click()
            page.wait_for_load_state('networkidle')
            assert services.spatial_snapshot(observed).shape('C1').length_cells == 2
            assert CargoFootprint.objects.filter(cargo=order.cargo).count() == 2
            observed.refresh_from_db()
            assert observed.payload['placements'] == placements and not Command.objects.exists()
            assert not errors, errors
            result['checks'].append('No-JS all stages, shared inventory, native cargo footprint form, immutable shape revision change, original observation unchanged, no execution intent')
            rotated_payload = json.loads(json.dumps(observed.payload))
            rotated_payload['placements'][0]['orientation'] = 90
            rotated = services.record_observation(admin, source_id='spatial-rotated', source_revision='one',
                observed_at=timezone.now(), provenance='SYNTHETIC', condition=condition, payload=rotated_payload)
            page.goto(origin + f'/requests/{order.pk}/')
            page.locator('[name=observation]').select_option(str(rotated.pk))
            page.get_by_role('button', name='반출 계획 만들기', exact=True).click()
            page.wait_for_load_state('networkidle')
            held_plan = Plan.objects.filter(order=order).latest('pk')
            assert held_plan.result['status'] == 'HOLD' and held_plan.result['moves'] == []
            assert held_plan.result['projected_placements'] is None
            cell = page.locator('[data-yard-panel="0"] [data-grid-area="yard"] [data-yard-layer="1"] [data-cargo-id="C1"]')
            assert cell.count() == 1 and cell.get_attribute('rowspan') == '2' and cell.get_attribute('colspan') == '1'
            assert page.locator('[data-yard-stage]').count() == 0
            assert not page.evaluate('document.documentElement.scrollWidth > innerWidth')
            page.screenshot(path=str(output / 'rotated-fit-hold.png'), full_page=True)
            assert not errors, errors
            result['checks'].append('Declared90deg source renders one rowspan cargo; fixed orientation cannot fit 1x3 stand and HOLDs without partial moves or projection')
            result['status'] = 'PASSED'
            result['errors'] = errors
            browser.close()
    except Exception:
        result['traceback'] = traceback.format_exc()
    finally:
        if child is not None:
            child.terminate()
            child.wait(timeout=10)
        log.close()
        (output / 'report.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    # Keep redirected Windows console output safe; the report file remains UTF-8.
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0 if result['status'] == 'PASSED' else 1


if __name__ == '__main__':
    raise SystemExit(main())
