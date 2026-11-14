---
name: HarborLogi
description: Authenticated Harbor product web for business requests, planning and execution evidence.
colors:
  navy: "#112C3F"
  ink: "#17364B"
  muted: "#506878"
  teal: "#2CBBB7"
  teal-soft: "#DAF5F2"
  orange: "#FE6D08"
  orange-soft: "#FFF1DA"
  paper: "#F4F8FB"
  white: "#ffffff"
  line: "#D7E3E9"
  focus: "#B94D00"
  warning-ink: "#873C0A"
  control-border: "#6B8292"
  success-ink: "#167044"
  success-soft: "#E1F7EA"
  danger-ink: "#B72D43"
  danger-soft: "#FFF0F2"
  unknown-soft: "#E7EEF6"
  teal-dark: "#087C80"

  teal-50: "#EFFBFA"

  teal-hover: "#08636B"

  orange-dark: "#B94D00"

  cream: "#FCF1D9"

  navy-soft: "#EAF1F6"

typography:
  headline:
    fontFamily: '"Segoe UI", "Malgun Gothic", "Apple SD Gothic Neo", "Noto Sans KR", sans-serif'
    fontSize: "1.75rem"
    fontWeight: 700
    lineHeight: 1.3
    letterSpacing: "-.02em"
  title:
    fontFamily: '"Segoe UI", "Malgun Gothic", "Apple SD Gothic Neo", "Noto Sans KR", sans-serif'
    fontSize: "1.125rem"
    fontWeight: 700
    lineHeight: 1.4
  body:
    fontFamily: '"Segoe UI", "Malgun Gothic", "Apple SD Gothic Neo", "Noto Sans KR", sans-serif'
    fontSize: "1rem"
    fontWeight: 400
    lineHeight: 1.6
  label:
    fontFamily: '"Segoe UI", "Malgun Gothic", "Apple SD Gothic Neo", "Noto Sans KR", sans-serif'
    fontSize: "0.875rem"
    fontWeight: 600
    lineHeight: 1.6
rounded:
  small: "4px"
  control: "6px"
  panel: "8px"
spacing:
  "1": "0.25rem"
  "2": "0.5rem"
  "3": "0.75rem"
  "4": "1rem"
  "6": "1.5rem"
  "8": "2rem"
  "12": "3rem"
  "16": "4rem"
components:
  button-primary:
    backgroundColor: "{colors.teal-dark}"
    textColor: "{colors.white}"
    rounded: "{rounded.control}"
    padding: "0.5rem 1rem"
    height: "44px"
  button-primary-hover:
    backgroundColor: "{colors.teal-hover}"
    textColor: "{colors.white}"
  button-quiet:
    backgroundColor: "{colors.white}"
    textColor: "{colors.teal-dark}"
    rounded: "{rounded.control}"
    padding: "0.5rem 1rem"
  input-search:
    backgroundColor: "{colors.white}"
    textColor: "{colors.ink}"
    rounded: "{rounded.control}"
    padding: "0.5rem 0.75rem"
  navigation-current:
    backgroundColor: "{colors.teal}"
    textColor: "{colors.ink}"
    rounded: "{rounded.control}"
    padding: "0.75rem"
  task-current:
    backgroundColor: "{colors.teal-soft}"
    textColor: "{colors.ink}"
    rounded: "{rounded.small}"
    padding: "1rem 0.75rem"
  tab-current:
    textColor: "{colors.teal-dark}"
    height: "48px"
  badge-hold:
    backgroundColor: "{colors.orange-soft}"
    textColor: "{colors.warning-ink}"
    rounded: "{rounded.small}"
    padding: "0.25rem 0.5rem"
  next-action:
    backgroundColor: "{colors.teal-soft}"
    textColor: "{colors.ink}"
    rounded: "{rounded.panel}"
    padding: "1.5rem"

  planning-bay:
    backgroundColor: "{colors.white}"
    textColor: "{colors.ink}"
    rounded: "{rounded.control}"
    padding: "0.75rem"

---

# Design System: HarborLogi

## Overview

**Creative North Star: "현재 업무와 다음 판단이 분명한 관제"**

Harbor는 사용자가 맡은 화물의 진행, 막힌 이유, 필요한 근거와 다음 행동을 판단하는 업무 도구다. 기존 네이비·청록·오렌지·아이보리와 컨테이너 H 아이콘을 유지한다. 밝고 차분한 작업면에서 선택과 다음 확인을 강조하고 상태에는 읽을 수 있는 문구를 붙인다.

