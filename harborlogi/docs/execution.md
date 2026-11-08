# 공정 제안과 업무 장부 사용법

## 원본 물리 근거 provider (D-915)

`HARBOR_EXECUTOR_EVIDENCE=physical`은 기존 실행기에 원본 Fleet closure 증거를 읽는 provider를 선택한다. 기본값은 꺼짐이다. 서버 환경의 `HARBOR_PLACEMENT_SOURCES`와 `HARBOR_RETREAT_SOURCES`에는 서로 다른 설치 출처를 각각 하나씩 지정하고, `HARBOR_PHYSICAL_EVIDENCE`에는 다음 형태의 JSON을 둔다. 실제 설치의 policy digest와 A/B owner, zone, 측정 관절 순서로 교체해야 한다.

```json
{"policy_digest":"<installed-policy-sha256>","arms":[{"arm":"A","workcell_id":"cell-a","instance_id":"arm-a","zone_id":"shared-zone","joint_names":["joint1","joint2"]}]}
```

관련 팔이 둘이면 B도 명시한다. 현재 ARM binding의 owner와 팔 대응이 다르거나 관련 팔 측정이 빠지면 거절한다. 같은 원본 증거를 다시 읽어도 시각·측정 식별자를 새로 만들지 않는다. 원본 capture가 5초보다 오래되거나, receipt와 원문 SHA가 다르거나, 관절 순서·실행 시도가 다르면 단계 확인에 쓰지 않는다. 과거 receipt와 현재 pose 조회를 조합하지 않는다. Harbor에는 observer 토큰을 공급하지 않고 기존 전용 Fleet service 신원을 사용한다.

이 provider는 `WAITING`인 현재 ARM 단계에서만 근거를 공급한다. 마지막 이동 확인 뒤 상태는 `AWAITING_FINAL_EVIDENCE`이며 `final` 요청에는 근거를 반환하지 않는다. `finish()`에는 별도 최종 업무 근거가 필요하다. 운영 producer·실제 Gazebo 시계 변환·실물 수용은 아직 검증하지 않았다.

개발 HTTP 경로는 기존 `tools.integration.real_fleet_http`에 `--evidence-only --app-python <isolated-harbor-python>`을 전달해 실행한다. 고정 플랫폼 SHA와 깨끗한 양 끝 checkout이 필요하다. 별도 소비자 프로세스가 실제 Fleet HTTP와 UDS를 사용하지만, 이 모드의 측정값은 합성 fixture다. Gazebo 물리 관측이나 장치 수용 결과로 해석하지 않는다.

## 기존 공정과 장부

필요한 플랫폼 계약은 D-836, API reference v1.292다. 후보 이송은 simulation profile의 기존 named operator 접수다. 기존 단일 `robot_id` 호출은 플랫폼에서 계속 지원한다.

