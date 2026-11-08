"""Exercise the real linked-grid editor and saved candidate import."""
import json


def exercise_direct_placement(page, origin, plan, output):
    from harbor_web.models import Audit, Command, Observation, Plan
    models = (Plan, Observation, Audit, Command)
    counts = [model.objects.count() for model in models]
    saved = json.loads(json.dumps(plan.result))
    page.set_viewport_size(dict(width=1440, height=1000))
    page.goto(origin + f'/plans/{plan.pk}/candidates/2/')
    figure = page.locator('[data-yard-3d][data-ready=true]')
    figure.wait_for()
    raw = page.locator('#yard-scene-data').text_content()
    toolbar = page.locator('[data-yard-edit-tools]')
    assert toolbar.is_visible()
    edit = toolbar.locator('[data-scene-action="edit"]')
    undo = toolbar.locator('[data-scene-undo]')
    redo = toolbar.locator('[data-scene-redo]')
    cargo = page.locator('[data-scene-cargo]')
    posts = []
    def observe(request):
        if request.method == 'POST':
            posts.append(request.url)
    page.on('request', observe)

    def cell(area, row, column):
        return page.locator(f'[data-yard-linked-flat] [data-grid-area="{area}"] [data-destination="{row},{column}"]')

    edit.click()
    cargo.select_option('C1')
    assert cell('yard', 2, 1).get_attribute('data-drop-state') == 'invalid'
    cell('yard', 2, 1).click()
    assert cell('yard', 2, 1).evaluate('(node)=>node===document.activeElement'), 'Rejected destination keeps focus'
    assert '지지' in page.locator('[data-scene-error]').inner_text()
    assert cargo.input_value() == 'C1' and figure.get_attribute('data-draft-changes') == '0'
    cargo.select_option('C2')
    assert cell('temp_a', 1, 2).get_attribute('data-drop-state') == 'valid'
    cell('temp_a', 1, 2).focus()
    page.keyboard.press('Enter')
    assert cell('temp_a', 1, 2).evaluate('(node)=>node===document.activeElement'), 'Keyboard placement keeps focus on the moved cargo'
    assert page.locator('[data-scene-editor] [name=column]').input_value() == '2'
    assert figure.get_attribute('data-draft-changes') == '1'
    scene = json.loads(raw)
    destination_address = next(area for area in scene['areas'] if area['id']=='temp_a')['addresses']['1,2,1']
    page.locator('[data-scene-change-panel] summary').click()
    assert destination_address in page.locator('[data-scene-changes]').inner_text()
    undo.click(); assert figure.get_attribute('data-draft-changes') == '0'
    redo.click(); assert figure.get_attribute('data-draft-changes') == '1'
    cargo.select_option('C1')
    cell('stand', 1, 3).click()
    assert '경계' in page.locator('[data-scene-error]').inner_text()
    cell('stand', 1, 1).click()
    assert figure.get_attribute('data-draft-changes') == '2'
    undo.click()
    cell('yard', 2, 1).click()
    assert redo.is_disabled(), 'New move after undo must discard the redo branch'
    page.evaluate('scrollTo(0,0)')
    page.screenshot(path=str(output / 'direct-placement-desktop.png'), full_page=True)
    edit.click()
    toolbar.locator('[data-scene-load-plan]').click()
    assert figure.get_attribute('data-editing') == 'true'
    expected = json.loads(raw)['frames'][-1]['boxes']
    page.wait_for_function('''expected => {
      const actual=JSON.parse(document.querySelector('canvas').dataset.renderedPositions || '{}');
      return expected.every(b => ['x','y','z'].every(k => actual[b.id]?.[k] === b[k]));
    }''', arg=expected)
    assert '저장된 이동' in page.locator('[data-scene-motion]').inner_text()
    assert undo.is_enabled() and redo.is_disabled()
    undo.click(); assert redo.is_enabled()
    redo.click()
    page.evaluate('scrollTo(0,0)')
    page.screenshot(path=str(output / 'recommended-placement-edit.png'), full_page=True)
    edit.click()
    page.set_viewport_size(dict(width=390, height=844))
    page.locator('[data-yard-view="flat"]').click()
    assert not figure.is_visible() and toolbar.is_visible()
    edit.click()
    assert figure.is_visible() and not page.locator('.model-viewport').is_visible()
    assert page.locator('[data-scene-editor]').is_visible()
    page.locator('[data-linked-cargo]').select_option('C2')
    cell('temp_a', 1, 2).click()
    assert figure.get_attribute('data-draft-changes') == '1'
    assert cargo.input_value() == 'C2'
    assert page.locator('[data-yard-linked-flat]').bounding_box()['y'] < page.locator('[data-scene-editor]').bounding_box()['y']
    assert not page.evaluate('document.documentElement.scrollWidth>innerWidth')
    page.evaluate('scrollTo(0,0)')
    page.screenshot(path=str(output / 'direct-placement-mobile-flat.png'), full_page=True)
    edit.click()
    assert not figure.is_visible()
    assert page.locator('#yard-scene-data').text_content() == raw
    plan.refresh_from_db()
    assert plan.result == saved and counts == [model.objects.count() for model in models]
    assert posts == []
    page.remove_listener('request', observe)
    page.set_viewport_size(dict(width=1440, height=1000))
    page.goto(origin + f'/plans/{plan.pk}/')
    page.locator('[data-yard-3d][data-ready=true]').wait_for()
    return ['Direct grid placement validates support/footprint, keeps cargo selected, shows canonical changes, undo/redo branches, imports saved candidate path, and edits mobile flat view without POST or source mutation']
