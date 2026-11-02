# Harbor 역할·앱·UI/UX 상세설계

상태: **상세설계와 단계별 구현 기준**, 2026-10-25. 관제 PC 중심 + 현장 태블릿 보조, 브라우저 웹앱은 사용자 확정 사항이다. 후속 승인으로 로컬 조회 웹 구현을 시작했다. 현재 실행 범위는 [웹 안내](../web-workbench.md), 운영 미해결 항목은 아래 설계를 따른다.

## 읽는 순서와 문서 소유

| 순서 | 문서 | 결정할 내용 |
|---|---|---|
| 0 | [제품 맥락](../../PRODUCT.md), [용어](../../CONTEXT.md) | 제품 목적·현재 기능·확정/미정의 구분 |
| 1 | [역할과 업무 흐름](01-roles-and-workflows.md) | 사람·장치·소프트웨어 책임, 승인·확인·완료 |
| 2 | [UI/UX 상세설계](02-ui-ux.md) | 메뉴·화면·조작·상태·PC/태블릿·예외 |
| 3 | [앱 구성과 계약](03-application-and-contracts.md) | 웹 경계·데이터·인증·현재 API·계약 결손 |
| 4 | [구현 순서와 수용 시험](04-delivery-and-acceptance.md) | 한 단계씩 만들 산출물·선행 조건·실패 시험 |
| 5 | [Fleet 승인·운영 인계](05-fleet-handoff.md) | 플랫폼 담당과 검토할 승인 화면·조회 데이터·신원·복귀 대조 |
| 6 | [브랜드 웹·조회 연동 구현](06-web-integration-plan.md), [디자인 시스템](../../DESIGN.md) | 첫 구현 범위·검증·시각 토큰과 운영 제한 |
| 7 | [디자인 철학](07-design-foundation.md) | 조치 필요 첫 화면, 판단 중심의 원칙·브랜드·정보 위계 |
| 8 | [네비게이션](08-navigation.md) | 전역 메뉴/목록/업무 탭 분리, PC/태블릿, URL·복귀·Fleet 인계 |
| 9 | [토큰·패턴 계약](09-tokens-and-patterns.md), [토큰 JSON](../../design/tokens.json) | 원색→의미→컴포넌트, 상태·밀도·포커스·적용 순서 |
| 10 | [네비게이션 페이지 구현](10-navigation-pages-plan.md) | 실제 운영 현황·업무·탭·drawer·복귀와 검증 범위 |
| 11 | [제품 흐름·재고/반출 계획](11-retrieval-planning-page.md) | 사용자 결과와 기능 결손 대조, 기존 planner를 연결한 합성 미리보기 |
| 12 | [제품 웹 완성 범위](12-web-product-completion.md), [화면·동작 계약](13-product-web-surface.md) | 계정·영속 입력·실행 인계·지원 계약과 남은 운영 게이트 |
| 14 | [반출 업무 작업 공간](14-outbound-workspace.md) | 업무별 야드·단계 예상·현재 판단·원래 실행·완료 조건·변경 이력 |
| 15 | [지정된3×3×3 야드](15-configured-yard-grid.md) | Y11~Y33, 칸별3층·총27개 주소, 설정된 행·열과 관측 누락 거절 |
| 16 | [다칸 점유 화물](16-multicell-cargo.md), [ADR0008](../adr/0008-multicell-cargo-occupancy.md) | 한 개체의 점유 영역·전체 지지·크기 원본·원자적 재취급·Django 유지 |
| 17 | [페이지별 운영 흐름](17-operator-page-flow.md) | 페이지 목적·담당자·다음 조치, 원본 선택 유지·확인 만료·기록 범위 |
| 18 | [Harbor–ROSY 파이프라인](18-harbor-rosy-pipeline.md), [편집 가능한 흐름도](harbor-rosy-pipeline.drawio) | 실제 API 호출·독립 승인 화면·실행/물리/업무 근거·배포 기준 |
| 19 | [컨테이너3D 보기](19-container-3d-view.md) | 입체 박스·회전/확대·선택·계획 순서 재생, 원본·평면도 동기화와 복구 |
| 20 | [모델 뷰어와 가상 배치 편집](20-container-workbench.md) | WebGL 모델·자유 카메라·핸들/입력 편집·논리 검사·복원·모듈 인터페이스 |
| 21 | [버전이 있는 구역·칸 편집](21-versioned-map-editor.md), [ADR0009](../adr/0009-versioned-flexible-maps.md) | 가변 구역·칸·역할 편집, 검증 후 새 기준 등록, 과거 기준과 실행 권한 보존 |
| 22 | [자동 배치·변경 기반 재계획](22-automatic-placement.md), [ADR0010](../adr/0010-automatic-placement-and-replanning.md) | 반출 순서 기반 입고 추천·계획상 예약·변경 이벤트·원본/실행 보존 |
| 23 | [물량·재취급·재계획 비교·느린 재생](23-operations-replan-playback.md), [ADR0011](../adr/0011-operations-replan-explanation-and-playback.md) | 관제 물량과 근거, 화물별 분리/복귀, 이전/현재 경로 비교, 가상 이력 재생 |
| 24 | [업무 실행기와 플랫폼 경계](24-business-execution-boundary.md), [ADR0013](../adr/0013-independent-business-execution-and-platform.md) | 목표 달성까지의 업무 운용, 연동 어댑터, 지속 실행·복구·재계획, ROSY의 독립 실행 권한. 구현 전 설계 |
| 25 | [Fleet 연결 설정과 계약 확인](25-fleet-connection-bridge.md) | 관리자 연결 프로필·서버 자격·명시적 신원/ARM 조회 검사·기존 제출/대조 설정의 일치 |
| 26 | [2D·3D 공통 보기와 계산 추천](26-linked-yard-and-ranked-recommendation.md), [ADR0014](../adr/0014-linked-spatial-views-and-ranked-planning.md) | 정확한 전체 점유 주소·양방향 선택·가상 편집, 완전한 반출 후보 비교·지지 그래프·탐색 한계 |
| 27 | [독립 서비스 인증과 협력](27-independent-integration-and-service-trust.md), [ADR0015](../adr/0015-service-identity-and-independent-integration.md) | 사람/기계/승인/관측 신원 분리, 공개 계약·데이터 소유·자격 교체·실제 수용 단계 |
| 28 | [재계획과 취소의 상태 구조](28-replan-and-cancel-structures.md) | 주문당 명령 이력(활성 유일 제약), 취소 종착 상태와 열린 계약 질문, 재계획 착수 조건과 수용 시험. 구현 전 상세설계 |
| 29 | [실행 binding 확장 — 다칸 배치와 팔 배정](29-spatial-binding-and-arm-assignment.md) | 다칸 입고 배치 binding, (이동·팔)별 설치 대응으로 도달 원본 교체, preflight는 설치 검증 보조로 한정, 열린 설치 질문. 구현 전 상세설계 |
| 30 | [ARM preflight 소비를 위한 설치 catalog digest](30-arm-preflight-catalog-digest.md) | 결정 기록(2026-10-27): C 유지 + A(설치 산출물 digest)를 설치 표준으로 채택. preflight 소비는 별도 변경으로만 연다 |
| 31 | [업무 실행기 착수 계획](31-business-executor-implementation-plan.md) | 설계24의 P0–P4 단계별 구현·시험·수용 기준, 현재 구현 지점 대조, ADR0013 검토 결론 |
| 32 | [화물 재배치 기준](32-cargo-rearrangement-criteria.md) | 재배치 종류(R0–R3), 순서 판정 통일, 반출 불가·막힘·교차 지지·수용 위치 지표, 회전을 목적으로 하는 재배치, R2 자동 실행 허용 범위, 인자별 기준. 판단 구조는 ADR0017 |

