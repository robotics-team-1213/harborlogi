# ROSY–Harbor 경계 분리 완료 검증

2026-10-29. 목표와 완료 기준은
[계획](../superpowers/plans/2026-10-29-complete-application-boundaries.md), 책임 정본은
[ADR0013](../adr/0013-independent-business-execution-and-platform.md)과
[설계24](../design/24-business-execution-boundary.md)를 따른다.

## 고정한 소스와 결과

- Harbor: `9b29c59465e245b9fb4aea602cbf3d7feb3cc85f`.
  경계 구현은 `0c9ca44cd0e27f8dba3e339982f3ff0d2f6bb0b1`, 마지막 수정은 제출자 감사 기록 보존이다.
- HTTP 상대 Fleet: `bb38fdcdd0140873e4e9d0133662982c1d6a30e0`, 기존 API v1.312.
- ROSY 공정 컴파일 분리는 별도 `refactor/cell-compile-boundary`의
  `3c1ad28faedd9007d2cdc9abbcc95e38a260578a`에 보존했다. 이번 HTTP 시험의 Fleet SHA와
  다른 검증이다. 해당 브랜치의 `docs/validation/cell-compilation-boundary-2026-10-29/result.md`가
  원격 영향 시험·설치 검증을 소유한다.

두 저장소의 main 착지·push·배포는 이번 완료 범위에 포함하지 않는다.

## 완성한 코드 경계

| 소유 모듈 | 책임 |
|---|---|
| ROSY Fleet·CORE·owner | admission·독립 승인·실행·자원 점유·장치 권한과 원본 사실 |
| `harbor/fleet_contract.py`, `rosy_http.py`, `execution.py` | 공개 HTTP 요청·검증·원본 결속·정규화 결과와 읽기 전용 표시용 projection |
| `harbor/workflow.py`, `transport.py` | 저장 의도·CAS·HOLD/불명 복구·검증 결과 채택·독립 업무 근거에 따른 전이 |
| `harbor/ledger.py` | 저장 payload 해석·장부 행 조회·read-only 조회·소비한 근거 조회 |
| `harbor_web/commands.py` | 권한·revision·업무 명령·최초/후속 제출·한 단계 진행·감사·종착 이력 |
| `harbor_web/executor.py` | 대상 선택·주기·bounded pass·backoff |
| 화면·pipeline | 검증된 projection과 업무 상태 표시 |

ARM·Cell·TRIP 내부 결과는 같은 `ExecutionSnapshot`을 사용한다. 원래 key·mission·attempt와
원본 job/receipt를 보존하며 Harbor 진행 의미를 별도로 제공한다. 기존 binding 메서드,
`BusinessLedger.propose()`의 mission ID, TransportLedger의 job dict 반환은 호환 진입점으로
유지한다. 승인·동작 성공·물리 확인·Harbor 업무 완료를 합치지 않았다.

장부 schema와 Fleet API는 바꾸지 않았다. Cell compile-before-intent와 그 뒤 stage CAS,
원래 요청으로만 대조하는 불명 복구, 단일 writer, 취소 요청과 실제 정지의 구분,
종착 명령 조작 거절, 복원 차단 상태의 읽기 전용 대조를 유지했다.
Workbench는 HTTP 뒤 장부를 다시 읽어 변경 시 STALE로 표시한다. 장부 조회는 초기화나
복구를 하지 않으며, command_detail의 기존 출력 필드 제한도 유지한다.

## 시험과 리뷰

| 검증 | 실제 결과 |
|---|---|
| 핵심 전체 `python -m unittest discover -s tests` | 180 testsRun, 실패·오류 0. 미설정 실제 HTTP 클래스 setUpClass 1건 skip |
| 웹 전체 `python -m harbor_web.manage test harbor_web.tests --noinput -v 1` | 238 실행 통과, skip 0 |
| 독립 설치 wheel의 핵심 회귀 | `python -I`, 178 실행 통과, skip 0 |
| 실제 Fleet ARM HTTP | 1 실행 통과, skip 0 |
| 실제 Fleet 인증 웹 ARM HTTP | 1 실행 통과, skip 0 |
| 실제 Fleet TRIP HTTP | 1 실행 통과, skip 0 |
| 실제 Fleet Cell HTTP | 3 실행 통과, skip 0 |

기본 핵심 discovery의 skip은 클래스 초기화 skip이다. `testsRun`에서 이를 차감해
통과 수를 계산하지 않는다. 별도 명시한 실제 HTTP 실행은 위의 6개다.
설치 검증은 배포 대상이 아닌 `tools.design_tokens` 개발 시험 2개를 제외했으며,
이 2개는 소스 전체 시험에서 통과했다. 설치 검증의 Harbor import 위치가 venv의
`site-packages`임을 assert하고 소스 application 경로를 Python 검색 경로에 넣지 않았다.

