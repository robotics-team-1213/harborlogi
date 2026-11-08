# 재계획과 취소의 상태 구조

2026-10-27 설계, 2026-10-29 종착 이력·조회 경계 보강.
결정: [ADR0013](../adr/0013-independent-business-execution-and-platform.md).
상위 계획: [설계31 P3](31-business-executor-implementation-plan.md).
아래 제안은 설계 당시 배경이며, 현재 구현은 Command의 주문별 이력과 공유 업무 서비스에서
검증한다. 이 문서의 구현 현황을 실물 정지·점유 해제 수용으로 해석하지 않는다.

## 설계 당시 제약 (2026-10-27)

- 당시 `Command.order`는 OneToOne이라 주문당 명령이 영구히 하나였다. 재계획으로 새 계획을 착수하려면 이전 명령을 지워야 했지만 Command 이력은 삭제 불가였다(설계24 지적).
- 당시 취소는 `CANCEL_PENDING`에 머물렀다. 종착 상태가 없어 "취소 완료"를 기록할 자리가 없었다.
- 불명(`UNKNOWN`)·원래 key 대조·backoff는 P1 실행기가 이미 처리하고 이 설계가 바꾸지 않는다.

## 제안 구조

### 1. 주문당 명령 이력

- `Command.order`를 OneToOneField에서 ForeignKey(`related_name='commands'`)로 바꾼다.
- 같은 주문에 **활성** 명령은 하나로 제한한다. 구현의 활성 집합은 `harbor_web.command_models.ACTIVE_COMMAND_STATES`를 따르며 준비 상태도 포함한다. DB 제약 `one_active_command_per_order`와 업무 서비스가 같은 집합을 사용한다.
- 종착 상태(`COMPLETED`, `BLOCKED`, `CANCELLED`)에 이른 명령은 이력으로 남는다. 역방향 접근 `order.command`를 쓰는 코드는 `order.commands.filter(상태 활성)` 조회로 바꾼다(영향 목록은 구현 커밋에서 점검).

### 2. 취소 종착 상태

- `CANCELLED`를 더한다. **전환은 운영자 확정으로만** 일어난다: `CANCEL_PENDING` 상태에서 운영자가 Fleet 콘솔에서 실제 정지·자원 해제를 사람이 확인한 뒤 Harbor의 종결 조치(`close_cancelled`)로 종착을 기록한다. 실행기·대조가 자동으로 종착을 만들지 않는다.
- 근거(2026-10-27 플랫폼 API reference v1.312 확인): ARM 작업 상태는 `PROPOSED`/`READY`/`RUNNING`/`ACTION_SUCCEEDED`/`GOAL_CONFIRMED`/`HOLD`뿐이고 취소 종착 상태가 없으며, "cancel과 HOLD는 점유를 유지하고 자동 재개되지 않는다"고 명시돼 있다. 취소 ACK는 정지·해제 증명이 아니므로(같은 문서), 앱이 조회 결과만으로 종찰을 만들 근거가 계약에 없다.
- 종결 조치는 취소 요청·사유·확정자를 감사와 함께 남기고, 남은 미제출 단계를 영구히 닫는다. 이미 종착인 명령은 어떤 조치로도 바뀌지 않는다.
- 플랫폼이 나중에 취소 종착·해제 근거를 공개 계약에 추가하면, 그 계약을 소비하는 별도 변경에서 자동 대조 전환을 다시 검토한다(이 설계가 미리 열지 않는다).

### 2026-10-29 구현의 조작·조회 경계

공유 업무 서비스는 활성 집합 밖 명령의 제출·대조·재개·취소 조작을 거절한다.
복원 차단 사유가 `RESTORE_RECONCILIATION_REQUIRED`인 `BLOCKED` 명령의 대조만
원래 요청의 GET 조회를 허용한다. 이 경로는 검증한 CommandFact만 추가하며 명령의
상태·version과 업무 장부는 바꾸지 않는다. timeout에도 실행을 열지 않는다.

최종 업무 근거 대기와 완료 단계는 현재 mission이 없으므로 이전 CommandFact를
현재 실행에 다시 결속하지 않는다. 저장된 이력은 유지하고 화면은 장부 상태를 조회한다.
코드·회귀 결과는 [검증 기록](../validation/2026-10-29-command-lifecycle-boundaries.md)을 따른다.

### 3. 재계획 흐름

- `prepare_command`의 "기존 주문 실행은 재조정 필요" 거절을 **종착 검사**로 바꾼다: 이전 명령이 종착일 때만 새 계획의 Command를 만든다. 활성 명령이 있으면 거절은 유지된다(이미 제출한 작업은 원래 계획·식별자에 고정).
- 미제출 단계가 남은 활성 명령을 대체하려면 먼저 취소를 종착까지 진행한다. "부분 완료 후 재계획"은 이 순서를 그대로 따른다.
- 새 Command는 이전 명령을 `supersedes`(null 허용 FK)로 참조해 실행 이력을 잇는다. 계획 revision·관측 원본은 Command 생성 시 각각 고정한다(기존 불변 식별 규칙 유지).
- 사용자 확정 범위 밖의 목표 변경은 기존대로 새 확정을 받는다(ADR0012). 자동 재계획 제안(ADR0010)과 실행 착수를 구분한다.

## 수용 시험 (구현의 완료 기준)

1. 1단계만 확인(부분 완료)된 활성 명령에서 다른 계획 착수가 거절된다.
2. 취소 → 운영자가 Fleet 콘솔에서 정지·해제를 확인 → `close_cancelled` 확정 → `CANCELLED` 종착 → 같은 주문의 새 계획 Command 착수(`supersedes` 참조), 이전 식별자·이력 불변.
3. `CANCEL_PENDING`에서 운영자 확정 전에는 종착 전환도 재전송도 없고, `close_cancelled`는 `CANCEL_PENDING` 외의 상태를 거절한다.
4. 종착 명령이 여럿인 주문의 화면·조회가 이력을 보존한다.
5. P1·P2 회귀(3단계 진행·원래 key 복구·근거 결속)가 그대로 통과한다.

## 하지 않는 것

- 병렬 업무·공동 계획 활성화(설계24: 별도 검증 전 비활성).
- 취소 ACK만으로 자원 해제·재무장을 여는 어떤 경로.
- Fleet 계약에 없는 취소 상태 값을 앱이 추측해 종착을 만드는 구현.
