# 역할 시나리오별 권한·메뉴 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 화면 접근·메뉴·현재 위치 이름·업무 안내를 한 원본(`harbor_web/access.py`)에서 읽고, 페르소나 시나리오대로 메뉴를 묶고, 역할별 예외 화면과 개발용 페르소나 계정을 갖춘다.

**Architecture:** `access.py`가 페이지 목록(경로·이름·허용 역할·메뉴 묶음)과 페르소나 이름을 가진다. 컨텍스트 프로세서가 사용자 역할로 메뉴 묶음을 계산해 `base.html`이 그린다. view의 `require_roles`는 그대로 두고 HTTP 행렬 시험이 `access.py`와 실제 응답을 대조한다. 업무 안내는 `scenarios.py`의 시나리오 데이터를 역할로 걸러 그린다.

**Tech Stack:** Django(서버 템플릿), Python 3.10+, `django.test.TestCase`, 기존 디자인 토큰 CSS.

**Spec:** `harborlogi/docs/design/34-role-scenario-access-and-menus.md`

## Global Constraints

- 접근 규칙은 바꾸지 않는다. 설계34 §4 행렬 = 2026-11-14 실측. 시험이 실패하면 규칙이 아니라 `access.py` 기록을 고친다.
- view 데코레이터(`require_roles(...)`)와 `urls_product.py`는 고치지 않는다(열린 KAN-115·118 브랜치와 충돌 방지).
- 새 색·간격 값을 만들지 않는다. `tokens.css`의 기존 `--semantic-*`·`--primitive-*` 변수만 쓴다. `design/tokens.json`을 고치지 않는다.
- 화면 문구는 한국어. 페르소나 이름은 정확히 `업무 담당`, `현장 확인`, `설치·운영 관리`, `조회`.
- 메뉴 묶음 이름은 정확히 `업무 처리`, `재고·근거`, `현장`, `관리`, `도움말`.
- 시험 명령(작업 디렉터리 `harborlogi/`):
  `HARBOR_WEB_MODE=test HARBOR_WEB_SECRET_KEY=ci-test-only-not-a-deployment-secret-000000000000000000000000 HARBOR_WEB_DATABASE=:memory: ~/harborlogi/harborlogi/runs/web-venv/bin/python -m django test harbor_web.tests --settings=harbor_web.settings --parallel 4`
  기준선: origin/main `65d7cca`에서 355개 OK.
