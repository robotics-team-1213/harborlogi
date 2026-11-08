# 한 Fleet 전체 흐름 모드 첫 실행 (D-927 흐름 E)

2026-10-30. 플랫폼 D-927 결정 2·7과 [ADR 0016](../adr/0016-demo-seams-follow-platform-d927.md) 4항에 따라 `real_fleet_http --full-flow`를 추가하고 처음 실행한 기록이다. 진척 지표는 처음 실패한 단계 번호다.

## 무엇을 바꿨나

- `--full-flow`: simulation Fleet 하나가 TRIP(합성 Pinky)과 ARM(합성 UDS owner)을 함께 서빙하고, 단계를 차례로 실행해 처음 실패에서 멈춘다. `--report`로 단계별 JSON을 남긴다.
- `ArmFleet`을 `install()`(owner 시작과 ARM 설치 인자)과 `attach()`(승인·관측 스레드)로 나눴다. `RealFleet`은 `extra_app`으로 같은 프로세스에 ARM을 설치한다.
- 단계와 Harbor 호출의 대응은 업무 실행기(흐름 A)가 순서를 소유하기 전까지 잠정이다: 0 한 Fleet, ① 계획 고정, ② 재취급 이동(ARM), ③ 이송(TRIP), ④ 반출 이동(ARM), ⑤ 복귀 이동과 퇴피 근거.

## 실행

| 항목 | 값 |
|---|---|
| 호스트 | `remote_pytest --pick sim`이 고른 공식 시험 PC, `nice -n 15 ionice -c3` |
| Harbor | `a72e43b24d219c3048e8f3e65ebf13684e207f0f` (`feat/full-flow-one-fleet`, 미게시) |
| 플랫폼 | 로컬 main `8acadbb82e5ec771f262aabc26d67ab0f82f94c7` (D-927 포함, 미게시) |
| 운반 | [Linux 시험 PC 운반 절차](../solutions/2026-10-27-linux-testpc-shipping.md), 두 checkout `status --porcelain` 0줄 |

## 결과

| 실행 | 결과 |
|---|---|
| `--full-flow` | 0 한 Fleet 통과, ① 통과, **② 실패**: Fleet `GOAL_CONFIRMED`(합성 owner Submit 1·Get 1, 관측 `PRE_DISPATCH`·`POST_ACTION`) 뒤 업무 근거 어댑터 없음 |
| 기본(Cell) | 3실행 통과 |
| `--transport-only` | 1실행 통과 |
| `--arm-only` | 1실행 통과 |
| `--web-arm-only` | 1실행 통과 |
| 기본 unittest | 180실행 통과, skip 1 |

처음 실패한 단계는 ②다. Fleet 쪽 ARM 실행은 한 Fleet 안에서 끝났고, Harbor가 Fleet 물리 근거(v1.330)를 `PlacementEvidence`·`RetreatEvidence`로 바꾸는 어댑터(설계 31 P2)가 없어 업무 확인에서 멈췄다.

## 다음에 막힐 곳 (②를 넘긴 뒤)

- 합성 설치에는 source→target 대응이 하나뿐이고 합성 owner는 성공 때 물체를 target에 둔다. ④·⑤의 두 번째 ARM 이동은 이동별 대응(S4, D-927 결정 6)과 이동마다 장면을 맞추는 owner가 필요하다.
- ④ S 내려놓기의 Harbor 호출은 업무 실행기가 정한다. 지금은 반출 이동으로 대신한다.

## 이 기록이 세지 않는 것

owner·관측·승인은 하네스 합성이다. 플랫폼 시뮬 owner(MuJoCo), `tools/sim/transfer_observer.py`, 매일 밤 실행은 아직 연결하지 않았다. 물리 시뮬·실물·현장 수용의 근거가 아니다.

## 2차 실행: 업무 근거 연결 뒤 (같은 날)

설계 31 P2의 Fleet 쪽 연결로 `harbor/physical_evidence.py`를 추가했다. 확인된 ARM 시도의 v1.330 근거를 받아, 바이트가 검증된 영수증 digest·시도와 맞을 때만 최신 물체 표본을 `PlacementEvidence`로, 측정 관절·해제 zone·플랫폼 clearance 판정을 `RetreatEvidence`로 바꾼다. 합성 관측은 측정 관절을 함께 보내고, 합성 장면은 이동마다 물체를 설치된 source에 둔다(S4 전의 shortcut).