1. `plan_outbound`로 관측 revision에 결속한 순수 반출 제안을 만든다.
2. 설치자가 각 `(cargo_id, source, destination)` 이동에 대해 검증한 `ProcessBinding`을 제공한다. recipe/cell ID와 digest, workcell/instance ID, 설치된 process artifact digest가 필요하다. 누락·중복 연결은 `bind_plan`이 거절한다. 이 binding은 물리 도달/충돌 검증을 대신하지 않는다.
3. `BusinessLedger(path, placement_sources=(...,), retreat_sources=(...,))`와 `prepare(key, revision, bound_moves, outbound_id=..., outbound_stand=..., move_arms=...)`로 업무 의도를 기록한다. `move_arms`는 이동마다 제안의 주 팔과 대체 팔(`(primary_arm, *alternate_arms)`)이고, 그 이동에서 쓰일 수 있는 팔 모두가 퇴피 근거의 대상이 된다. 두 출처 목록은 겹치면 안 된다. 같은 key·내용은 중복 생성하지 않고 다른 내용은 거절한다. 장부 경로는 운영 데이터 위치로 정한다.
4. `RosyClient(base_url, token)`을 주입해 `ledger.propose(key, client)`를 호출한다. 문서/공정 digest와 단일 transfer preview를 확인한 뒤 기존 `/cell-app/proposals`에 제안한다. 자동 승인하지 않는다. compiler artifact가 중간에 바뀌거나 응답이 불명확하면 HOLD다. POST를 자동 재전송하지 않는다.
5. 별도 운영자가 기존 Fleet 절차로 검토·승인한다. `ledger.refresh(key, client, now=...)`는 현재 cell job의 status/attempt를 읽는다. `ACTION_SUCCEEDED`만으로는 진행할 수 없다.
6. `confirm(key, MoveEvidence(placement, retreat), now=...)`에 서로 독립된 두 근거를 전달한다(ADR 0005 결정 6). 내려놓기 근거 `PlacementEvidence`는 카메라 관측(화물 ID·위치·관측 시각·출처)이다. 퇴피 근거 `RetreatEvidence`는 팔별 `ArmRetreat`(Fleet zone claim 해제 사실, owner 측정 관절값·측정 시각·revision, 그 자세가 구역 밖이라는 플랫폼 판정 `outside_zone`)이다. Harbor는 FK를 계산하지 않는다. 두 근거 각각의 출처(설정된 해당 종류 목록)·mission/attempt·계획 revision·시각이 맞고, 내려놓기는 화물·목적지가, 퇴피는 그 이동의 관련 팔 모두의 claim 해제·구역 밖·측정 시각이 맞으며 Fleet goal도 확인돼야 다음 제안을 만들 수 있다. 근거가 하나라도 없으면 진행하지 않는다. 두 근거를 만드는 producer와 플랫폼 조회 계약은 아직 없으므로(D-856 P1) 시험은 합성 근거만 쓴다. 관측 ID는 한 번만 소비한다. refresh와 관측의 기본 유효 기간은 각각 5초다.
7. 마지막 재취급 화물 복귀 뒤에도 `AWAITING_FINAL_EVIDENCE`다. 반출 대상 화물 이동 단계의 mission/attempt와 결속된 반출대 내려놓기 근거와, 이번 요청에 쓰인 팔 전부(A·B)의 퇴피 근거로 `finish`해야 COMPLETED가 된다.

두 근거의 `revision`은 승인할 업무 계획의 기준 revision이다(`ArmRetreat.pose_revision`은 owner 측정값의 revision으로 따로 보존한다). 실제 관측 producer의 원본 revision·인증을 대체하는 ROSY wire schema가 아니다. 현재 인증된 생산자에서 이 값으로 변환하는 운영 어댑터는 없으며, 실물 적용 전 그 출처·원본 시각·identity 대응을 검증해야 한다.

확인한 기존 producer의 한계와 필수 필드별 책임, 운영 어댑터 구현·수용의 선행 조건은 [물리 관측 수용 조건](observation-acceptance.md)에 있다. 관절 상태·그리퍼 OPEN·Fleet goal 확인·대기 자세 도달만으로 화물 도착·팔 퇴피·내려놓기를 만들어내지 않는다.

재시작 후 기존 WAITING 단계는 새 `refresh` 전에는 확인할 수 없다. 미응답 SUBMITTING은 HOLD로 복구하며 늦은 응답이 와도 HOLD를 되돌리지 않는다. 원장의 같은 request key로 재대조한 뒤 운영자가 판단해야 한다. 자동 복구·물리 점유 해제·진행 중 조작 takeover는 없다.

장부의 각 변경은 증가하는 `row_version`에 결속된다. compile·조회 HTTP 응답을 받은 뒤에도 처음 읽은 버전·단계·상태가 같아야 적용한다. 다른 호출자가 새 단계·시도·완료로 진행했다면 늦은 응답을 거절하고 최신 상태를 다시 읽어야 한다. 기존 장부에는 이 버전을 기본값 0으로 추가하며 업무 상태를 보존한다.

후보 이송은 수동 제외를 적용한 후보를 `client.trip(candidates, steps, request_key=...)`로 전달한다. Fleet만 현재 lease/claim을 확보한다. 동일 key의 재전송은 기존 작업을 읽고, 새 의도에는 새 key가 필요하다. 이송 요청과 화물 인계 procedure를 자동 조합하는 commissioning은 아직 없다.

실제 HTTP는 HTTPS 또는 loopback HTTP를 사용한다. 토큰·실제 장치 주소는 외부 설정으로 공급한다. 인증 헤더를 다른 URL로 전달하지 않도록 redirect와 POST retry를 사용하지 않는다.

## 장부 보관

`BusinessLedger`에는 설치자가 지정한 저장소 밖 절대 경로를 전달한다. 장부는 업무 의도·관측 판정을 담는 앱 상태이며 Fleet의 실행 원장과 별도로 보관한다. 다음 예시는 플랫폼 저장소를 복제하지 않은 환경에서도 사용할 수 있다. 이 작업 공간의 실제 배치에는 `Rosy/rosy-data/app-state/port/`의 절대 경로를 지정한다.