새 화면을 설계할 때는 07→08→09를 먼저 읽고 01~05의 역할·계약·수용 조건과 대조한다. 운영 현황/업무/사용 안내와 토큰은 10에서 로컬 페이지로 구현했다. 전체 제품 메뉴·운영 권한·실물 수용까지 완료한 것은 아니다.

Harbor 업무·화면·내부 장부는 이 저장소에서 설계한다. ROSY의 계약·승인·정지·owner 구현은 플랫폼 저장소가 소유한다. 이 설계가 플랫폼 API를 새로 등록하거나 기존 ADR을 변경하지 않는다.

## 기준 자료와 실제 상태

기준 Harbor commit `176880cfefd944f2dac45c318a9aa1236f6c5ae4`. 아래 자료와 실제 코드를 대조했다. 역사 기록의 조사 SHA를 현재 기능의 증거로 사용하지 않는다.

| 자료 | 설계에 반영한 사실 |
|---|---|
| ADR0005·CONTEXT.md | S 외부 운영, A/B 적층·재취급·반출, 위치 두 층, 완료3요소 |
| ADR0006·platform-contract.md | 중립 ARM 제안/조회/receipt와 Harbor 업무 판정의 분리 |
| observation-acceptance.md | source 이름만으로 producer 인증이 되지 않으며 운영 어댑터는 미구현 |
| site.py·site.example.json·availability.py | 공급/적층·이송 자원과 후보 제외, 상시 컨베이어, 직렬 이동 제한 |
| departure.py | 차량별 신선한 S 퇴피 확인의 한 번 소비; 공급 인계 회차와 TRIP의 원자 결속은 없음 |
| outbound.py·process.py·workflow.py·transport.py | 결정적 계획, binding, 업무/이송 장부와 불명 상태 보존 |
| __main__.py·README·브랜드 자산 안내 | 현재 CLI 예제이며 웹 UI가 없고, 브랜드 이미지는 컨셉 자산 |
| 중립 ARM/기존 Cell/TRIP validation | HTTP simulation 증거. 운영 UI·실물 안전 수용 증거가 아님 |