| 실행 | 결과 |
|---|---|
| `--full-flow` (Harbor `24ec71b`, 플랫폼 같은 핀) | 0·①·②·③·④ 통과, **⑤ 실패**: 복귀 이동은 Fleet `GOAL_CONFIRMED`, 최종 완료에서 `Observation identity was already consumed` |
| 기본 unittest (노트북) | 183실행 통과, skip 2 |

⑤의 원인은 계약 공백이다. `BusinessLedger.finish`는 반출 이동 attempt에 묶인 새 관측(반출 화물이 반출대에 있고 관련 팔 모두 퇴피)을 요구한다. Fleet 근거는 attempt마다 한 번 저장되는 불변 기록이라 ④에서 쓴 근거밖에 없고, 같은 근거 ID는 장부가 거절한다. 하네스에서 우회하지 않았다. 결정이 필요하다:

- (가) Fleet에 마지막 이동 뒤 재관측(반출 화물 위치·전체 팔 퇴피)을 받는 계약을 추가한다(플랫폼 D-927 결정 8 절차).
- (나) Harbor 최종 완료가 마지막 이동의 퇴피 근거와 반출 이동의 배치 근거를 함께 받도록 장부 규칙을 바꾼다(Harbor ADR 0005 결정 6 갱신).

재회귀(Harbor `cf0bd4e`, 같은 플랫폼 핀, 같은 시험 PC): `--full-flow`은 위와 같이 ⑤에서 멈추고, 기본 3·`--transport-only` 1·`--arm-only` 1·`--web-arm-only` 1실행 모두 통과, 기본 unittest 183실행 통과(skip 1). 관측 표본에 측정 관절을 넣은 하네스 변경이 기존 모드를 깨지 않았다.

## 3차 실행: 재관측 계약으로 ⑤ 해결 (같은 날)

위 (가)를 택했다. 플랫폼 D-930(API v1.334, 브랜치 `feat/arm-transfer-recheck` `800aec0d82`, 로컬 main 착지 대기)이 확인된 시도의 재관측을 추가했고, Harbor `fleet_recheck_evidence`가 반출 시도의 재관측을 요청·조회해 새 ID의 배치·퇴피 근거로 최종 완료를 닫는다. 같은 근거를 다시 쓰지 않는다.

| 실행 (Harbor `67ed6e9`, 플랫폼 `800aec0d82`) | 결과 |
|---|---|
| `--full-flow` | **0·①·②·③·④·⑤ 모두 통과**, 업무 장부 `COMPLETED` |
| 기본 3·`--transport-only` 1·`--arm-only` 1·`--web-arm-only` 1 | 모두 통과 |
| 기본 unittest | 185실행 통과(시험 PC skip 1, 노트북 skip 2) |

owner·관측·승인은 여전히 하네스 합성이고, 이동별 대응(S4)과 ④의 업무 호출은 shortcut이다. 플랫폼 MuJoCo owner·`transfer_observer.py`·매일 밤 실행으로의 교체가 다음 단계다.

## 4차 실행: 플랫폼 시뮬 관측자로 교체 (같은 날)

정상 단계 관측을 하네스 스레드에서 플랫폼 `tools/sim/transfer_observer.py`(D-927 결정 3, 브랜치 `feat/sim-transfer-observer` `7adb1caf14`, 로컬 main 착지 대기)로 옮겼다. 하네스는 시험 운영자 승인과 합성 owner가 움직이는 scene만 남기고, scene을 시뮬 상태 파일로 쓴다. 관측자는 별도 프로세스·관측자 자격으로 PRE_DISPATCH·POST_ACTION·RECHECK 문맥에 답하며, 링크 경계는 측정 관절의 URDF FK, 속도는 관절 속도에서 계산한다. 팔 기준 자세는 공유 zone 밖을 향하게 두었다(S4 전 shortcut).

| 실행 (Harbor `743efaa`, 플랫폼 시험용 병합 `9696cd87ac` = D-930 `3de9a2527f` + 관측자 `7adb1caf14`) | 결과 |
|---|---|
| `--full-flow` | **0~⑤ 모두 통과**, 업무 `COMPLETED`, 관측 phase PRE_DISPATCH·POST_ACTION·RECHECK |
| 기본 3·`--transport-only` 1·`--arm-only` 1·`--web-arm-only` 1 | 모두 통과 |

시험용 병합 커밋은 두 브랜치를 합친 것이며 문서 충돌을 풀지 않았다(코드 충돌 없음). 두 브랜치가 로컬 main에 착지하면 main SHA로 다시 고정한다. owner는 아직 합성이고 MuJoCo owner 연결은 다음 단계다.
