# 2026-11-04 고정 양 끝 실제 Fleet 전체 흐름 (기준 갱신)

목적: `docs/platform-contract.md`의 기준 플랫폼을 v1.292에서 현재 공개 main으로 옮기기 전에, 고정한 양 끝으로 실제 Fleet HTTP 흐름을 다시 확인한다.

| 항목 | 값 |
|---|---|
| 호스트 | 모델 PC, 사용자 유닛 `rosy-harbor-nightly.service`(D-927 E). 수동으로 시작했다 |
| 스크립트 | `tools/integration/nightly_full_flow.sh`. 두 저장소를 shallow clean checkout하고, 플랫폼 의존성 venv에서 `real_fleet_http --full-flow`를 실행한다 |
| Harbor | `7ceda0fe71d6fb7b751f1c232531232238d00782` (팀 main, PR #20–#25 병합 후) |
| 플랫폼 | `ed455ea03af48100085c32487ba11a56ce80c085` (공개 main, API reference v1.355, D-965 포함) |
| 보고서 | 모델 PC `~/rosy-jobs/harbor-nightly/reports/20261104T015558Z.json` |

## 결과

종료 코드 0, `first_failure` 없음.

| 단계 | 결과 | 초 |
|---|---|---|
| 0 한 Fleet | 통과 | 0.01 |
| ① 반출 전 변경 지시 | 통과 | 0.01 |
| ② 재취급 화물 처리(ARM) | 통과 | 0.53 |
| ③ 이송(TRIP) | 통과 | 2.40 |
| ④ S 내려놓기 | 통과 | 0.75 |
| ⑤ 재취급 정리·팔 퇴피 | 통과 | 1.03 |

## 함께 바꾼 것

- 야간 실행 설정 `~/rosy-jobs/harbor-nightly/harbor-nightly.env`가 2026-10-30 브랜치 `feat/full-flow-one-fleet`를 계속 쓰고 있었다. 이를 `HARBOR_REF=main`으로 바꿨다(이전 파일은 `.20261104`로 보존). 이제 매일 밤 팀 main과 플랫폼 main을 함께 확인한다.

## 이 결과가 말하지 않는 것

- 시뮬레이션 Fleet 하나에서의 계약 확인이다. 운영 R(robttt)은 계정 DB 전환 보류로 아직 2026-11-02 후보(`480de9e`, D-965 없음)를 쓰고 있어, 현장 ARM은 503이다.
- 실행 하네스는 이송을 운영자 신원의 `trip-jobs`로 제출한다. PR #26의 D-908 제안 경로(서비스 신원)는 이 실행에 포함되지 않았다.
- 실물 장치와 인증된 현장 producer는 포함하지 않는다.
