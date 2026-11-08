# 통합관제 R을 거친 ARM 시험 (`--arm-via-r`)

2026-11-03. ADR0018 공백 닫힘 조건 3(Harbor 통합 시험이 R 경로로 돈다)의 첫 실행 기록이다. 실물·운영 수용 기록은 아니다.

## 검증한 소스

- Harbor: `03469f31ed4a82ac273b1d35dbe4177109abb92b`(브랜치 `feat/arm-via-r-harness`, origin/main `e7ae789` 병합 포함).
- ROSY Platform: `ed455ea03af48100085c32487ba11a56ce80c085`(공개 `origin/main`, API v1.355 포함).
- 시험 호스트: 모델 PC(`~/rosy-jobs/harbor-arm-via-r`). 두 저장소를 shallow clean checkout해 실행했고 하네스의 전체 SHA·clean 검사를 통과했다. 끝나면 run 폴더를 지웠다. 로그는 `reports/20261104T094817Z-*.log`, `...094933Z-*.log`, full-flow 보고서 JSON.
- 이 기록은 실물·운영 수용이 아니다. 아래 「이전 실행」은 게시 전 SHA(AI PC)의 첫 기록이다.

## 공개 SHA 결과 (2026-11-04)

| 명령 | 결과 |
|---|---|
| `real_fleet_http --platform-root <platform> --platform-sha ed455ea... --arm-via-r` | 통과(1회), 이어 10회 반복 모두 통과. 실패 단계 없음 |
| 기본 모드(옵션 없음) | 통과(3 tests OK) |
| `--full-flow` | 통과(0~⑤ 6단계 모두 ok, `first_failure: null`) |

`--arm-via-r`는 owner submit 1, 관측 `PRE_DISPATCH`·`POST_ACTION`을 확인했다. 이전 1회 HOLD는 다시 나오지 않았으나 원인은 여전히 모른다(미해결 절).

## 이전 실행 (2026-11-03, 게시 전 SHA)

- Harbor: `935a9be919b6ff0f1f0e2ccfb1085b7235bfe9f3` 이후 `--arm-via-r` 수정 포함 브랜치.
- ROSY Platform: `e95c2658b29cb73f6fb00f8b7a263491444e9514`(API v1.356, 로컬 main 착지 전 묶음 브랜치).
- 시험 호스트: AI PC, git bundle로 옮긴 clean checkout.

## 구성

- R: ARM 설치 없는 Fleet. 관제 단위 설정과 R의 relay만 있다.
- 셀: 기존 `ArmFleet`(합성 owner, 독립 observer, 시험 운영자). 제안은 R의 service principal(`control-unit:R`)로 생긴다.
- Harbor: R의 origin·토큰만 받는다. 시험은 기존 `tests/integration/arm_http.py` 그대로다.

## 결과 (이전 실행)

| 실행 | 결과 |
|---|---|
| `--arm-via-r` 18회 | 17회 통과(owner submit 1, 관측 PRE_DISPATCH·POST_ACTION, 업무 장부 WAITING 유지, receipt `AT_TARGET_AND_CLEAR`, 다른 principal 403). 1회 HOLD |
| `--arm-only` 3회(기준) | 3회 통과 |

통과한 실행에서 Harbor 업무 장부는 R이 보여 준 `proposal_principal_id`(Harbor 자신)로 결속했고, 셀 운영자는 셀에서만 승인했다.

## 실행 중 고친 것

- 하네스가 `fleet.tokens['approver']`를 모든 모드에서 읽었다. R에는 승인자가 없으므로 선택 값으로 바꿨다.
- 셀 시험 운영자가 `control-unit:R` 자격으로 ARM 경로를 조회했다. 관제 단위 자격은 `/api/fleet/control-unit`에만 닿으므로(`CONTROL_UNIT_CREDENTIAL_SCOPE_REQUIRED`) 셀 운영자가 위임 목록에서 결속된 작업을 찾도록 바꿨다. 플랫폼 경계는 그대로다.
- `TMPDIR`를 긴 run 경로로 두면 합성 owner의 UNIX socket 경로가 길이 한도를 넘는다. 기본 `/tmp`로 실행했다.

## 미해결

- 1회 HOLD: owner submit 0, 관측 PRE_DISPATCH만 있는 상태에서 셀 작업이 HOLD가 됐다. 같은 조건의 수동 재현 2회와 이후 시험 17회(실패 시 셀 작업 상태·이유·최근 이벤트를 출력하는 하네스 `3a37b3a` 10회 포함)에서는 다시 나오지 않았다. 원인은 확인하지 못했다. 다음 실패 때 하네스 출력의 `== cell job` 줄이 원인 근거다.
- 공개 SHA 재시험은 위에서 끝냈다(11/11 통과). 1회 HOLD의 원인은 미확인이므로 재발하면 `== cell job` 출력으로 조사한다.
- 현장 R↔CELL-A TLS 연결은 플랫폼 D-960 수용 조건이며 이 기록 범위 밖이다.
