# ADR 0022: 운영 실행 관문은 조건별 점검표다

날짜: 2026-11-04

상태: 제안(PR 검토 대기). 사용자 결정(2026-11-04)을 반영했다.
- 잇는 결정: ADR0005(책임 분담)·ADR0013(업무 실행 독립)·ADR0015(서비스 신원)·ADR0018(R 하나)·ADR0020·ADR0021.
- 범위: 관문 조건·신선도·설정까지다. 사람 확인 근거를 입력하는 화면과 장부 연결은 이 결정의 범위가 아니며 별도 작업이다.

## 배경

`capability(plan)`의 운영 분기는 `AUTHENTICATED_OBSERVATION_AND_EVIDENCE_PRODUCER_REQUIRED` 하나로 막혀 있었다. 무엇이 갖춰졌고 무엇이 남았는지 보이지 않았고, 열리는 날 코드를 다시 고쳐야 했다. 2026-11-04 운영 PC 사실은 다음과 같다. R 세션은 정상이다. ARM 조회와 이송 제안 조회는 503이다. R 중계에는 아직 `physical-evidence`·`rechecks`가 없어 기본 404가 온다(착지 중). 설치 binding은 `[]`다. 플랫폼에는 운영 프로필을 알리는 공개 신호가 없다.

## 사용자 결정 (2026-11-04)

- "실제로 이제 동작해야 하는 시간": ROSY의 실물 ARM 경로를 열기 시작한다(별도 플랫폼 작업).
- 업무 완료 근거(내려놓기·팔 퇴피)는 자동 생산자가 생길 때까지 **기명 현장 운영자 확인**으로 받는다. Harbor는 이를 '사람 확인' 출처로 구분해 업무 완료에 쓴다.

## 결정

1. 시뮬레이션 경로(개발·시험 모드 + `HARBOR_WEB_ALLOW_SIMULATION=1` + 루프백 + `SYNTHETIC` 관측)는 그대로 둔다. 이 네 조건이 모두 맞지 않으면 운영 분기이며, `HARBOR_WEB_ALLOW_SIMULATION`만으로는 운영 모드가 열리지 않는다.
2. 운영 실행은 아래 조건이 **모두 참**일 때만 연다. 조건을 빼거나 환경 변수 하나로 우회하지 않는다. `capability()`는 첫 미충족 조건의 `READINESS_*` 코드를 이유로 돌려준다.
3. 신호는 관리자 연결 점검(`check_connection`) 때 읽기 전용 GET으로 모아 시각과 함께 감사 기록에 저장한다. `capability()`는 네트워크를 부르지 않고 저장된 점검을 읽는다. 점검은 현재 설정·토큰 지문에 묶이고, **10분**이 지나면 `READINESS_CHECK_STALE`("다시 점검")로 막는다. 점검이 없으면 `READINESS_CHECK_REQUIRED`다.

| 조건 | 신호 출처 | 미충족 코드 | 소유 |
|---|---|---|---|
| (a) 설치 binding이 비어 있지 않고 계획에 대응 | 로컬, 매 요청 | `READINESS_BINDING_REQUIRED` | 설치 담당자 |
| (b) R 세션이 설정 principal·service 역할과 일치 | 점검: `GET /api/fleet/session` | `READINESS_SESSION_UNMET` | Harbor 설치 담당자 |
| (c) R ARM 경로 설치 | 점검: 없는 키 GET이 404 `ARM_TRANSFER_JOB_NOT_FOUND` | `READINESS_ARM_UNMET` | ROSY |
| (d) R 근거·재확인 경로 응답 | 점검: 없는 mission의 `physical-evidence`와 `rechecks/{key}` GET이 둘 다 404 `ARM_TRANSFER_JOB_NOT_FOUND` | `READINESS_EVIDENCE_UNMET` | ROSY |
| (e) 이송 제안 경로 설치 | 점검: 없는 키 GET이 404 `TRIP_PROPOSAL_NOT_FOUND` | 표시 전용 | ROSY |
| (f1) 플랫폼이 운영 프로필에서 실물 ARM 실행을 공개 | 점검: 제안 `GET /api/fleet/execution-profile` (아직 없음, 항상 거짓) | `READINESS_ARM_EXECUTION_UNANNOUNCED` | ROSY |
| (f2) 근거 생산자 | (i) 로컬: 관리자가 켠 사람 확인 설정 + 현장 확인 권한 활성 사용자 1명 이상, 또는 (ii) 점검: 플랫폼이 인증된 자동 생산자를 공개(아직 없음) | `READINESS_EVIDENCE_PRODUCER_REQUIRED` | Harbor 관리자 |
| (g) 계획 관측이 합성이 아님. 사람 선언(`DECLARED`)은 사람 확인 생산자가 켜졌을 때만 | 로컬, 계획의 provenance와 (f2-i) 설정 | `READINESS_OBSERVATION_UNAUTHENTICATED` | Harbor |

