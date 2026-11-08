# Harbor 종착 상태와 현재 실행 조회 경계

2026-10-29. 코드·시험 commit은 `b2e1f2a07ce7b6b4cd4014770a6bff164524feab`이며,
기준은 `779aa8e`다. 이전 ARM 대조 분리에서 남은 application service의 상태·조회
책임을 보강했다. Fleet 공개 API·실행 승인·장치 제어·DB schema는 바꾸지 않았다.

## 재현과 해결

- 종결된 명령의 reconcile/cancel 호출이 장부와 Fleet을 조회한 뒤 명령을 다시 활성
  상태로 바꿀 수 있었다. 공유 `_operation`이 모델의 `ACTIVE_COMMAND_STATES`를 재사용해
  허용 상태를 확인하고, 종결 명령의 실행 조작을 부작용 전에 거절한다.
- 마지막 단계의 confirm 뒤에는 `step_index == len(moves)`이고 현재 mission이 없다.
  command_detail이 이전 fact를 해당 index로 결속하려다 IndexError를 냈다. 현재 mission이
  있을 때만 현재 job·receipt를 검증하며, 최종 근거 대기·완료는 장부와 과거 이력으로 읽는다.
- 전체 회귀에서 복원 차단 명령의 원래 GET 대조를 유지해야 하는 기존 계약을 확인했다.
  그 경로는 command identity·장부 payload/digest·원래 request key·mission·principal을
  검증한 fact만 추가한다. command 상태·version과 장부를 바꾸거나 제출·재개하지 않는다.

장부 binding 검사는 일반 준비 경로와 복원 조회 경로가 같은 helper를 사용한다.
종결 이력과 복원 후 사실 확인을 분리하되 검증 규칙을 복제하지 않았다.

## 검증

기준 코드에 새 회귀 시험을 먼저 추가한 RED에서 history suite 8개 실행 중
11 assertion 실패와 IndexError 1건이 발생했다. 수정 후 같은 8개 시험이 통과했다.
최종 전체 웹 회귀에는 복원 성공·timeout의 불변성 검사도 포함했다.

| 명령 | 결과 |
|---|---|
| `python -m unittest discover -s tests -v` | 173 실행, 172 통과, 1 skip |
| `python -m harbor_web.manage test harbor_web.tests --noinput -v 1` | 236 실행, 모두 통과 |

웹 시험은 `harbor_web.settings`, test mode, 임시 DB에서 실행했다.
별도 읽기 전용 reviewer가 최초 수정과 복원 예외 보완을 검토했고 지적 사항은 없었다.
미수집 web-static 디렉터리 경고는 있었으며 브라우저 렌더링 검증으로 계산하지 않는다.

회귀가 확인한 범위:

- CANCELLED/COMPLETED/일반 BLOCKED의 submit·reconcile·resume·cancel 거절과
  command·장부·Fleet 호출·audit/fact의 무변경.
- 실제 취소→운영자 종결 후 웹 POST 거절.
- 합성 3단계 업무의 최종 근거 대기와 완료 화면 GET 200, 장부 무변경,
  현재 job 없음과 과거 CommandFact 보존.
- 복원된 원래 실행의 GET 대조와 timeout 거절. `resume=True`도 새 실행을 열지 않음.

## 한계

기본 discovery의 실제 Fleet 통합 skip은 성공 증거가 아니다. 이 commit의 고정 실제
Fleet HTTP 통합·브라우저·실물·운영 수용은 실행하지 않았다. 이전 commit의 통합 기록을
이 수정의 실행 근거로 대체하지 않는다. main 병합·push·CI 실행도 포함하지 않는다.

CE: 해당 없음 — 원인과 복원 예외의 판단이 공통 코드·기존 설계·회귀 시험에 드러나며,
별도의 장치 성질이나 운영 복구 순서를 검증한 작업은 아니다.
