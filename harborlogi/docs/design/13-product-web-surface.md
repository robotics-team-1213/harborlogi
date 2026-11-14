# Harbor 제품 웹 화면·동작 계약

상태: plan12 v0.2 승인에 따른 구현 명세. 기존 브랜드·운영 도구 화면을 확장한다. mode: Operate. 코드로 작동하는 화면을 검증하며 새 브랜드/홍보 시안을 만들지 않는다.

2026-10-25 후속: [반출 업무 작업 공간](14-outbound-workspace.md)이 반출 요청/계획 상세의 현행 구성을 정한다. 전역 탐색에 `/outbound/` 반출 관제를 추가하고 기존 재고 영역은 ‘재고·관측’으로 표시한다. 입고·현장·실행 조작의 기존 권한/경계는 유지한다. 아래 FIRST VIEWPORT는 확장 전 구현의 기록이며 현재 반출 화면은 14와 실제 브라우저 검증을 따른다. 토큰은 로고 기반 팔레트 보강 후147개다.

## Direction contract

THESIS: 필요한 조치에서 원래 요청과 근거로 이어진다. 입력·관측·계획·실행을 한 상태로 축약하지 않는다.

OWN-WORLD: 기존 navy 탐색, paper 작업면, white 자료 표, teal 주요 조작, orange 보류와 현재124 토큰을 계승한다. 이름·사유·출처를 먼저 읽고 긴 ID는 상세에서 확인한다.

STORY: 담당자가 조치 사유를 찾고 화물/요청을 입력한다. 현재 관측을 고정해 계획을 검토하고, 지원되는 단계만 ROSY로 인계한다. 현장 담당자는 자기에게 배정된 한 회차를 확인한다.

FIRST VIEWPORT: PC 왼쪽200px 전역 탐색, 위64px 계정/환경, 본문32px 여백. 제목과 주요 조작 한 줄, 그 아래 보류 사유가 있는 실제 업무 표를 둔다. 계획 상세는 먼저 검토 상태/시각/기준, 다음 같은 순서의 관측·예상 배치, 이어 단계 표와 실행 조건을 둔다. 태블릿은 접이식 탐색과 단일 열, 현장 확인은 대상/차량/회차와 세 결과 선택이 우선이다.

FORM: 기존 운영 현황/업무 화면의 확장. 알려진 데이터 표·폼·상세 패턴을 유지한다. concept-seed/comp 선택은 새 시각 세계를 만들지 않는 이 확장에 적용하지 않았으며, 생성기 실행 실패를 뜻하지 않는다. 서명 상호작용은 원래 관측을 유지한 채 계획과 실행 근거를 나란히 대조하는 것이다.

FINISH: unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, DESIGN.md, and every shipping raster carrying its provenance

## 경로·동작·권한

| 경로 | 읽기 | 쓰기/책임 |
|---|---|---|
| `/` | viewer/operator/admin. field-only는 자기 확인으로 이동 | 조치 필요 요청·실행 불명·미등록 기준 안내 |
| `/outbound/` | viewer/operator/admin | operator 우선순위 변경·재취급 단계 적용(`/outbound/reorder/`, `/outbound/tiers/apply/`) |
| `/cargo/`, `/cargo/new/` | viewer/operator/admin | operator 화물 등록. 중복/단위 검사, 배치를 만들지 않음. 화물 이력·바닥 면적 상세는 읽기 |
| `/requests/`, `/requests/new/`, `/requests/<id>/` | viewer/operator/admin | operator 입고/반출 요청·revision 변경·관측 선택 후 계획. 요청 시간선은 읽기 |
| `/inventory/`, `/inventory/import/` | viewer/operator/admin | admin 관측 원본 등록(`/inventory/import/`은 admin만). DECLARED/SYNTHETIC만 가능. 인증 관측으로 승격 금지 |
| `/plans/<id>/` | viewer/operator/admin | operator 검토 후 현재 단계 제안. 과거 계획/현재 freshness·불명 gate 구분 |
| `/work/`, `/work/<id>/` | `/work/`는 viewer/operator/admin. 상세는 field 포함(field는 본인 배정으로 좁힘) | operator 원래 key 대조·취소. 완료/안전 강제 버튼 없음 |
| `/rearrangement/` | viewer/operator/admin | 자동 재배치 허용 범위와 실행 기록 읽기 |
| `/field/`, `/field/<id>/` | field 자신의 배정만; operator/admin은 관리 맥락 | operator 배정(`/field/new/`)/출발 전제 기록, field 본인 현장 확인 |
| `/records/`, `/records/compare/` | viewer/operator/admin | operator 측정 자료 등록(`/records/measurements/new/`), 같은 조건 비교 |
| `/settings/`, `/settings/conditions/new/`, `/settings/maps/new/`, `/settings/integration/` | admin | 설치 기준 참조 등록, 구역 맵 편집, ROSY 연동 조건 확인. Fleet 실행 권한 없음 |
| `/workflow/` | viewer/operator/field/admin (역할 없는 계정 제외) | 역할 시나리오별 업무 안내. 쓰기 없음 |
| `/accounts/*`, `/admin/` | 본인 계정, active staff+명시적 admin | Django 계정/비밀번호/역할 정책. 앱 operator 권한 자동 부여 없음 |

화면 접근과 메뉴 노출의 단일 원본은 `harbor_web/access.py`다(PAGES의 경로·역할, 메뉴 그룹). 위 표는 이 원본과 view의 `require_roles`와 일치해야 하며 `harbor_web/tests/test_access_policy.py`가 등록된 모든 정적 경로를 역할별·역할 없는 계정으로 HTTP 요청해 어긋남을 잡는다. 근거와 시나리오는 [역할 시나리오별 화면 권한과 메뉴](34-role-scenario-access-and-menus.md)다. 전역 탐색은 역할이 열 수 있는 항목만 업무 처리(운영 현황·반출 관제·화물·요청·실행·대조·자동 재배치) / 재고·근거(재고·관측·기록·검증) / 현장 / 관리(설정·계정) / 도움말(업무 안내)로 묶어 보인다. 계획은 요청 상세에서 생성한다. 장비 전체 운영은 Fleet로 인계한다. 목록 검색/필터는 query string과 뒤로 가기에서 유지하고 pagination을 사용한다.

오류는 입력 보존과 필드별 이유, 403은 권한과 역할별 복귀 경로(조회 역할이 있으면 운영 현황, 현장 확인만 있으면 현장 확인으로 돌아가고, 역할이 없으면 역할 부여 요청 안내와 비밀번호 변경 링크만 보인다), 409는 변경 사유와 현재 원본 재조회, 미연동은 필요한 계약과 담당 경계를 표시한다. 정보가 없는 상태를 정상0으로 표시하지 않는다. 브라우저 표 가로 스크롤은 표 영역 안에 한정하고 페이지 전체 overflow를 만들지 않는다.