- 비밀번호·토큰·`runs/` 파일을 커밋하지 않는다. `git add`는 바꾼 경로만.
- 커밋 메시지 `feat(web): <내용>` 형식, 끝에 `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

- 역할이 하나도 없는 활성 계정: 메뉴에 403 링크가 하나도 없어야 한다(지금은 `업무 안내`가 403). Task 1 `test_no_role_user_sees_no_links`.
- 겸임 계정(operator+field, admin+viewer): 묶음·항목이 중복 없이 한 번씩. Task 1 `test_combined_roles_list_each_page_once`.
- 접힌 메뉴(`nav-compact`)와 900px 이하 펼침 메뉴: 묶음 제목이 아이콘 줄을 깨지 않고, 기존 `data-nav-tip` 툴팁이 그대로 동작. Task 1 `test_group_label_has_no_icon_and_keeps_tooltips` + 브라우저 확인(Task 5).
- 현장 확인만 가진 계정이 `/`로 들어오면 `/field/`로 이동(유지). Task 1 행렬 시험의 특례.
- 비활성(`is_active=False`) 계정은 로그인으로 돌려보냄(유지). Task 1 행렬 시험에 포함.

---

### Task 1: 접근 원본과 묶음 메뉴

**Files:**
- Create: `harborlogi/harbor_web/access.py`
- Modify: `harborlogi/harbor_web/presentation.py` (컨텍스트 값 추가, 기존 키 유지)
- Modify: `harborlogi/harbor_web/templates/base.html` (주 메뉴, 머리 현재 위치, 사이드바 아래 역할)
- Modify: `harborlogi/harbor_web/templates/nav_item.html` (필요하면 `page` 객체를 받도록)
- Modify: `harborlogi/harbor_web/static/harbor_web/navigation.css` (묶음 제목·구분선)
- Test: `harborlogi/harbor_web/tests/test_access_policy.py`
- Keep passing: `harborlogi/harbor_web/tests/test_navigation.py` (operator `/outbound/`의 `class="nav-icon"` 10개 유지)

**Interfaces:**
- Produces (`harbor_web/access.py`):
  - `PERSONAS: dict[str, str]` = `{'operator': '업무 담당', 'field': '현장 확인', 'admin': '설치·운영 관리', 'viewer': '조회'}` (이 순서가 표시 순서)
  - `NAV_GROUPS: tuple[tuple[str, str], ...]` = `(('work','업무 처리'), ('evidence','재고·근거'), ('field','현장'), ('admin','관리'), ('help','도움말'))`
  - `@dataclass(frozen=True) class Page: key: str; path: str; label: str; hint: str; roles: frozenset[str]; group: str | None = None; utility: bool = False` — `group=None`이면 메뉴에 없는 화면
  - `PAGES: tuple[Page, ...]` — 메뉴 9개(설계34 §5 순서, 기존 `key`·`label`·`hint` 문구 그대로: `overview`,`outbound`,`cargo`(`/requests/`),`work`,`rearrangement`,`inventory`,`records`,`field`,`settings`,`workflow`) + 메뉴 밖 정적 경로 `cargo_list`(`/cargo/`), `cargo_new`, `request_new`, `observation_new`(`/inventory/import/`), `field_new`, `measurement_new`, `comparison`(`/records/compare/`), `condition_new`, `map_new`, `integration`. `roles`는 200을 받는 역할(설계34 §4). `overview.roles`에 field는 없다.
  - `def page(key: str) -> Page` (없으면 `KeyError`)
  - `def can_open(user_roles: Iterable[str], key: str) -> bool`
  - `def nav_groups(user_roles: Iterable[str]) -> list[dict]` → `[{'key': str, 'label': str, 'items': list[Page]}]`, 빈 묶음 제외, 묶음·항목 순서 고정
  - `def persona_names(user_roles: Iterable[str]) -> list[str]` (PERSONAS 순서)
  - `MENU_PAGES: tuple[Page, ...]` = `group`이 있는 PAGES(역할과 무관한 전체 메뉴)
- Produces (컨텍스트, `presentation.context`): 기존 키 전부 + `nav_groups`, `persona_names`, `menu_pages`(=`access.MENU_PAGES`). `section`은 view가 넘기므로 컨텍스트 프로세서는 모른다. crumb은 템플릿에서 `menu_pages`를 돌며 `item.key == section`인 `label`을 쓴다(현장 확인 계정이 `section='work'`인 실행 상세를 볼 때도 `실행·대조`가 나와야 하므로 `nav_groups`가 아니라 전체 목록을 돈다).

- [ ] **Step 1: 실패하는 시험 작성** `test_access_policy.py`

```python
PERSONA_USERS = ('viewer', 'operator', 'field', 'admin')  # 각 group 하나만 가진 사용자 + 역할 없는 'nobody'

def test_every_static_page_matches_registered_roles(self):
    for page in access.PAGES:
        for name in PERSONA_USERS + ('nobody',):
            # field 전용의 '/'는 302 -> '/field/' 특례
            # name in page.roles -> 200, 아니면 403
            ...

def test_inactive_user_is_sent_to_login(self):  # '/'와 '/field/' 모두 302, Location이 '/accounts/login/'으로 시작

def test_nav_groups_follow_roles(self):
    keys = lambda roles: [(g['key'], [p.key for p in g['items']]) for g in access.nav_groups(roles)]
    self.assertEqual(keys({'field'}), [('field', ['field']), ('help', ['workflow'])])
    self.assertEqual([g for g, _ in keys({'viewer'})], ['work', 'evidence', 'help'])
    self.assertEqual([g for g, _ in keys({'operator'})], ['work', 'evidence', 'field', 'help'])
    self.assertEqual([g for g, _ in keys({'admin'})], ['work', 'evidence', 'field', 'admin', 'help'])
    self.assertEqual(keys(set()), [])

def test_combined_roles_list_each_page_once(self):  # {'operator','field'}, {'admin','viewer'}: 항목 key 중복 없음

def test_no_role_user_sees_no_links(self):
    # nobody로 '/accounts/password_change/' GET -> 주 메뉴(<nav aria-label="주 메뉴">) 안에 href 없음, 사이드바 아래 '역할 미배정'

