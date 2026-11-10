"""Linked logical selection on the real multicell browser fixture; no server writes."""
import json


def exercise_occupied_location(page, stage, cargo_id, position):
    """A selected empty address becomes its occupant, including covered non-anchor cells."""
    preview = page.locator('[data-yard-preview]')
    grid = page.locator('[data-yard-linked-flat]')
    scene_id = page.locator('[data-yard-3d]').get_attribute('data-scene')
    source = page.locator('#' + scene_id).text_content()
    posts = []
    def observe(request):
        if request.method == 'POST':
            posts.append(request.url)
    page.on('request', observe)
    page.locator('[data-yard-stage="0"]').click()
    page.locator('[data-scene-animate]').uncheck()
    if grid.is_hidden():
        page.locator('[data-yard-view="flat"]').click()
    grid.locator(f'[data-grid-area="{position["area"]}"] [data-linked-level="{position["level"]}"]').click()
    cells = grid.locator('[data-selection]')
    chosen = next(index for index, value in enumerate(cells.evaluate_all('(nodes) => nodes.map(n => JSON.parse(n.dataset.selection))'))
        if all(value[key] == value_at_position for key, value_at_position in position.items()))
    cells.nth(chosen).click()
    assert page.locator('[data-scene-cargo]').input_value() == ''
    assert json.loads(page.locator('canvas').get_attribute('data-location-marker'))['column'] == position['column']
    page.locator(f'[data-yard-stage="{stage}"]').click()
    identity = json.loads(preview.get_attribute('data-selection'))
    assert identity['cargoId'] == cargo_id, identity
    assert position in identity['coveredPositions'], identity
    assert page.locator('[data-scene-cargo]').input_value() == cargo_id
    assert grid.locator(f'[data-cargo-id="{cargo_id}"]').get_attribute('aria-pressed') == 'true'
    assert page.locator('canvas').get_attribute('data-location-marker') == ''
    assert '빈 위치' not in page.locator('[data-scene-info]').inner_text()
    assert page.locator('#' + scene_id).text_content() == source and posts == []
    page.remove_listener('request', observe)
    page.locator('[data-yard-stage="0"]').click()
    if page.locator('[data-yard-3d]').is_hidden():
        page.locator('[data-yard-view="3d"]').click()