업무 식별→현재 판단→진행→근거→추적 정보의 순서로 읽게 한다. 전역 탐색, 업무 목록의 검색·보기, 한 업무의 탭을 구분한다. Harbor 업무 상태, ROSY 실행, physical receipt와 업무 완료 근거는 각각 이름 붙이고 미확인과 조회 실패를 드러낸다.

**Key Characteristics:**

- 조치 필요 중심의 운영 현황과 별도 업무 목록.
- 전역 메뉴·업무 선택·업무 탭의 세 층 탐색.
- 상태 색과 문구, 출처와 조회 범위를 함께 표시.

**적용 상태와 정본:** 이 문서는 인증된 Django 제품 웹과 별도로 유지하는 로컬 조회 preview의 구현 기록이다. [design/tokens.json](design/tokens.json)의 147개 primitive→semantic→component 토큰이 런타임 값의 정본이며, [생성 도구](tools/design_tokens.py)가 [tokens.css](harbor/web_assets/tokens.css)를 만든다. Frontmatter는 DESIGN.md 도구를 위한 현재 값의 기록이며 별도 편집 원본이 아니다. 토큰을 변경하면 생성 CSS와 이 기록을 함께 갱신한다. Sidecar는 확장 메타데이터와 현재 CSS 변수에 연결한 패널 예제를 담는다.

- [디자인 철학](docs/design/07-design-foundation.md), [네비게이션](docs/design/08-navigation.md), [토큰·패턴 계약](docs/design/09-tokens-and-patterns.md): 제품 기준과 후속 범위.
- [페이지 구현 계획](docs/design/10-navigation-pages-plan.md), [재고·반출 계획 페이지](docs/design/11-retrieval-planning-page.md), [PRODUCT.md](PRODUCT.md), [브랜드 자산](assets/brand/README.md): 맥락과 범위.
- 제품 웹 근거: `harbor_web/templates/`, `harbor_web/static/harbor_web/{product.css,product.js}`, `harbor_web/urls_product.py`. 기존 조회 preview 근거: `harbor/web_assets/{index.html,app.css,app.js,tokens.css}`.
- 역할별 화면 계약: [제품 웹 화면·동작 계약](docs/design/13-product-web-surface.md).

인증된 제품 웹은 팀 계정·명시적 역할, 영속 화물/요청, 관측 원본 등록, 불변 계획, 실행 intent, 현장 회차와 감사/비교 화면을 구현했다. 최종 수용은 진행 중이며 실제 운영 관측 producer, 모든 이동 단계의 설치 binding, 현장 UAT와 LAN/TLS 설치가 남은 게이트다. 기존 읽기 전용 preview의 합성 계획·Fleet 조회 범위는 별도로 유지한다. 기존 Operate 화면의 FORM을 확장했으므로 concept-seed/comp 선택과 새 raster 생성은 적용하지 않았다. 생성기 실패나 사용 불가를 의미하지 않는다.

## Colors

### Primary

로고 청록은 현재 메뉴 선택, 짙은 청록은 주요 조작·링크, 연한 파생 청록은 선택·행 hover 면을 나타낸다. 네이비는 전역 탐색 배경, 버튼 hover는 짙은 청록 파생 톤을 사용한다. 로고의 실제 대표색에서 기능별 대비를 확장한다.

### Secondary

오렌지는 브랜드의 일부다. 보류·대조 필요와 데모 맥락은 연한 오렌지와 warning-ink를 사용한다. 데모 표식은 장애 심각도 표시가 아니다. 성공·위험·미확인 의미 토큰을 제공하며 실제 badge는 info·warning·success·unknown 변형을 사용한다. danger 토큰의 존재를 장치 안전 알람 구현으로 해석하지 않는다.

### Neutral

밝은 쿨화이트는 페이지 바탕, 흰색은 목록·입력·보조 헤더 면이다. 로고의 크림은 보조 팔레트에 보존하며 전체 바탕을 베이지로 채우지 않는다. ink는 주본문, muted는 보조 설명, line은 구분선, control-border는 입력과 보조 버튼 경계다. 밝은 면의 키보드 포커스는 focus, 네이비 면에서는 paper를 사용한다. 2026-10-25 사용자 요청으로 로고 원본의 영역별 대표색을 샘플링해 브랜드 앵커를 맞췄다. 래스터 질감이 있으므로 수치는 단일 벡터 원색의 주장 대신 대표색이다.

## Typography