```python
from pathlib import Path
from harbor.workflow import BusinessLedger

ledger_path = Path.home() / "rosy-data" / "app-state" / "harbor" / "business.sqlite3"
ledger_path.parent.mkdir(parents=True, exist_ok=True)
# 실제 설치에서 검증한 관측 출처 ID로 교체한다.
ledger = BusinessLedger(ledger_path, placement_sources=("synthetic-camera",),
                        retreat_sources=("synthetic-retreat",))
```

경로와 출처는 설치 예시이며 실제 서버·로봇에 연결하거나 실행을 승인하지 않는다. 플랫폼의 `ROSY_DATA_ROOT`는 이 독립 앱의 장부 경로를 자동 선택하지 않는다. 상대 경로를 전달하면 현재 작업 디렉터리가 기준이므로 실행 위치가 달라질 때 다른 장부를 여는 실수를 피해야 한다.

가동 중인 장부는 DB 파일만 복사하지 말고 SQLite online backup API 등 일관된 snapshot 방식으로 보관한다. 읽기 전용 사본의 무결성을 확인하고, 앱을 재시작하기 전 정본 경로와 미확정 요청/HOLD를 대조한다. 장부 복원이나 파일 이동을 로봇 점유 해제·자동 재전송 근거로 삼지 않는다.

## 업무 실행기와 근거 출처 설정

웹 앱의 상시 업무 실행기(설계24·27 P1)와 근거 어댑터(P2)는 환경 변수로 켠다. 모두 명시적 옵트인이며, 설정하지 않으면 실행기가 돌지 않고 근거 출처가 placeholder로 남아 아무 제출도 승인하지 못한다.

| 변수 | 역할 | 기본 |
|---|---|---|
| `HARBOR_EXECUTOR_ENABLED=1` | 서비스 프로세스에서 상시 실행기 시작(사람 확정→즉시 제출 뒤 남은 단계를 진행) | 끔 |
| `HARBOR_EXECUTOR_INTERVAL` | 실행기 조회 간격(초). 조회 간격·한 번에 처리할 업무 수·backoff를 이 값으로 제한한다 | 5 |
| `HARBOR_EXECUTOR_BATCH` | 한 tick이 다루는 최대 활성 업무 수 | 4 |
| `HARBOR_PLACEMENT_SOURCES` / `HARBOR_RETREAT_SOURCES` | 설치자가 등록한 배치·퇴피 근거 출처 ID(쉼표 구분, 서로 겹치지 않음). 장부의 `placement_sources`·`retreat_sources`가 이 값을 쓴다 | 미설정(placeholder) |
| `HARBOR_EXECUTOR_EVIDENCE=intake` | 실행기가 제출된 근거(intake)를 소비해 confirm·finish를 이어간다 | 끔(근거 없이 단계가 열리지 않음) |

근거 제출은 `harbor_web.evidence.submit`(현장 확인 역할)로 들어오며, mission·attempt·계획 revision에 결속되고 신선(기본 5초)하며 아직 소모되지 않은 것만 채택된다. 운영 producer의 전송·자격 계약은 [물리 관측 수용 조건](observation-acceptance.md) 산출물이 열기 전까지 설치 통합 담당 영역이다.

## 실제 Fleet 계약 시험

### 중립 이송 의도와 수행 조회 (D-856 첫 흐름)

API v1.298 지원 서버에서는 `harbor.transport.TripIntent(request_key, candidates, destination, via=())`로 단일 이송 의도를 만들고 `TransportLedger(path)`에 먼저 보존한다. 이 표는 기존 업무 SQLite와 함께 둘 수 있는 앱 제출 의도와 Fleet 참조이며 실행 원장이나 업무 완료의 정본이 아니다. 한 장부는 같은 Fleet origin/principal 설정에만 사용한다.

`prepare(intent)` 뒤 `submit(key, client, operator_confirmed=True)`는 기존 named-operator TRIP 접수를 정확히 한 번 요청하고 GET으로 request key·digest·robot·step을 대조한다. TRIP POST 자체는 기존 계약상 운영자의 승인·시작이다. 이 함수는 Cell 제안의 admit를 수행하거나 업무 계획을 자동 승인하지 않는다. 호출자는 화물 보유·공급암 S 출발 gate 등 해당 업무 조건을 먼저 확인해야 한다. 이번 simulation 예제는 화물 없는 운송이며 S gate를 대체하지 않는다.