웹은 `harbor_web.settings`, test mode와 임시 DB로 실행했다. 미수집 web-static 경고는
있었고 브라우저 렌더링 검증으로 계산하지 않는다. 경계 AST 검사 4개는 실행기의 장부/HTTP
접근, 소비자의 직접 SQL/저장 payload 해석, 업무·화면의 Fleet 상태 해석과 직접 endpoint
호출을 막는다. 추가 동작 시험은 단일 readback·fact 저장·잘못된 완료 receipt의 HOLD·재POST
거절·정규화 TRIP·실제 제출자 감사 보존을 검증한다.

새 submit 시험 2개와 경계 검사 3개를 구현 전에 실행해 실패를 확인했다.
별도 읽기 전용 reviewer가 계획과 전체 diff를 검토했다. 표시 projection의 이동 중 변수,
업무 HOLD 비교와 TRIP 내부 결과 경계를 보완했고, 최종 제출자 감사 결함은 서로 다른
준비자·제출자의 RED→GREEN 시험으로 수정했다. `9b29c59` 최종 리뷰의 잔여 findings는 없다.

## 원격 HTTP와 설치 근거

승인된 AI 시험 PC에서 메모리 6GiB·CPU 200% scope로 실행했다. 시작 전 여유는
172GiB, 사용률 62%로 저장소 게이트를 통과했다. 기존 하네스 의존 venv를 재사용하고,
별도 깨끗한 venv에는 Harbor wheel과 web 의존성만 설치했다. 시험 전후 양쪽 checkout은
clean이며 SHA를 확인했다. 하네스 부모만 플랫폼을 로드한다.

실행은 `tools.integration.real_fleet_http`에 고정 platform root/SHA와 격리된 app-python을
전달하고 `--arm-only`, `--web-arm-only`, `--transport-only`, 기본 Cell 모드를 각각 사용했다.
ARM과 웹 ARM은 각각 synthetic UDS submit 1·lookup 1 및 PRE_DISPATCH/POST_ACTION proof를
확인했다. 합성 observer와 simulation owner이므로 실물 동작 증거가 아니다.

첫 ARM 시도는 긴 임시 경로의 `AF_UNIX path too long`으로 시작 전에 실패했으며 제출 0건이다.
소스 변경 없이 짧은 작업 전용 TMPDIR로 바꾼 뒤 위 6개 HTTP가 통과했다.
처음의 설치 전체 discovery도 개발용 tools 미포함 오류로 실패했고, 이를 성공으로 세지 않았다.
실패 로그와 최종 로그를 함께 회수했다.

로컬 원본 로그: `%TEMP%/harbor-boundaries-evidence-20261029/`.
로그 hash와 결과를 회수한 뒤 종료된 원격 소스·venv run과 짧은 소켓 임시 run을
각각 정리했다. 원격 잠금이 없고 해당 경로를 참조하는 프로세스가 없음을 확인했다.
이번 로컬 소스 운반 폴더와 임시 검증 venv는 휴지통으로 옮겼으며 로그는 유지한다.

| 로그 | SHA256 |
|---|---|
| `arm.log` | `b80db62538f5c3b316283127b40f1de5e31770ff50885a077819dd2985433565` |
| `web-arm.log` | `37b11245a363257737c9b187d7e92e5e24e7bb0bc227d18d3d7d24818170d6b8` |
| `transport.log` | `9fbbf7d7c8fcf05604784cbd0a0319fe3b15f8c108fc690285ea522a36b86604` |
| `cell.log` | `3b90b8abd62f190651553fbaf2d493fb1556428dd6d91f607254b179a9d15eb7` |
| `installed-core.log` | `87cadf2cb1f38639f2a973e0c65c2011b6bcd99fa7ef3c038ba4895cdf8f74c9` |
| `install.log` | `eea8d64a9314afb6487266832eec7de2539f2485e1a86705800cf3a91e81320a` |

## 완료 범위와 CE

이 기록은 코드·계약·호스트 설치/HTTP 검증의 완료다. 브라우저 시각 회귀·현장 producer의
진위·장치 동작·운영 서비스 활성화·실물 수용·main 병합·push를 검증하거나 수행하지 않았다.

CE: 새 교훈 기록 — 원격 저장소 정책의 긴 run 이름과 Unix 소켓 경로 제한이 충돌하는
환경 제약을 실제 실패·동일 SHA 재실행으로 검증해
[교훈](../../../docs/solutions/integration-issues/unix-socket-path-budget-2026-10-29.md)에 남겼다.
일반 경계 리팩터링과 감사 수정은 코드·회귀 시험·설계에 드러나므로 별도 교훈으로 늘리지 않았다.
교훈의 frontmatter와 기계적 path/link 검사도 통과했다. 기존 `CONCEPTS.md`는 없어
어휘 파일은 만들지 않았고, 작업 공간 지침의 교훈 검색 경로를 유지했다.
