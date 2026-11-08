# ADR 0029: Harbor는 반입 화물을 ROSY 항만 사건 4개로 받는다

날짜: 2026-11-08

상태: 수락(2026-11-08, 사용자 결정 D1~D7 "S 결과와 컨베이어 끝 도착을 어느 컨테이너인지와 함께 Harbor에 알린다", "대체 팔 전환은 Fleet이 맡는다", "Harbor는 항만 도메인만 가진다", "S load는 Fleet 작업으로 한다", "11/14 동결 전에 자동 사건까지 한다").
- 번호: 0027은 ROSY 계정 로그인(PR #46), 0028은 관제 단위·장치 owner·호스트가 쓴다. 병합 시점 `main`의 번호를 다시 확인한다.
- 대체: ADR 0005 결정 5의 "공급암 S는 플랫폼 밖 외부 단계"와 결정 6의 "S 퇴피를 Harbor가 확인해 이송 차량 출발을 허용한다". 나머지 0005 결정은 유지한다.
- 짝 결정: ROSY D-996(항만 사건과 반입 신원). 계약 필드는 그 ADR과 아래 3절이 같다.

## 배경

ADR 0005는 S를 플랫폼 밖 단계로 두고, Harbor가 S 퇴피를 사람 확인으로 받아 이송 차량 출발을 허용했다. 그래서 Harbor 설정과 코드에 컨베이어 용량, S 퇴피 판정, 팔 instance 대응표, 관절값이 들어왔다. 이제 S load는 ROSY Fleet의 `ARM_TRANSFER` 작업이 되고(D5), 셀 사실을 항만 사건으로 바꾸는 일은 ROSY 셀 Fleet이 맡는다. Harbor는 어느 컨테이너가 어디까지 왔는지만 알면 된다.

## 결정

1. **S는 ROSY 안의 Fleet 작업이다.** 이송이 도착하면 ROSY가 S 작업을 스스로 제안하고, 기명 승인(D-977)을 거친다. Harbor는 S·컨베이어·A/B 기구·마커·카탈로그 종류를 모른다(D4). Harbor 설정의 컨베이어 검증과 S 퇴피 판정을 없앤다.
2. **Harbor가 받는 사건은 4개다.**

   | 사건 | Harbor에서의 뜻 |
   |---|---|
   | `container.received_at_intake` | 그 화물이 장치 작업대 입구에서 받을 준비가 됐다 |
   | `container.stored` | 장치 칸에 놓였고 관련 팔이 공간을 비웠다. 반입 업무 완료 |
   | `container.handling_failed` | 사람 처리가 필요하다 |
   | `vehicle.release_permitted` | 그 차량이 출발해도 된다. Harbor의 S 퇴피 판정을 대신한다 |

3. **조회 계약.** Harbor service principal이 R에 `GET /api/fleet/port-events?after=<cursor>&limit=<1..200>`을 보낸다. 응답은 `{"events": [...], "cursor": "<opaque>", "complete": bool}`이다.
   - `cursor`는 R이 만든 불투명 문자열이다. Harbor는 연결마다 저장했다가 그대로 돌려보내고 해석하지 않는다. 그래서 workcell id를 몰라도 된다.
   - 사건은 닫힌 모델이다. 필드는 `event_id`(64 hex), `kind`, `observation_ref`, `object_ref`, `intake_point`, `slot_ref`, `vehicle_id`, `reason`, `identity`, `observed_at`이고, kind마다 필수·금지 필드가 정해져 있다. 모르는 필드나 kind는 거절하고 커서를 옮기지 않는다.
4. **소비 규칙.**
   - `event_id`는 한 번만 소비한다. 다시 오면 무시한다.
   - 화물 상태는 `TRIP_ARRIVED → RECEIVED_AT_INTAKE → STORING → COMPLETED`로 단조 전이만 한다. 늦게 온 사건이 상태를 되돌리지 않는다. 새로 생긴 업무 상태는 `RECEIVED_AT_INTAKE` 하나이고, 보관 단계는 기존 업무 장부(`BusinessLedger`)와 Fleet ARM 승인 경로가 맡는다.
   - HOLD 사유는 `MISMATCH`(선언과 판독 불일치), `UNKNOWN`(이송에 선언되지 않은 화물이거나 화물 코드가 없음), `LOST`(시간 초과, 또는 ROSY가 `LOST`로 보고), `DAMAGED`(ROSY가 손상으로 보고)다. `container.handling_failed`는 늘 사람 처리가 필요하다는 뜻이므로 `UNREADABLE`을 뺀 모든 사유는 HOLD다.
   - `complete:false`는 닿지 않은 셀이 있다는 뜻이다. 진행 중인 화물을 HOLD하지 않고 다음 조회까지 기다린다. 정해진 시간 동안 사건이 없으면 `LOST`로 HOLD한다. 시간 초과는 그 연결에서 R이 한 번이라도 응답해 cursor가 저장된 뒤에만 센다. 엔드포인트가 아직 없거나(404) 닿지 않으면 화물을 `LOST`로 만들지 않는다.
5. **신원 규칙.**
   - 개체 신원은 판독한 값(`identity: observed`) 또는 기명 사람 확인(`identity: attested`, ADR 0023 방식)으로만 맞춘다. 순서(FIFO)나 추정으로 완료까지 가지 않는다.
   - `object_ref`는 Harbor가 이송에 선언한 화물 코드(`Cargo.code`)다. ROSY는 해석하지 않고 그대로 나른다. 마커 번호와 `object_ref`의 짝은 ROSY marker-profile이 갖고, Harbor는 `marker_id`를 저장하지 않는다.
   - ROSY가 판독하지 못하면(`UNREADABLE`) Harbor는 그 화물을 기명 사람 확인으로만 `RECEIVED_AT_INTAKE`로 올린다.
6. **팔 선택과 퇴피(D3).** 대체 팔 전환은 Fleet이 맡는다. Harbor는 팔 `instance_id`를 고르는 근거로 쓰지 않는다. ROSY `ArmTransferJobCreate`가 아직 `instance_id`를 요구하므로 설치된 주 팔 값을 보내기만 하고, 수령증 대조에는 쓰지 않는다. 퇴피 근거는 Fleet 근거에 나온, 실제로 움직인 팔의 것만 요구한다. 관절값은 저장하지 않는다.
7. **동작 권한은 바뀌지 않는다.** 사건으로 작업 제안이 열리더라도 ROSY 기명 승인은 그대로 남는다. Harbor는 사건으로 장치 동작을 직접 일으키지 않는다.

## 결과

- 이송 제안(`POST /api/fleet/trip-proposals`)에 선택 필드 `object_ref`를 싣는다. ROSY는 `object_ref`가 있을 때만 그것을 `request_digest` 본문에 넣는다(키 정렬·공백 없는 JSON, ROSY 161957814a). 같은 key에 다른 `object_ref`면 ROSY가 409로 거절한다. Harbor의 digest 계산은 `harbor/fleet_contract.py:trip_digest` 한 곳이다. ROSY 쪽 필드와 R 조회 경로가 착지하기 전에는 Harbor 소비기가 빈 결과 또는 연결 실패로 기다리기만 한다.
- `harbor/port_events.py`가 사건 모델·화물별 상태·커서를 갖고, `harbor/departure.py`는 `vehicle.release_permitted`로만 출발을 허용한다.
- 바뀌지 않는 것: 반출·재취급 업무 완료 조건(ADR 0005 결정 6의 A·B 부분), 사람 확인 출처 분리(ADR 0023), Fleet 승인 경계(ADR 0012·0015).