운영체제의 한글 가능 sans stack을 사용하며 원격 폰트를 내려받지 않는다. 본문은 body, 페이지·업무 제목은 headline, 섹션 제목은 title, 조작과 보조 설명은 label을 사용한다. Metadata는 primitive-font-caption(0.75rem)이다. 본문 line-height는 body-line, 제목은 실제 CSS의 heading-line 또는 섹션 1.4를 사용한다. 이전 화면의 body15·headline30과 모바일 제목 축소 규칙은 적용하지 않는다.

문단의 읽기 폭은 read-measure(72ch)로 제한한다. 긴 ID는 줄바꿈하고 fact 값과 단계 번호는 tabular numerals로 정렬한다. 핵심 상태와 실패 이유를 metadata 크기로 압축하지 않는다.

## Layout

제품 웹은 900px 초과에서 왼쪽 고정 탐색(200px), sticky topbar(64px), 본문 여백(32px)을 사용한다. 본문 최대 폭은 1800px이며 1279px 이하에서는 여백 24px와 facts 두 열을 사용한다. 계정·환경은 topbar에 둔다. 표의 스크롤은 표 영역에 한정한다.

900px 이하에서는 메뉴를 본문 위에서 펼치고 접으며 각 메뉴 묶음은 이름 아래에 항목을 두 열로 보여 주고 비교 배치는 한 열이다. JavaScript가 없으면 메뉴가 보인다. 제품 웹 메뉴는 modal drawer가 아니다. 600px 이하에서는 본문 여백 16px, 제목·조작의 상하 배치, 전체 폭 검색 필드를 사용한다. 조치 필요 표는 사유와 후속 링크가 있는 행으로 재배치한다. 이동 표는 내부 가로 스크롤을 유지하고 facts는 두 열이다. 버튼·입력은 최소 44px, 현장 결과 버튼은 최소 56px다. 화면 폭은 역할을 바꾸지 않는다.

기존 조회 preview는 별도 768/1280px 토큰 breakpoint와 업무 목록/상세, modal drawer 구성을 유지한다. 이 값은 제품 웹의 900/600px 실제 CSS 규칙을 대체하지 않는다. 채택된 147개 토큰과 간격 scale을 재사용하며 제품 웹 breakpoint는 추가 토큰이 아니다.

## Elevation & Depth

작업면은 평면이며 그림자는 없다. 배경 톤, 구분선과 간격으로 위계를 만든다. Sticky 탐색·topbar와 drawer/backdrop은 primitive-layer 단계로 분리한다. 키보드 포커스는 focus-width와 focus-offset을 사용한다.

기존 preview drawer는 navigation duration과 ease 토큰으로 전환하고 reduced motion에서는 reduced 토큰으로 줄인다. 제품 웹 메뉴에는 별도 전환 애니메이션이 없다. Feedback duration은 토큰으로 제공되지만 별도 버튼 애니메이션은 현재 CSS에 적용되지 않았다. 추정 로봇 움직임이나 진행률을 애니메이션으로 만들지 않는다.

## Shapes

상태·업무 행에는 small, 입력·버튼·전역 메뉴 항목에는 control, 안내 패널·컨셉 배너에는 panel 모서리를 사용한다. 단계 번호는 원형이다. 면·제목·간격·구분선을 우선하며 모든 정보를 같은 카드로 감싸지 않는다. 컨테이너 H 원본을 다시 그리지 않는다.

## Components

### Buttons and inputs

제품 웹의 주요 버튼은 역할에 따라 화물·요청 저장, 계획 생성·검토, 지원되는 단계 제안과 현장 확인을 제공한다. 기존 preview의 주요 버튼은 Fleet 콘솔 인계와 합성 계획 미리보기다. 보조 버튼은 장부 새로고침·ROSY 상태 재조회·원래 mission ID 복사를 제공한다. Hover는 선택/행동 의미 토큰을, focus는 공통 outline을 사용한다. 비활성 버튼은 disabled 면과 보조 텍스트·wait cursor를 사용한다. 검색과 보기 select는 보이는 label, 조작 최소 높이와 입력 경계를 유지한다.

### Navigation and work tabs