응답 유실이나 제출 중 재시작은 `UNCERTAIN`이고 새 POST는 거절한다. `reconcile(key, client)`는 원본 key의 GET만 수행한다. GET 404·연결 오류는 자동 재접수·새 key·장치 재배정의 근거가 아니다. `ACKNOWLEDGED`는 일치하는 Fleet 참조를 조회했다는 뜻이며 HOLD 작업도 포함할 수 있다. 로봇의 `GOAL_CONFIRMED`는 화물 내려놓기·팔 퇴피·반출 완료를 뜻하지 않는다. 현재 반출 업무 장부와 관측 판정은 그대로 유지한다.

### 이송 제안 경로 (D-908, API v1.329)

Harbor 서비스 신원은 `POST /api/fleet/trip-jobs`(named operator 전용)를 쓸 수 없으므로 `TransportLedger.propose(key, client)`가 `RosyClient.trip_propose`로 `POST /api/fleet/trip-proposals`를 정확히 한 번 보낸다. Harbor는 승인하지 않는다(`admit` 호출 없음). 다른 named operator가 승인해야 같은 request key의 trip job이 생긴다. 시뮬레이션 프로필 전용이며, 미설치 서버는 503 `TRIP_PROPOSAL_UNAVAILABLE`이다.

`harbor.execution.submit_trip_proposal_execution`/`read_trip_proposal_execution`이 응답을 저장된 의도(key·digest·후보·step)와 대조하고 제안자 주체를 확인한다. 승인자가 제안자와 같으면 거절한다. 대기 중이면 mission_id 없는 `awaiting_approval` 스냅샷을, 승인되면 `trip-jobs?request_key=`를 읽어 job의 mission_id가 제안과 같은지 확인한 스냅샷을 돌려준다. 장부는 Fleet 상태 문자열을 해석하지 않고 이 정규화된 phase만 본다.

장부 상태: PLANNED → SUBMITTING → AWAITING_APPROVAL(승인 대기, mission_id 없음) → ACKNOWLEDGED(job 연결). 응답 유실·재시작은 UNCERTAIN이며 `reconcile_proposal(key, client)`가 같은 key로만 조회하고 재전송하지 않는다. 기존 직접 `submit()`은 operator 신원이 `trip()`을 쓸 때만 남겨 두었다(최소 변경, 기존 시험·통합 하네스 유지). 서비스 신원의 앱 경로는 `propose()`다.

**미검증**: 실제 Fleet(v1.329)에 대한 제안→승인→job 시험은 돌리지 않았다. 단위 시험은 가짜 Fleet이며, `tests/integration/transport_http.py`는 아직 제안 경로를 다루지 않는다.

**웹 반입 이송(ADR 0029).** `HARBOR_TRIP_DESTINATION`(반입 장소 ID)과 `HARBOR_LEDGER_PATH`가 있으면 `prepare_departure`가 출발 의도와 함께 `harbor-trip:<출발 key>` 이송 의도를 PLANNED로 저장하고, 화물 코드를 `object_ref`로 선언해 반입 명단(`IntakeLedger`)에 올린다. 웹 요청은 Fleet에 아무것도 보내지 않는다. 업무 실행기가 tick마다 `harbor_web/intake_loop.py`로 PLANNED 이송을 한 번 제안하고, 원래 key로 대조해 Fleet이 도착을 확인하면 TRIP_ARRIVED로 올린 뒤, R의 항만 사건 한 쪽을 읽는다. 설정이 없으면 예전처럼 출발 의도만 남는다.

`RosyClient.trip_job(id)`와 `trip_request(key)`는 공정 문서 없는 수행 snapshot을 읽는다. 이송 후보·장소와, ADR 0029부터는 해석되지 않는 화물 코드 `object_ref` 하나만 wire로 보낸다. `request_digest`는 후보·step에 더해, `object_ref`가 있을 때만 그것까지 덮는다(ROSY 161957814a와 같은 바이트). Harbor 쪽 계산은 `fleet_contract.trip_digest` 한 곳이다. recipe·pallet/layer는 새 요청에 넣지 않는다. typed 동작의 추가·도달 사전 검증·중립 팔 동작은 후속 계약이다.

