"""Capture authenticated local synthetic screens; never publish access credentials."""
import json
from pathlib import Path
import sys
from datetime import datetime, timezone

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tools/integration'))
sys.path.insert(0,str(Path.home()/'AppData/Local/Temp/harbor-web-browser'))
from navigation_browser_checks import exercise_navigation
from playwright.sync_api import sync_playwright


def main():
    access=json.loads((ROOT/'runs/product-preview/access.json').read_text(encoding='utf-8'))
    output=ROOT/'runs/outbound-screens-2026-10-26'
    output.mkdir(parents=True,exist_ok=True)
    origin='http://127.0.0.1:8060'
    images=[]
    with sync_playwright() as p:
        browser=p.chromium.launch(channel='msedge',headless=True)
        page=browser.new_page(viewport=dict(width=1440,height=1000))
        errors=[]
        page.on('pageerror',lambda e:errors.append(str(e)))
        page.goto(origin+'/accounts/login/')
        page.locator('[name=username]').fill(access['username'])
        page.locator('[name=password]').fill(access['password'])
        page.locator('button[type=submit]').click()
        page.wait_for_url(origin+'/')
        page.goto(origin+'/outbound/')
        exercise_navigation(page)

        def capture(name,description,selector=None):
            page.keyboard.press('Escape')
            if selector:
                page.locator(selector).scroll_into_view_if_needed()
            else:
                page.evaluate('window.scrollTo(0,0)')
            page.screenshot(path=str(output/name))
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            images.append(dict(file=name,description=description,url=page.url,viewport=page.viewport_size))

        capture('01-outbound-queue.png','반출 관제 목록: 지시 순서·검토 상태·다음 판단')
        href=page.locator('.action-table .cargo-link').first.get_attribute('href')
        page.goto(origin+href)
        figure=page.locator('[data-yard-3d][data-ready=true]')
        figure.wait_for()
        figure.locator('[data-scene-action=fit]').click()
        page.wait_for_timeout(250)
        capture('02-outbound-workspace.png','반출 업무: 현재 판단과 관측 기준·계획의 구분')
        capture('03-observed-yard-3d.png','관측 기준의 실제 3D 모델 화면', '[data-yard-3d]')
        stages=page.locator('[data-yard-stage]')
        assert stages.count()>=4
        stages.nth(2).click()
        page.wait_for_function('() => document.querySelector("[data-yard-3d] canvas").dataset.moving === "false"')
        capture('04-outbound-projection.png','대상 반출 뒤 예상 배치: 실제 관측은 바뀌지 않음','[data-yard-3d]')
        stages.last.click()
        page.wait_for_function('() => document.querySelector("[data-yard-3d] canvas").dataset.moving === "false"')
        page.locator('[data-yard-view=flat]').click()
        capture('05-restoration-plane.png','복귀 후 예상 배치의 자리 평면 보기','[data-yard-flat]')
        capture('06-execution-evidence.png','ROSY 인계 제한과 업무 완료 근거의 구분','#execution')
        page.locator('#navigation').get_by_role('link',name='반출 관제',exact=True).focus()
        page.get_by_role('tooltip').wait_for(state='visible')
        page.screenshot(path=str(output/'07-navigation-tooltip.png'))
        images.append(dict(file='07-navigation-tooltip.png',description='키보드 포커스로 표시한 네비게이션 툴팁',url=page.url,viewport=page.viewport_size))
        page.goto(origin+'/outbound/')
        page.set_viewport_size(dict(width=768,height=1024))
        capture('08-tablet-outbound.png','태블릿 반출 목록: 다음 판단과 업무 열기')
        page.set_viewport_size(dict(width=390,height=844))
        page.locator('#nav-toggle').click()
        page.screenshot(path=str(output/'09-mobile-navigation.png'))
        images.append(dict(file='09-mobile-navigation.png',description='모바일 메뉴: 아이콘과 이름을 함께 표시',url=page.url,viewport=page.viewport_size))
        assert not errors,errors
        browser.close()
    report=dict(captured_at=datetime.now(timezone.utc).isoformat(),synthetic=True,real_robot=False,
        checks=['icon rail','pointer/focus/hoverable tooltip','Escape dismissal','density persistence','tablet/mobile labels','no overflow'],images=images,errors=errors)
    (output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(dict(output=str(output),screenshots=len(images),checks=report['checks'],errors=errors),ensure_ascii=False))


if __name__=='__main__':
    main()