제품 웹 전역 탐색은 담당자의 일 순서로 묶는다(정의는 `harbor_web/access.py`, 설계는 `docs/design/34`). 업무 처리: 운영 현황(`/`), 반출 관제(`/outbound/`), 화물·요청(`/requests/`), 실행·대조(`/work/`), 자동 재배치(`/rearrangement/`). 재고·근거: 재고·관측(`/inventory/`), 기록·검증(`/records/`). 현장: 현장 확인(`/field/`). 관리: 설정·계정(`/settings/`). 도움말: 업무 안내(`/workflow/`). 조회·업무 담당·관리는 업무 처리와 재고·근거를 보고, 현장 확인은 업무 담당·현장 확인·관리가, 설정·계정은 관리만 본다. 업무 안내는 모든 역할이 본다. 보일 항목이 없는 묶음은 이름도 숨긴다. 묶음 이름 아래에 구분선을 두고 compact rail에서는 이름을 시각적으로만 숨긴다. 사이드바 하단에는 역할 코드 대신 담당 이름(업무 담당·현장 확인·설치·운영 관리·조회)을 보여 준다. 권한이 없는 화면(403)의 돌아가기 링크는 역할을 따른다: 조회 가능하면 운영 현황, 현장 확인이면 현장 확인, 역할이 없으면 비밀번호 변경만 둔다. 화물 등록·요청 등록 같은 화면은 메뉴 항목이 아니라 영역 내부 링크이며 계획은 요청 상세에서 생성한다. 현재 링크는 `aria-current`로 표시한다. 로그인·로그아웃·비밀번호 변경과 명시적 앱 역할이 구현되어 있다.

검색·필터·pagination은 URL 맥락을 유지한다. 폼은 보이는 label, 필드 오류와 입력 보존을 제공한다. 403·409·미연동은 이유와 복귀/재조회 경로를 제공한다. 상세 facts, 펼칠 수 있는 원본, 관측/예상 배치 비교, 이동 표와 실행 조건을 사용한다. 기존 preview의 업무 개요/계획/실행·근거 탭과 키보드 탭 동작은 해당 preview 범위다.

### Work rows, badges and next check

현재 업무 행은 선택 경계와 연한 면을 함께 사용한다. Badge는 상호작용이 없는 한국어 상태 label이다. 운영 현황은 장부의 SUBMITTING·HOLD·AWAITING_FINAL_EVIDENCE에서 도출 가능한 조치 필요 업무를 보여 주며 WAITING은 별도 대기로 둔다. 담당이 없으면 담당 미지정으로 표시한다. 다음 확인은 현재 판단과 책임을 담는 tonal section이다.

### Product planning and separate synthetic preview

제품 웹은 저장한 요청과 선택한 관측 revision으로 불변 계획을 만들고 관측/예상 배치와 단계를 대조한다. DECLARED/SYNTHETIC 등록은 인증 관측이 아니다. 다음 합성 상호작용 설명은 별도 preview 범위다. Preview는 단일 목표 화물과 합성 조건으로 기존 서버 planner 결과를 읽는다. 입력 변경 시 이전 결과를 숨기고 재계산을 안내한다. 계산 중 버튼을 비활성화하고 실패는 alert와 미확인 안내로 남긴다. 관측 배치와 계획 후 예상 배치를 별도 제목·설명·출처로 분리한다. 합성 고정 시점·source·revision과 합성 팔 도달 표임을 명시한다.

칸의 stack은 위에서 아래로 표시하며 화물 ID·단·대상 설명을 붙인다. 반출 대상은 기존 선택 면, 위의 재취급 대상은 기존 주의 면을 사용한다. 이 도식은 논리 배치이며 실제 위치 지도나 도달성 증거가 아니다. 예상 배치는 ‘관측 아님’으로 표시하고 원래 관측 배치는 바꾸지 않는다.

READY 결과는 재취급→반출→복귀의 단계와 주/대체 팔을 보여 준다. 임시 공간 부족·오래된 관측 등의 HOLD 결과는 이유를 표시하고 부분 이동이나 예상 완료 배치를 만들지 않는다. 필요한 반출대 내려놓기·재취급 화물 복귀·관련 팔 퇴피는 확인 전 조건으로 제시한다. 이 화면은 실행 배정·허가·완료 판정, 순위 지시 접수·입고 등록·실제 관측·양팔 성능 비교 또는 전체 UR/SR 수용을 제공하지 않는다. 기존 147개 토큰만 재사용한다.

### Evidence and brand

실행·근거 탭은 Fleet 실행, physical receipt, Harbor 업무 장부를 분리한다. 원래 ID와 추적 정보는 펼칠 수 있게 보존한다. Loading·빈 목록·연결 실패·불일치·미확인을 구별하며 화면 조회 시각을 원본 관측 시각으로 표시하지 않는다. Skip link·live status·오류 alert·선택 후 제목 focus를 유지한다.

