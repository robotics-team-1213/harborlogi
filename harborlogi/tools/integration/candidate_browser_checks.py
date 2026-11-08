"""Inspect saved alternatives with the shared viewer; never mutate the plan."""
import json


def exercise_candidate_preview(page, origin, plan, output):
    from harbor_web.models import Audit, Command, Observation, Plan
    saved = json.loads(json.dumps(plan.result))
    alternative = saved['optimization']['alternatives'][1]
    assert alternative['moves'] != saved['moves']
    counts = [model.objects.count() for model in (Plan, Observation, Audit, Command)]
    posts = []
    def observe(request):
        if request.method == 'POST':
            posts.append(request.url)
    page.on('request', observe)
    page.locator('.planning-recommendation > details > summary').first.click()
    page.locator(f'a[href="/plans/{plan.pk}/candidates/2/"]').click()
    page.wait_for_load_state('networkidle')
    page.locator('[data-yard-3d][data-ready=true]').wait_for()
    assert page.get_by_role('heading', level=1).inner_text() == '저장된 후보 2 배치 비교'
    assert page.locator('form[data-command]').count() == 0
    source = json.loads(page.locator('#yard-scene-data').text_content())
    assert [frame['move']['destination_placement'] for frame in source['frames'][1:]] == [
        move['destination_placement'] for move in alternative['moves']]
    for width, height in ((1440, 1000), (1280, 800), (768, 1024), (390, 844)):
        page.set_viewport_size(dict(width=width, height=height))
        page.goto(origin + f'/plans/{plan.pk}/candidates/2/')
        page.locator('[data-yard-3d][data-ready=true]').wait_for()
        assert not page.evaluate('document.documentElement.scrollWidth>innerWidth'), width
        page.locator('[data-scene-animate]').uncheck()
        page.locator('[data-scene-cargo]').select_option('C1')
        assert '2×1' in page.locator('[data-scene-info]').inner_text()
        if width >= 1200:
            assert page.locator('[data-yard-linked-flat]').is_visible()
            assert page.locator('[data-yard-linked-flat] [data-cargo-id="C1"]').get_attribute('aria-pressed') == 'true'
        page.locator('[data-yard-stage="1"]').click()
        assert page.locator('[data-yard-stage="1"]').get_attribute('aria-pressed') == 'true'
        page.evaluate('scrollTo(0, 0)')
        assert page.locator('section[data-yard-preview]').bounding_box()['y'] < height
        page.screenshot(path=str(output / f'{width}-candidate-2.png'), full_page=True)
    page.locator(f'nav[aria-label="비교할 후보"] a[href="/plans/{plan.pk}/candidates/1/"]').click()
    page.wait_for_load_state('networkidle')
    first = json.loads(page.locator('#yard-scene-data').text_content())
    assert [frame['move']['destination_placement'] for frame in first['frames'][1:]] == [
        move['destination_placement'] for move in saved['moves']]
    page.get_by_role('link', name='원래 계획으로 돌아가기', exact=True).click()
    page.wait_for_load_state('networkidle')
    page.locator('[data-yard-3d][data-ready=true]').wait_for()
    plan.refresh_from_db()
    assert plan.result == saved and counts == [model.objects.count() for model in (Plan, Observation, Audit, Command)]
    assert posts == []
    page.remove_listener('request', observe)
    return ['Saved candidate2 shows its own full move sequence in the shared 2D/3D viewer across four widths; candidate1 matches the original recommendation; atomic C1 footprint linked; no plan/observation/audit/command mutation or POST']