판정 규칙은 다음과 같다.
- 코드 있는 404만 "설치됨"이다. 코드 없는 404(`{"detail":"Not Found"}`, 경로 없음)와 503은 미충족이다. 401·403·5xx·네트워크 실패·예상 밖 200은 미확인이며 둘 다 막는다.
- 탐침 키는 무작위로 만든 존재할 수 없는 키이며 GET만 쓴다. 작업·제안·재확인을 만들지 않는다.
- 조건 순서는 (a), 점검 신선도, (b), (c), (d), (f1), (f2), (g)다.

**(e) 이송 제안은 관문에서 뺀다.** 지금 웹 실행기는 ARM 한 단계만 제출하고 이송 단계가 없다. 요구하지 않는 경로를 관문에 넣으면 이유 없이 막힌다. 그래서 점검표에 표시만 한다(503 `TRIP_PROPOSAL_UNAVAILABLE`이면 미충족 표시). 이송 단계가 있는 계획을 실행하게 되는 변경이 (e)를 관문에 넣는다.

**(f2)의 사람 확인 생산자를 켜는 일은 관리자 화면의 명시적 설정 변경이다.** 환경 변수가 아니라 `EvidenceProducerSetting` 행(버전 포함)이며, 변경마다 감사 기록(`evidence_producer.configure`)을 남긴다. 켜도 현장 확인 권한 사용자가 없으면 거짓이다.

**(g)는 관측 출처 잠금이다.** 합성(`SYNTHETIC`) 관측은 운영에서 항상 막는다. 사람이 선언한(`DECLARED`) 관측은 사람 확인 생산자(f2-i)가 켜져 있는 동안에만 쓴다. 근거와 관측을 모두 기명 사람이 맡는다는 사용자 결정(2026-11-04, "실제로 이제 동작해야 하는 시간", 완료 근거는 현장 운영자 확인)을 관측에도 같게 적용한 것이다. 사람 확인을 끄면 사람 선언 관측도 다시 막힌다. 인증된 관측 입력 경로가 생겨 새 provenance 값이 추가되면 그 값은 사람 확인 없이 통과한다.

## 플랫폼에 요청

(CONTRIBUTING §4) 읽기 전용 `GET /api/fleet/execution-profile`, viewer 이상 권한.

```json
{"profile": "production", "arm_execution": "physical", "revision": "<opaque string>",
 "observation_producers": [{"producer_id": "...", "authenticated": true}],
 "evidence_producers": [{"producer_id": "...", "authenticated": true}]}
```

- `profile`은 ROSY 배포 프로필 그대로(`simulation`·`device_trial`·`production`)이고, `arm_execution`은 ARM 경로가 실물을 실행할 때만 `physical`이다. 시뮬레이션 프로필은 `simulation`이다.
- 생산자 목록은 운영 모드에서 실제로 근거를 제출할 수 있는 인증된 생산자만 담는다. 신원 값(토큰·주소)은 담지 않는다. `revision`은 바뀔 때 바뀌는 불투명 값이다.
- Harbor는 엄격히 읽는다. `profile`이 `production` 또는 `device_trial`이고 `arm_execution == physical`이면 (f1)(2026-11-04 갱신: ROSY는 실물 팔을 `device_trial`에서만 연다, D-797·D-858 S4), 거기에 두 목록이 비어 있지 않고 모두 `authenticated: true`이면 (ii)가 참이다. 경로가 없거나 형식이 다르면 거짓이다. 계약이 달라지면 이 ADR과 `_platform_signal`을 함께 고친다.

## 결과

- 조건이 채워지면 (a)~(d)·(f2)는 코드 변경 없이 열린다. (f1)은 플랫폼이 위 신호를 내면 코드 변경 없이 참이 된다. (g)는 사람 확인 생산자를 켜면 사람 선언 관측으로 열리고, 자동 관측은 새 provenance 값이 필요하다.
- 시뮬레이션 시험은 그대로 통과한다.
- 설정 화면(`/settings/integration/`)에 조건별 충족·미충족·미확인, 점검 시각, 소유, 다음 조치를 보인다.
- 미해결: 사람 확인 근거 입력 화면과 장부 연결, 운영용 관측 입력 경로, 점검 10분 값의 현장 조정.