H 아이콘은 `assets/brand/harbor-app-icon-20261023.png`를 `/assets/icon.png`로 제공한다. 도움말은 기존 `assets/brand/harborlogi-github-banner-20261023.png`를 `/assets/banner.png`로 제공하고 컨셉 이미지 caption을 유지한다. 배너는 장치 실측이나 실행 증거가 아니다.

## Do's and Don'ts

- Do keep business state, ROSY execution and receipt evidence separately labeled.
- Do pair status color with readable text and preserve keyboard focus.
- Do reuse the existing Harbor name, container H icon and concept banner.
- Do derive shared CSS values from design/tokens.json and regenerate tokens.css.
- Don't present demo data or concept artwork as device evidence.
- Don't imply that a read, an Action success or a receipt authorizes execution or completes all business checks.
- Don't equate implemented login and roles with deployment security or field acceptance.
- Don't present declared inventory, audit records or fixture comparisons as authenticated operational evidence or field performance acceptance.
- Do label synthetic observed stacks and projected plan results separately; keep HOLD free of partial moves or projected completion.

## 물량·경로·재계획을 읽는 관제 (2026-10-26)

mode: Operate. [설계23](docs/design/23-operations-replan-playback.md). 첫 화면은 선택 관측의 화물/점유와 전체 등록 지시·계획 작업량을 읽는 단위를 먼저 구분한다. 실제 완료량은 업무 장부가 완료한 인계만 요청당 한 번 세고, 시간당 물동량은 최근 24시간의 첫~마지막 완료 사이로만 세고, 교대·일 단위 처리율은 미집계로 표시한다. 작업 공간은 화물별 출발→임시→반출/재적재와 이전/현재 제안을 나란히 보여준다. 경로에서 같은3D 개체로 이동할 수 있다. 기본4.5초의 이동과 로컬 편집 재생은 이해를 돕는 설명이며 실제 경로/작업 시간으로 표현하지 않는다. 기존 브랜드·의미 토큰을 재사용한다.

### 아이콘 중심 네비게이션의 기존 적용

mode: Operate. PC 주 메뉴는 76px 아이콘 레일을 기본으로 제공한다. 이름 펼치기 버튼으로 기존 폭과 이름을 복원하고 해당 브라우저의 선택을 보존한다. 메뉴 아이콘은 같은 24px·1.8px 선의 자체 SVG이며 이름·경로·역할별 표시·현재 위치를 유지한다. 마우스와 키보드 포커스로 용도 툴팁을 표시하고 Escape로 닫는다. 툴팁 위로 포인터를 옮겨도 읽을 수 있으며 화면 안에 배치한다.

태블릿·모바일에서는 열린 메뉴에 아이콘과 이름을 함께 표시한다. JavaScript가 없으면 이름과 기본 title 안내를 보존한다. 스크린리더 이름과 aria-current는 아이콘 표시와 독립이다. 메뉴의 열림/닫힘 문구와 aria-expanded는 같은 상태를 표현한다. 레일 폭은 component.navigation.rail-width 토큰에서 관리한다.

### 로고 기반 팔레트의 기존 적용

로고 대표색: navy #112C3F, teal #2CBBB7, orange #FE6D08, cream #FCF1D9. 청록 홈의 #087C80은 조작·링크용 짙은 톤, #DAF5F2/#EFFBFA는 선택/hover 면의 파생 톤이다. 오렌지 #B94D00은 포커스 등 대비가 필요한 용도에 쓴다. 로고와 아이콘은 교체하지 않는다.

primitive → semantic → component 연결을 유지한다. 버튼 기본/hover/disabled/focus, 메뉴 선택, 표 header/row-hover/border, 입력 background/text/placeholder/border/focus를 색상 토큰으로 분리했다. 메뉴·본문 구조·크기·업무 의미는 유지한다. 색만으로 상태를 구분하지 않는다.