팀의 현행 UR/SR 정본과 실사용자 인터뷰의 추가 대조는 미완료다. 이번 설계의 `UX-*`, `ARCH-*`, `AC-*`는 추적용 초안 ID이며 승인된 팀 UR/SR 번호가 아니다. 내부 팀 문서·계정·환경 주소를 공개 저장소에 복사하지 않는다.

## 권장 구성과 대안

**권장안:** Harbor는 업무 워크벤치, 태블릿은 현장 확인, Fleet은 실행 승인·로봇 운영·정지·복구를 담당한다. 같은 업무 ID/mission ID를 표시해 사용자가 이어서 대조한다. 초기에는 별도 앱으로 전환하고, 검증되지 않은 로그인 공유·자동 승인·iframe 통합을 전제하지 않는다.

장비 중심 종합 관제를 Harbor에 다시 만들면 Fleet 권한과 기능이 중복된다. 읽기 전용 현황판만 만들면 계획·보류 이유·근거 대조를 처리할 수 없다. 권장안은 기존 역할 경계를 유지하면서 업무 조작을 제공한다. 웹 rendering/배포 stack은 별도 미정이다.

## 첫 제품 범위

2026-10-25 후속: [제품 완성 계획](12-web-product-completion.md), [화면·동작 계약](13-product-web-surface.md), [ADR0007](../adr/0007-authenticated-harbor-web.md), [실행 안내](../product-web.md). 팀 자체 계정·역할 권한은 사용자 확정이며 새 제품 웹 구현/검증을 진행한다. 아래 첫 범위·미정 표는 초기 설계 이력이다. 현행 경로/권한과 남은 운영 계약은 후속 정본을 따른다.

- PC: 업무 목록, 재고·계획 검토, 작업 상세/근거, 예외, 장비 현황, 설정의 최소 화면.
- 태블릿: 배정된 현장 확인 목록과 S 퇴피 확인 상세. 차량별 확인이 다른 공급 인계 회차에 재사용되지 않아야 한다.
- 첫 실제 조작: 계약·인증이 준비된 단일 ARM 제안. 다단계 반출 전체와 TRIP 출발은 각각 선행 결손을 해소한 뒤 개방한다.
- 완료 버튼으로 관측·claim·Action 결과를 만들지 않는다. 고정 source의 simulation 성공을 운영 가능으로 표시하지 않는다.

## 검토가 필요한 결정

| 항목 | 추천안 / 미정 | 미정일 때의 처리 |
|---|---|---|
| 실제 사람 역할·겸임 | 업무 담당/현장 확인/Fleet 승인/설치·운영 관리 책임을 구분 | 역할 화면은 가설로 표시; 운영 권한 발급 보류 |
| 승인자의 독립성 | 실제 사람이 달라야 하는지, principal만 달라야 하는지 확정 필요 | 현 계약이 서로 다른 사람을 증명한다고 주장하지 않음 |
| 웹·인증 stack | 서버 측 Harbor 웹 계층 + 기존 domain 모듈 재사용 권장 | framework/SSO·권한 모델 선택 전 운영 쓰기 비활성 |
| 관측·supply handoff episode | 운영 source 인증·ID 대응·신선도·회차 결속 필요 | 관련 확인/완료/출발 차단 |
| 시각 디자인 | 기존 자산 유지, 업무 도구의 정보 위계 우선 | 픽셀 시안·디자인 시스템 확정은 다음 검토 |

이 문서 묶음의 완료는 설계 초안의 완성을 뜻한다. 구현 착수는 04의 선행 조건과 검토 결정을 따른다.