def test_menu_renders_groups_and_persona_names(self):
    # operator로 '/workflow/' GET: 'data-nav-group="work"', 'data-nav-group="field"' 있음, 'data-nav-group="admin"' 없음
    # '업무 처리' 묶음 제목, 사이드바 아래 '업무 담당', 영문 '<span>operator</span>' 없음

def test_group_label_has_no_icon_and_keeps_tooltips(self):
    # operator '/outbound/': 'class="nav-icon"' 10개(기존), 'data-nav-tip="반출 관제 · 우선순위·재취급 계획"' 유지, 'aria-current="page"' 1개

def test_crumb_uses_registered_label(self):
    # operator '/rearrangement/' -> topbar crumb(<span class="crumb">)에 '자동 재배치'
```

- [ ] **Step 2: 실패 확인** — 위 시험 명령에 `harbor_web.tests.test_access_policy`를 붙여 실행. Expected: `ModuleNotFoundError: harbor_web.access` 또는 assertion 실패.

- [ ] **Step 3: `access.py` 구현** — 위 Interfaces 그대로. `roles` 판정은 `harbor_web.auth.roles(user)`의 결과 집합을 받는다(직접 DB 조회 금지).

- [ ] **Step 4: 컨텍스트·템플릿·CSS** — `base.html` 주 메뉴는 `{% for group in nav_groups %}<div class="nav-group" role="group" aria-labelledby="nav-group-{{ group.key }}" data-nav-group="{{ group.key }}"><p class="nav-group-label" id="nav-group-{{ group.key }}">{{ group.label }}</p>{% for item in group.items %}{% include 'nav_item.html' ... %}{% endfor %}</div>{% endfor %}`. 접기 버튼(`data-nav-density`)은 그대로 맨 위. `utility` 표시는 `관리`·`도움말` 항목에 유지. 머리 crumb의 긴 `if section == ...` 분기를 등록 이름 조회로 바꾸되 `{% block crumb %}`는 남긴다(하위 템플릿이 덮어씀). 사이드바 아래는 `persona_names`, 비면 `역할 미배정`. CSS: `.nav-group-label`은 캡션 크기·보조 텍스트색(어두운 사이드바 위 `--semantic-text-inverse` 계열), 묶음 사이 구분선 1px; `.nav-compact` 안에서는 제목을 sr-only(기존 `.nav-compact .nav-density .nav-label`과 같은 숨김 규칙)로 숨기고 구분선만 남긴다.

- [ ] **Step 5: 통과 확인** — 전체 시험 명령. Expected: 기존 355개 + 새 시험 모두 OK.

- [ ] **Step 6: Commit** — `git add harbor_web/access.py harbor_web/presentation.py harbor_web/templates/base.html harbor_web/templates/nav_item.html harbor_web/static/harbor_web/navigation.css harbor_web/tests/test_access_policy.py` 후 `feat(web): 역할 시나리오별 메뉴 묶음과 접근 원본`.

### Task 2: 역할별 403 복귀와 계정 관리 링크

**Files:**
- Modify: `harborlogi/harbor_web/templates/403.html`
- Modify: `harborlogi/harbor_web/templates/settings.html` (계정·역할 절의 `/admin/` 링크들)
- Test: `harborlogi/harbor_web/tests/test_access_fallbacks.py`

**Interfaces:**
- Consumes: 기존 컨텍스트 `can_read`, `can_field`, `app_roles`, `user.is_staff`. Task 1의 이름은 쓰지 않는다(병렬 진행).

- [ ] **Step 1: 실패하는 시험 작성**

```python
def test_reader_returns_to_overview(self):       # viewer로 '/settings/' -> 403, 본문에 '<a href="/">운영 현황으로 돌아가기</a>'
def test_field_only_returns_to_field(self):      # field로 '/requests/' -> 403, '<a href="/field/">현장 확인으로 돌아가기</a>', '운영 현황으로 돌아가기' 없음
def test_no_role_user_gets_role_notice(self):    # 역할 없는 사용자 '/' -> 403, '역할이 아직 없습니다', password_change 링크, '운영 현황으로 돌아가기' 없음
def test_account_admin_link_needs_staff(self):   # admin(non-staff) '/settings/' -> '/admin/' href 없음, '계정 관리에는 staff 권한이 필요합니다'
def test_staff_admin_sees_account_links(self):   # admin(is_staff=True) '/settings/' -> 'href="/admin/auth/user/"' 있음
```

- [ ] **Step 2: 실패 확인** — `harbor_web.tests.test_access_fallbacks`만 실행. Expected: assertion 실패.
- [ ] **Step 3: 구현** — `403.html`: `can_read`이면 운영 현황, 아니고 `can_field`면 현장 확인, 둘 다 아니면 역할 안내 + `{% url 'password_change' %}` 링크. 기존 문장 “계정 관리자에게 필요한 역할이나 현장 배정을 확인하세요.”는 유지. `settings.html`: `/admin/`로 가는 링크들을 `{% if user.is_staff %}`로 감싸고 아니면 안내 문장.
- [ ] **Step 4: 통과 확인** — 전체 시험 명령. Expected: 모두 OK.
- [ ] **Step 5: Commit** — 바꾼 세 파일만 add, `feat(web): 역할별 403 복귀와 staff 전용 계정 관리 링크`.

### Task 3: 개발 미리보기 페르소나 계정

**Files:**
- Modify: `harborlogi/tools/product_preview.py`
- Test: `harborlogi/harbor_web/tests/test_preview_personas.py`

**Interfaces:**
- Produces: `tools/product_preview.py`의 `PERSONA_ACCOUNTS: dict[str, str]` = `{'operator': 'harbor-operator', 'field': 'harbor-field', 'admin': 'harbor-admin', 'viewer': 'harbor-viewer'}` 와 `def ensure_persona_accounts(saved: dict | None) -> dict` — `saved`는 access.json의 `personas` 값(없으면 `None`). 반환은 `{role: {'username': str, 'password': str}}`. 이미 있는 비밀번호는 유지, 없는 역할만 `secrets.token_urlsafe(24)`로 만든다. 각 사용자는 그 group 하나만, `admin`만 `is_staff=True`. Django가 설정된 뒤에만 호출.
- `main()`은 기존 `harbor-preview`(네 역할) 흐름을 유지하고, `ensure_persona_accounts` 결과를 access.json의 `personas`에 저장(파일 권한 0600 유지, 원자적 교체: 같은 폴더 임시 파일 → `os.replace`). 합성 현장 회차가 있으면 `harbor-field`에 배정. 콘솔에는 사용자 이름만 출력, 비밀번호 출력 금지.

- [ ] **Step 1: 실패하는 시험 작성**

```python
def test_creates_one_account_per_persona(self):   # 결과 키 = {'operator','field','admin','viewer'}; 각 User의 group 집합 = {role}; admin만 is_staff
def test_keeps_saved_passwords(self):             # 두 번째 호출에 첫 결과를 넘기면 비밀번호 동일, check_password True
def test_fills_missing_persona(self):             # saved에 operator만 있으면 operator 유지 + 나머지 생성
```

- [ ] **Step 2: 실패 확인** — `harbor_web.tests.test_preview_personas`만 실행. Expected: `ImportError`/`AttributeError`.
- [ ] **Step 3: 구현** — 위 Interfaces.
- [ ] **Step 4: 통과 확인** — 전체 시험 명령 + `cd harborlogi && python -m unittest discover -s tests` (도메인 시험; 기준선과 같은 결과).
- [ ] **Step 5: Commit** — 두 파일만 add, `feat(web): 개발 미리보기에 페르소나별 계정`.

### Task 4: 업무 안내를 페르소나 시나리오로 (Task 1 병합 뒤)

**Files:**
- Create: `harborlogi/harbor_web/scenarios.py`
- Modify: `harborlogi/harbor_web/guide_pages.py` (`workflow` view 컨텍스트만)
- Modify: `harborlogi/harbor_web/templates/workflow.html`
- Test: `harborlogi/harbor_web/tests/test_workflow_guide.py`

**Interfaces:**
- Consumes: `access.PERSONAS`, `access.page(key)`, `access.can_open(roles, key)`, `harbor_web.auth.roles(user)`.
- Produces: `@dataclass(frozen=True) class Step: page: str; text: str`, `@dataclass(frozen=True) class Scenario: key: str; persona: str; title: str; steps: tuple[Step, ...]; handoff: str` 와 `SCENARIOS: tuple[Scenario, ...]` = 설계34 §3의 S1~S11(키·페르소나·제목·화면 순서 그대로, `handoff`는 ‘다음 담당’ 열, `—`이면 `''`). S10 단계의 화면은 `settings`(계정 관리 `/admin/`은 PAGES에 없으므로 글로 안내). `def scenario_rows(user_roles) -> dict` → `{'mine': [...], 'others': [...]}`; 각 단계는 `{'label', 'href' 또는 None, 'owner'(열 수 없을 때 페르소나 이름), 'text'}`.

- [ ] **Step 1: 실패하는 시험 작성**

```python
def test_every_step_points_to_a_registered_page(self):  # 모든 Step.page가 access.page()로 찾아짐, persona가 PERSONAS 키
def test_field_sees_own_scenario_first(self):           # field로 '/workflow/': 'S5' 시나리오가 '내 일' 절 안, '/requests/new/' 링크 없음, '다른 담당의 일' <details> 안에 S1 제목
def test_operator_steps_link_only_openable_pages(self): # operator: '/cargo/new/' 링크 있음, '/inventory/import/' 링크 없음 + 그 단계에 '설치·운영 관리' 표시
def test_existing_guidance_is_kept(self):               # viewer: '현재 운영 연결의 범위' 문단 유지; field: '5분' 유효시간 안내 유지
```

- [ ] **Step 2: 실패 확인** — `harbor_web.tests.test_workflow_guide`만 실행.
- [ ] **Step 3: 구현** — 기존 `workflow.html`의 01~05 설명 문장과 현장 확인 문단은 해당 시나리오 단계 `text`로 옮겨 내용을 보존한다(설계34 §6). 시나리오 제목은 `<h2>`, 단계는 순서 목록. 다른 담당의 일은 `<details><summary>다른 담당의 일</summary>`.
- [ ] **Step 4: 통과 확인** — 전체 시험 명령. 기존 시험이 옛 문구를 확인해 실패하면, 문구가 시나리오 단계로 옮겨졌는지 보고 시험 기대값을 새 위치에 맞춘다(문구를 지우는 쪽으로 맞추지 않는다).
- [ ] **Step 5: Commit** — 네 파일(+ 고친 기존 시험) add, `feat(web): 업무 안내를 페르소나 시나리오로`.

### Task 5: 명세 갱신과 화면 확인 (Task 1~4 병합 뒤, 두 갈래 병렬)

**Files (UI/UX 갈래):** `harborlogi/docs/design/02-ui-ux.md` §1, `harborlogi/docs/design/08-navigation.md` §2·§8, `harborlogi/DESIGN.md` ‘Navigation and work tabs’
**Files (기능 갈래):** `harborlogi/docs/design/13-product-web-surface.md` 경로·권한 표와 전역 탐색 문장, `harborlogi/docs/design/17-operator-page-flow.md` 업무 안내 행, `harborlogi/docs/product-web.md` 로컬 화면 검토 절, `harborlogi/docs/design/README.md` 목차 34행, `harborlogi/docs/design/34-role-scenario-access-and-menus.md` 상태 줄

- [ ] **Step 1:** 각 문서의 해당 절을 실제 구현(브랜치 HEAD)에 맞춘다. 정본 관계는 유지: 경로·권한 정본 = 13, 역할 시나리오·메뉴 묶음 = 34, 문서끼리 링크로 잇는다. 날짜 붙은 검증 기록은 고치지 않는다.
- [ ] **Step 2:** 오케스트레이터가 새 브랜치로 미리보기(별도 포트·별도 DB)를 띄워 페르소나 네 계정으로 메뉴·403·업무 안내를 PC 폭과 600px 폭에서 확인하고 `harborlogi/docs/validation/2026-11-14-role-scenario-menus.md`에 남긴다(커밋 SHA, 시험 결과, 확인 못 한 것).
- [ ] **Step 3: Commit** — 문서 경로만 add, `docs(web): 역할 시나리오 권한·메뉴 명세 갱신`.

## 실행 순서

1. 설계34와 이 계획을 `feat/role-scenario-menus`에 먼저 커밋한다.
2. 1차 병렬: Task 1·2·3을 각각 `feat/role-scenario-menus-<nav|fallback|preview>` 하위 워크트리에서 구현 → 검토 → `feat/role-scenario-menus`로 병합.
3. Task 4(Task 1 결과 필요).
4. Task 5 두 갈래 병렬 + 화면 확인.
5. 전체 시험·브랜치 검토. push·PR은 사용자 지시 뒤에만.