공정 compiler 없는 실제 Fleet simulation HTTP 검증은 아래와 같이 명시 실행한다. 일반 단독 시험의 integration skip과 별개다.

```text
python -m tools.integration.real_fleet_http --platform-root <플랫폼-root> --platform-sha <전체-40자리-SHA> --transport-only
```

### 한 Fleet 첫 흐름 전체 (D-927)

`--full-flow`는 시뮬 Fleet 하나에서 TRIP과 ARM을 함께 돌려 ①~⑤를 잇고, 처음 실패한 단계에서 멈춰 `--report` JSON에 양 끝 SHA와 단계별 결과를 남긴다. 정상 단계 관측은 플랫폼 `tools/sim/transfer_observer.py`가 별도 프로세스로 맡고, 하네스는 시험 운영자 승인과 합성 owner의 scene만 둔다.

```text
python -m tools.integration.real_fleet_http --platform-root <플랫폼-root> --platform-sha <전체-40자리-SHA> --full-flow --report <파일>
tools/integration/nightly_full_flow.sh <작업-폴더> [harbor-ref] [platform-ref]
```

`nightly_full_flow.sh`는 두 저장소의 지정 ref(기본 main)를 얕게 받아 한 번 실행하고 `<작업-폴더>/reports/`에 보고서와 로그를 2주 보관한다. 종료 코드가 0이 아니면 보고서의 `first_failure`가 멈춘 단계다.

### 통합관제 R을 거친 ARM (D-965, ADR 0018)

`--arm-via-r`는 같은 ARM 시험(`tests/integration/arm_http.py`)을 R 하나에 연결해 돌린다. R은 ARM 설치 없는 Fleet이고, 셀 Fleet(`ArmFleet`)이 제안을 R의 service principal(`control-unit:R`)로 받아 셀 운영자만 승인한다. Harbor는 R의 origin과 토큰만 받는다. 플랫폼 API v1.355 이상이 필요하다.

```text
python -m tools.integration.real_fleet_http --platform-root <플랫폼-root> --platform-sha <전체-40자리-SHA> --arm-via-r
```

### 기존 Cell 공정 계약 시험

`python -m unittest discover -s tests -v`의 통합 skip은 실제 서버 미검증을 뜻한다. 시험 PC에 깨끗한 고정 플랫폼 소스를 준비하고, 해당 플랫폼의 기존 시험 의존성을 설치한 별도 환경에서 다음 개발 도구를 실행한다.

```text
python -m tools.integration.real_fleet_http --platform-root <플랫폼-root> --platform-sha <전체-40자리-SHA>
```

도구는 Harbor의 시뮬레이션 하네스(`tools/integration/fleet_harness.py`, [ADR-0003](adr/0003-own-fleet-contract-harness.md))로 고정 플랫폼 Fleet을 loopback에 띄우고 임시 장부·합성 operator/viewer 자격으로 `tests/integration`을 별도 프로세스에서 실행한 뒤 서버를 종료한다. 서버 환경만 플랫폼 시험 코드를 import한다. Harbor 런타임 wheel에는 `tools`가 포함되지 않는다. 일반 운영 Fleet·실물 장치에 대한 도구가 아니다.

독립 실행에서는 `HARBOR_INTEGRATION=1`과 도구가 만든 임시 `HARBOR_INTEGRATION_CONFIG`를 함께 공급한다. 명시 모드에서 설정 누락·연결 오류는 실패이며 skip을 성공 증거로 삼지 않는다. 플랫폼·Harbor 전체 SHA 두 개와 exit code를 함께 기록한다. fixture의 합성 관측은 업무 완료의 운영 증거를 제공하지 않는다.

업무 대조 시험에서는 별도 시험 actor가 Fleet의 기명 승인자로 승인한다. Harbor의 런타임 클라이언트에 승인 기능을 추가하지 않는다. 실제 `GOAL_CONFIRMED` 뒤에도 Harbor 장부가 WAITING에 남는지, 잘못된 관측을 거절하는지, 재시작 후 새 조회가 필요한지, 인계 뒤에도 별도 내려놓기·퇴피 근거 전에는 AWAITING_FINAL_EVIDENCE인지 확인한다. 관측 값은 시험에 명시한 합성 데이터이며 인증된 현장 관측을 읽는 어댑터가 아니다. [실행 결과와 제한](validation/2026-10-22-business-reconciliation.md)을 따른다.
