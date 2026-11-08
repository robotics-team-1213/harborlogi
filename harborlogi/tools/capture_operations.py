"""Read-only local synthetic operations evidence, including real browser playback video."""
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import sqlite3
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(Path.home()/'AppData/Local/Temp/harbor-web-browser'))
from playwright.sync_api import sync_playwright


def main():
    access=json.loads((ROOT/'runs/product-preview/access.json').read_text(encoding='utf-8'))
    output=ROOT/f'runs/operations-evidence-{datetime.now(timezone.utc):%Y-%m-%d}'
    output.mkdir(exist_ok=True)
    origin='http://127.0.0.1:8060'
    db=sqlite3.connect((ROOT/'runs/product-preview/product.sqlite3').resolve().as_uri()+'?mode=ro',uri=True)
    order_id=db.execute("SELECT o.id FROM harbor_web_order o JOIN harbor_web_cargo c ON o.cargo_id=c.id WHERE o.kind='OUTBOUND' AND c.code='C1' ORDER BY o.id LIMIT 1").fetchone()[0]
    tables=['harbor_web_plan','harbor_web_observation','harbor_web_command']
    counts=lambda:[db.execute('SELECT COUNT(*) FROM '+name).fetchone()[0] for name in tables]
    before=counts()
    images=[];errors=[]
    with sync_playwright() as pw:
        browser=pw.chromium.launch(channel='msedge',headless=True)
        page=browser.new_page(viewport=dict(width=1440,height=1000))
        page.on('pageerror',lambda e:errors.append(str(e)))
        page.goto(origin+'/accounts/login/')
        page.locator('[name=username]').fill(access['username']);page.locator('[name=password]').fill(access['password'])
        page.locator('button[type=submit]').click();page.wait_for_url(origin+'/')
        # Waitress does not reload cached templates after source edits, even in this preview.
        assert page.get_by_text('실제 완료량·처리율',exact=True).is_visible(), 'Restart the local preview before capturing the current operations template.'
        def capture(name,caption,selector=None):
            page.keyboard.press('Escape')
            if selector:page.locator(selector).scroll_into_view_if_needed()
            else:page.evaluate('window.scrollTo(0,0)')
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.screenshot(path=str(output/name))
            images.append(dict(file=name,caption=caption,viewport=page.viewport_size))
        capture('01-operations-overview.png','물량·계획·실행: 화물 개체와 점유 칸, 전체 지시와 계획 작업량의 구분')
        assert page.locator('.operations-unmeasured').inner_text().find('미집계')>=0
        page.goto(origin+f'/requests/{order_id}/')
        page.locator('[data-yard-3d][data-ready=true]').wait_for()
        assert page.locator('.cargo-route').count()>=2
        capture('02-cargo-separation-routes.png','화물별 분리·임시 이동·목표 반출·재적재 경로','.plan-explanation')
        capture('03-plan-before-after.png','이전 Plan과 현재 Plan의 화물별 경로 비교','.plan-comparison')
        page.locator('[data-plan-sequence]').click()
        assert page.locator('[data-scene-play]').get_attribute('aria-pressed')=='true'
        capture('04-plan-sequence-playback.png','배치에서 전체 순서 보기: 관측 배치부터 계획 순서를 차례로 재생','[data-yard-3d]')
        state=page.context.storage_state()
        video_context=browser.new_context(viewport=dict(width=1280,height=900),storage_state=state,
            record_video_dir=str(output/'video'),record_video_size=dict(width=1280,height=900))
        clip=video_context.new_page()
        clip.on('pageerror',lambda e:errors.append(str(e)))
        clip.goto(origin+f'/requests/{order_id}/#yard')
        figure=clip.locator('[data-yard-3d][data-ready=true]');figure.wait_for();figure.scroll_into_view_if_needed()
        figure.locator('[data-scene-action=fit]').click()
        assert figure.locator('[data-scene-speed]').input_value()=='1'
        figure.locator('[data-scene-play]').click()
        clip.wait_for_function('() => {const p=document.querySelector("[data-scene-progress]");return p.dataset.mode==="plan" && Number(p.dataset.progress)>=35 && Number(p.dataset.progress)<=65;}')
        clip.screenshot(path=str(output/'05-slow-midair.png'))
        images.append(dict(file='05-slow-midair.png',caption='4.5초/이동의 중간 프레임: 화물을 들어 올려 이동하는 실제 렌더',viewport=clip.viewport_size))
        clip.wait_for_function('() => document.querySelector("[data-scene-play]").getAttribute("aria-pressed")==="false"')
        video=clip.video
        video_context.close()
        shutil.copyfile(video.path(),output/'slow-plan-playback.webm')
        page.locator('[data-yard-stage="3"]').click()
        page.wait_for_function('() => document.querySelector("[data-yard-3d] canvas").dataset.moving==="false"')
        # At 1200px and wider the floor plan sits beside the 3D view, so there is no view switch to press.
        capture('06-restacking-plane.png','복귀·재적재가 끝난 계획상 예상 배치의 평면도','[data-yard-linked-flat]')
        for width,height in [(768,1024),(390,844)]:
            page.set_viewport_size(dict(width=width,height=height));page.goto(origin+'/')
            capture(f'07-overview-{width}.png',f'{width}px 운영 요약: 단위·미관측·실적 미집계 유지')
        assert not errors,errors
        browser.close()
    assert counts()==before,'Reading or playback changed original plans, observations or commands'
    db.close()
    report=dict(captured_at=datetime.now(timezone.utc).isoformat(),synthetic=True,real_robot=False,
        images=images,video='slow-plan-playback.webm',checks=['operations units','cargo route selection',
        'plan comparison','actual4.5s playback','three widths','no model writes'],errors=errors)
    (output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(dict(output=str(output),images=len(images),video=report['video'],errors=errors)))


if __name__=='__main__':main()
