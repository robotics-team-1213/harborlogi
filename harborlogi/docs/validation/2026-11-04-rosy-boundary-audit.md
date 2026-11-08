# 2026-11-04 Harbor–ROSY 역할 경계와 통신 점검

사용자 요청: "ROSY와 Harbor의 역할이 나뉘고, 그러면서 서로 역할에 따라 통신이 되는 유기적인 부분도 되어 있는지 체크".

대상:
- Harbor: `feat/port-next-steps` 작업 트리.
- ROSY Platform: 로컬 `main` `54e653c4cc`. 이 노트북의 checkout이며, 공개 main과의 차이는 확인하지 않았다.

방법: 코드 읽기와 로컬 시험이다. 실제 Fleet 서버나 시험 PC에는 연결하지 않았다.

## 결과

| 항목 | 판정 | 근거 |
|---|---|---|
| 역할 분리 | 정상 | Harbor 제품 코드는 ROSY 패키지를 import하지 않는다. ROSY의 operations·applications·contracts에 항만 업무어가 없다. 이번에 `tests/test_module_boundaries.py`의 `test_product_code_reaches_rosy_only_over_fleet_http`로 import 금지를 시험으로 고정했다(이전에는 관례) |
| 통신 경로 | 정상(경로·메서드 기준) | Harbor가 쓰는 Fleet 경로는 모두 ROSY main에 있다: `/api/fleet/session`, `cell-app/compile`·`proposals`, `cell-jobs/{id}`, `trip-jobs`(POST·GET·`?request_key`), `arm-transfers/jobs`(POST·GET·`?request_key`·`{id}`·`/physical-receipt`·`/physical-evidence`·`/rechecks`·`/cancel`). 제안 경로는 `operator`·`service`를 허용한다 |
| 요청·응답 필드 | 미확인 | pydantic 스키마와 필드 단위로 대조하지 않았다 |
| 기준 플랫폼 pin | 낡음 | `docs/platform-contract.md`의 기준은 v1.292 커밋이고, 본문은 v1.334까지 소비한다. 그 사이 ARM·cell-app·trip route 파일에 커밋이 있었다. 고정 HTTP로 다시 확인하기 전에는 기준을 바꾸지 않았다 |
| R 경유(ADR 0018) | 부분 | D-965 팔 위임(v1.353–1.355)과 R의 ARM 중계가 main에 있다. R 중계에 `physical-evidence`·`rechecks`가 없어(v1.359 착지 대기) R을 거치면 업무 근거·완료 재관측이 되지 않는다. `cell-app/compile`·`proposals`는 중계 대상이 아니고 `require_named_operator`라 service 토큰으로 되는지 확인하지 못했다 |
| 실행 가능한 시험 | 통과 | `python -m unittest discover -s tests` 통과(실제 Fleet HTTP 1건은 `HARBOR_INTEGRATION` 미설정으로 skip). Django 283개 통과 |

## Harbor가 우회하지 않는 것(ROSY 쪽 일)

- R 중계에 `physical-evidence`·`rechecks` 추가. 이것은 v1.359로 착지 대기 중이다. Harbor가 셀 관제에 직접 붙어 메우는 것은 ADR 0018이 금지한다.
- R로 cell-app compile·proposal을 보낼 때 응답을 확정해야 한다(위임 또는 명시적 거절).

## 운영 R 실측 (같은 날, main `5e5cde9` 배포 직후)

robttt에 배포한 Harbor 컨테이너 안에서 설정된 R origin과 서비스 토큰으로 읽기 전용 호출만 했다. 없는 request key만 조회했으므로 작업을 만들거나 바꾸지 않았다.

| 호출 | 결과 | 뜻 |
|---|---|---|
| `GET /api/fleet/session` | 200, `harbor-lan-service`·`service` | 신원과 역할 연결은 동작한다 |
| `GET /api/fleet/arm-transfers/jobs?request_key=<없는 키>` | 503 `ARM_TRANSFER_JOB_UNAVAILABLE` | 운영 R에서 ARM 경로를 쓸 수 없다. 설치된 R 후보 `480de9e`(플랫폼 2026-11-02 게시)에는 D-965 팔 위임 커밋(v1.353–1.355)이 없다 |
| `GET /api/fleet/trip-jobs?request_key=<없는 키>` | 404 | 없는 키에 대한 응답이다. 경로 존재 여부는 이것만으로 판정하지 않는다 |

따라서 **현장에서 Harbor의 실행 경로(ARM)는 아직 닫혀 있다.** 열려면 플랫폼이 D-965(그리고 근거·재확인 중계 v1.359)를 공개 main에 게시하고, 서명 후보로 R을 갱신해야 한다. Harbor에서 셀 관제에 직접 붙어 우회하지 않는다(ADR 0018).

이송: Harbor 라이브러리의 `client.trip()`은 `POST /api/fleet/trip-jobs`를 쓴다. 이 경로는 플랫폼에서 운영자(`require_named_operator`)만 허용하므로, Harbor 서비스 신원으로는 제출할 수 없다. 앱 서비스용 경로는 D-908 `trip-proposals`(제안 → 다른 운영자 승인, 시뮬레이션 전용)다. 지금 Harbor 웹 제품은 이송을 제출하지 않고(실행기는 ARM만), 이송 장부는 라이브러리와 통합 시험에서만 쓴다. 제안 경로 구현은 PR 없이 `feat/trip-proposal-transport`(2026-10-28)에 있다. 이 구현은 2026-10-29 경계 정리 전 코드라, 합치려면 Fleet 상태 해석을 `harbor/execution.py` 쪽으로 다시 맞춰야 한다.

## 확인하지 않은 것

- 고정 양 끝 SHA의 실제 Fleet HTTP 계약 시험(`tests/integration/test_real_fleet_http.py`). 깨끗한 플랫폼 checkout과 시험 PC가 필요하다.
- 운영 R에서 작업을 실제로 만드는 호출. 위 실측은 읽기만 했다.