참고: [Atlassian 색상 역할](https://atlassian.design/foundations/color/)처럼 브랜드 원색과 기능·상태별 역할을 구분하되, 실제 색상은 HarborLogi 로고를 따른다.

## 반출 업무 작업 공간

mode: Operate. [작업 공간 설계](docs/design/14-outbound-workspace.md). 반출 요청과 저장된 계획을 같은 업무 구조로 표시한다. PC는 논리 야드와 이동 순서를 주 영역에, 현재 판단과 관측 선택을 옆 영역에 둔다. 좁은 폭에서는 현재 판단→야드→계획 입력 순서로 읽는다. DOM에서도 현재 판단을 먼저 두며 CSS 배치만으로 읽기 순서를 뒤집지 않는다.

야드는 지정된3행×3열×3층을 기본3D 박스로 보여주며 층별 평면으로 전환할 수 있다. 평면에서1/2/3층을 고르면 같은 행·열의9개 위치를 보여 준다. 빈 위치를 포함해27개 주소를 보존한다. 재고와 계획 모두 같은 지도 표현을 쓰며 단계 변경 시 선택한 층을 유지한다. 임시 칸과 반출대는 설정된 맵 밖 보조 위치로 구별하고, 지도 없는 과거 원본은 좌표 미등록 목록으로 보존한다. 맵 정본은 `config/yard-layout.json`이고 각 계획은 당시 설치 기준의 사본을 따른다.

다칸 화물은 한 개의 이름과 이어진 점유 면으로 표시한다. 관측이 명시한 방향에 따라 colspan/rowspan을 쓰며1×1·2×1·3×1의 길이 비율을 유지한다. 같은 화물을 여러 개의 독립 카드로 반복하지 않는다. 구역별 화물 수와 점유 단위 수를 따로 표시하고 크기 원본·모든 점유 주소를 보존한다. 야드와 임시/반출 구역의 층 선택은 독립적이며 각 구역 안에서는 계획 단계를 바꿔도 선택 층을 유지한다. 크기 변경은 불변 이력으로 남고 과거 배치를 재해석하지 않는다.

관측과 각 이동 후 예상 배치를 버튼으로 전환한다. 반출 대상은 네이비 채움과 ‘반출 대상’ 글자, 재취급 대상은 오렌지 면과 ‘재취급 대상’ 글자로 표시한다. 단 번호·빈 단·장치/임시/반출대의 역할을 함께 읽는다. 화물 원본·실측 좌표를 합성하지 않으며 모든 미리보기는 저장된 계획만 사용한다. JavaScript 없이도 원본과 모든 예상 배치, 이동 순서, 근거를 읽을 수 있다.

웹 제출·업무 장부·수행 원본·물리 receipt·업무 완료 조건을 분리한다. 다른 계획을 보고 있어도 완료 조건의 팔/화물은 원래 실행 계획을 따른다. 요청/순위 변경·새 원본·오래된 관측은 목록과 상세 모두 재검토로 표시하며 실행 불명은 원래 작업 대조가 우선한다. 긴 원본 식별과 변경 원문은 펼침으로 보존한다.

3D는 관측에 명시된 행·열·층과 점유 길이를 같은 stage에서 읽어 회전·확대 가능한 상자로 투영한다. 화물 하나는 크기에 맞는 박스 하나다. 면의 ID는 같은 박스의 표기이며 새 화물을 만들지 않는다. 카메라는 박스를 가리지 않는 범위로 맞추고, 선택 목록은 가려진 화물도 ID·크기·주소로 확인하게 한다. 계획 재생은 사용자 시작으로만 현재 단계 버튼과 동기화하고 들어 올리기→이동→내려놓기로 순서를 설명한다. 실제 팔 궤적·실행 상태로 표시하지 않는다. 중지·동작 줄이기·페이지 숨김을 존중하며 JS/Canvas 실패 때 원래 평면도를 제공한다. [3D 설계](docs/design/19-container-3d-view.md).

현재 3D 구현은 [모델 작업 공간](docs/design/20-container-workbench.md)을 따른다. 로컬 WebGL 엔진에서 골/모서리/이름/조명/그림자를 가진 박스를 표시하고 회전·팬·휠/핀치 확대·선택 맞춤·선택 격리·전체 화면으로 살핀다. 카메라는 재생 때 강제로 재설정하지 않는다. 상세 패널은 선택 ID·크기·기준 위치를 보여주며 좁은 화면에서는 아래로 배치한다. 가상 편집에서는 전체 폭의 다른 색 배지와 ‘가상’ 위치·변경 수를 표시한다. 핸들/행·열·층 입력은 범위·겹침·전체 지지 검사 뒤 화면 사본만 바꾼다. 편집 중 계획 단계/재생/평면 전환은 잠기고 원본 복원으로 끝낸다. WebGL 실패 시 원본 평면 조회를 유지하며 소스 관측·계획·업무 완료는 바꾸지 않는다.