def exercise_linked_views(page, output):
    page.set_viewport_size(dict(width=1440, height=1000))
    page.locator('[data-yard-stage="0"]').click()
    page.locator('[data-scene-animate]').uncheck()
    figure = page.locator('[data-yard-3d]')
    grid = page.locator('[data-yard-linked-flat]')
    preview = page.locator('[data-yard-preview]')
    cargo = page.locator('[data-scene-cargo]')
    canvas = figure.locator('canvas')
    source = page.locator('#yard-scene-data').text_content()
    posts = []
    page.on('request', lambda request: posts.append(request.url) if request.method == 'POST' else None)

    assert figure.is_visible() and grid.is_visible(), 'Desktop keeps both views visible'
    grid.locator('[data-grid-area="yard"] [data-linked-level="1"]').click()
    wide = grid.locator('[data-cargo-id="C1"]')
    wide.focus()
    page.keyboard.press('Enter')
    assert cargo.input_value() == 'C1'
    identity = json.loads(preview.get_attribute('data-selection'))
    assert identity['cargoId'] == 'C1' and identity['area'] == 'yard'
    assert len(identity['coveredPositions']) == 2
    assert wide.locator('..').get_attribute('colspan') == '2'
    assert 'Y11-L1' in wide.inner_text() and 'Y12-L1' in wide.inner_text()
    assert wide.evaluate('(b) => b === document.activeElement'), 'Keyboard selection preserves focus'

    # A real projected hit, after framing one cargo, must select the matching floor.
    cargo.select_option('C2')
    page.locator('[data-scene-action="focus"]').click()
    page.locator('[data-scene-action="isolate"]').click()
    cargo.select_option('')
    # Clearing selection clears isolation, so reselect before clicking the framed box.
    cargo.select_option('C2')
    page.locator('[data-scene-action="isolate"]').click()
    rect = canvas.bounding_box()
    canvas.click(position=dict(x=rect['width'] / 2, y=rect['height'] / 2))
    assert cargo.input_value() == 'C2', 'Real 3D hit selects C2'
    assert grid.locator('[data-grid-area="yard"] [data-linked-level="2"]').get_attribute('aria-pressed') == 'true'
    assert grid.locator('[data-cargo-id="C2"]').get_attribute('aria-pressed') == 'true'
    page.locator('[data-scene-action="isolate"]').click()

    grid.locator('[data-grid-area="yard"] [data-linked-level="1"]').click()
    empty = grid.locator('[data-grid-area="yard"] [data-cargo-id=""]').first
    expected_empty = json.loads(empty.get_attribute('data-selection'))
    empty.click()
    assert cargo.input_value() == ''
    assert json.loads(canvas.get_attribute('data-location-marker')) == expected_empty
    assert '화물 아님' in page.locator('[data-scene-info]').inner_text()
    assert canvas.get_attribute('data-box-count') == '6', 'Location marker never becomes fake cargo'

    exercise_occupied_location(page, 2, 'C1', dict(area='stand', row=1, column=2, level=1))

    cargo.select_option('C2')
    page.locator('[data-yard-stage="1"]').click()
    moved = json.loads(preview.get_attribute('data-selection'))
    assert moved['cargoId'] == 'C2' and moved['area'] == 'temp_a' and moved['level'] == 1
    assert grid.get_attribute('data-source') == 'predicted'
    assert grid.locator('[data-grid-area="temp_a"] [data-cargo-id="C2"]').get_attribute('aria-pressed') == 'true'

    page.locator('[data-yard-stage="0"]').click()
    page.locator('[data-scene-action="edit"]').click()
    cargo.select_option('C2')
    editor = page.locator('[data-scene-editor]')
    editor.locator('[name=area]').select_option('temp_a')
    for name in ('row', 'column', 'level'):
        editor.locator(f'[name={name}]').fill('1')
    editor.locator('button[type=submit]').click()
    assert not page.locator('[data-scene-error]').inner_text()
    assert grid.get_attribute('data-source') == 'local-draft'
    assert grid.locator('[data-grid-area="temp_a"] [data-cargo-id="C2"]').count() == 1
    assert grid.locator('[data-grid-area="yard"] [data-cargo-id="C2"]').count() == 0
    page.screenshot(path=str(output / 'linked-desktop-draft.png'), full_page=True)
    assert page.locator('#yard-scene-data').text_content() == source

    page.set_viewport_size(dict(width=390, height=844))
    page.locator('[data-yard-view="flat"]').click()
    assert grid.is_visible() and figure.is_visible()
    assert page.locator('.model-viewport').is_hidden() and editor.is_visible(), 'Flat editing keeps its controls available'
    assert grid.get_attribute('data-source') == 'local-draft'
    grid.locator('[data-cargo-id="C2"]').click()
    page.locator('[data-yard-view="3d"]').click()
    assert cargo.input_value() == 'C2'
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), 'Mobile has no page overflow'
    page.screenshot(path=str(output / 'linked-mobile-draft.png'), full_page=True)
    page.locator('[data-scene-action="edit"]').click()
    assert grid.get_attribute('data-source') == 'original'
    assert json.loads(preview.get_attribute('data-selection'))['area'] == 'yard'
    assert page.locator('#yard-scene-data').text_content() == source
    # Switching views during a real move must settle the selected endpoint.
    page.locator('[data-scene-animate]').check()
    page.locator('[data-yard-stage="1"]').click()
    page.wait_for_function('''() => {
      const progress = document.querySelector('[data-scene-progress]');
      return document.querySelector('canvas').dataset.moving === 'true' &&
        Number(progress.dataset.progress) > 15 && Number(progress.dataset.progress) < 80;
    }''')
    moving = page.locator('[data-scene-progress]').get_attribute('data-cargo')
    endpoint = next(box for box in json.loads(source)['frames'][1]['boxes'] if box['id'] == moving)
    page.locator('[data-yard-view="flat"]').click()
    page.locator('[data-yard-view="3d"]').click()
    page.wait_for_function('''([id, endpoint]) => {
      const canvas = document.querySelector('canvas');
      const rendered = JSON.parse(canvas.dataset.renderedPositions || '{}')[id];
      return canvas.dataset.moving === 'false' && rendered &&
        ['x','y','z'].every(key => rendered[key] === endpoint[key]);
    }''', arg=[moving, endpoint])
    assert grid.get_attribute('data-source') == 'predicted'
    assert json.loads(preview.get_attribute('data-selection'))['area'] == 'temp_a'
    page.locator('[data-yard-stage="0"]').click()
    assert page.locator('#yard-scene-data').text_content() == source
    assert posts == [], posts
    page.set_viewport_size(dict(width=1440, height=1000))
    page.locator('[data-scene-animate]').check()
    return ['Linked desktop/mobile: keyboard 2D→3D, real projected 3D→2D, exact multicell addresses, empty marker, stage/draft reconciliation and midair view-switch endpoint; source unchanged and zero POST']


def exercise_unobserved_selection(page, output):
    page.set_viewport_size(dict(width=1440, height=1000))
    page.locator('[data-yard-3d][data-ready=true]').wait_for()
    grid = page.locator('[data-yard-linked-flat]')
    source = page.locator('#yard-scene-data').text_content()
    posts = []
    page.on('request', lambda request: posts.append(request.url) if request.method == 'POST' else None)
    assert grid.get_attribute('data-source') == 'map-definition'
    location = grid.locator('[data-selection]').first
    expected = json.loads(location.get_attribute('data-selection'))
    assert '미관측 위치' in location.inner_text() and '빈 위치' not in location.inner_text()
    location.click()
    assert json.loads(page.locator('canvas').get_attribute('data-location-marker')) == expected
    for selector in ('[data-scene-info]', '[data-scene-motion]'):
        text = page.locator(selector).text_content()
        assert '미관측 위치' in text and '점유 확인 안 됨' in text and '빈 위치' not in text
    assert page.locator('[data-scene-motion]').is_visible()
    assert page.locator('canvas').get_attribute('data-box-count') == '0'
    assert page.locator('#yard-scene-data').text_content() == source and posts == []
    page.screenshot(path=str(output / 'unobserved-location-selection.png'), full_page=True)
    return ['Map-definition location marker and visible status retain unobserved occupancy; no fake cargo, source change or POST']
