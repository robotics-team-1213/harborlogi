# Harbor ARM 실행 대조 경계 검증

2026-10-29. ADR0013과 상세설계24의 ARM 실행 대조 경계를 구현하고,
기존 호환 진입점의 장부 버전 보존을 보완했다. 실물·운영 수용 기록은 아니다.

## 검증한 소스

- Harbor: `0ba3e4cbf94cc0f0875704ab77ea07fd2e16aad8`.
- 최초 대조 분리: `f1efb1742bce07b74d35e16ff77cc690aca947d4`.
- ROSY Platform: `bb38fdcdd0140873e4e9d0133662982c1d6a30e0`, 기존 ARM 수용 pin/API v1.312.
- 통합 시험 호스트: 공식 model-role 시험 PC. site PC·운영 Fleet·실물 장치 미사용.
- 양 끝 소스는 자기완결 shallow checkout으로 운반했다. 하네스의 전체 SHA·clean
  checkout 검사를 통과했고, 이전 Windows→Linux 운반 교훈을 적용했다.

## 변경과 회귀 재현

`harbor/execution.py`가 원래 key·mission·principal과 job/receipt의 결속을
검증한다. 업무 장부와 CommandFact는 동일한 대조 결과를 사용한다.
웹과 상시 실행기는 같은 `reconcile_command()` 경로를 사용한다.
독립 업무 근거 없이 실행 결과만으로 업무를 완료하지 않는다.

추가 검토에서는 `refresh`·`resume_arm`·`recover_arm`의 최초 장부 조회와
공통 대조 내부의 두 번째 조회 사이에서 HOLD가 변경되면, 변경된 버전을 새 기준으로
받아들이는 문제를 재현했다. `test_compatibility_entry_points_keep_the_original_ledger_version`의
세 subcase는 최초 분리 커밋에서 모두 `ValueError not raised`로 실패했다.
각 진입점이 처음 읽은 row를 공통 대조에 넘기도록 수정한 뒤 통과했다.
최종 저장의 `_check_current`가 같은 원본 버전을 검사하며 새 HOLD를 덮어쓰지 않는다.

## 결과

| 시험 | 최종 결과 | 해석 |
|---|---|---|
| `python -m unittest discover -s tests -v` | 173 실행, 172 통과, 1 skip | 기본 discovery의 실제 Fleet 통합은 미설정 skip |
| `python -m harbor_web.manage test harbor_web.tests --noinput -v 1` | 233 실행, 통과 | 실제 `harbor_web.settings`, test mode, 임시 DB |
| `real_fleet_http --arm-only` | 1 실행, 실패0, skip0, 1.172초 | 고정 실제 Fleet HTTP와 합성 UDS·관측 |
| `real_fleet_http --web-arm-only --app-python <웹 Python>` | 1 실행, 실패0, skip0, 4.772초 | 인증된 Harbor 웹에서 같은 실제 Fleet HTTP 경로 |
| 읽기 전용 별도 코드 리뷰 | 최초 변경·버전 보존 보완 모두 지적 없음 | 시험 실행을 대신하지 않음 |

통합 명령은 Harbor의 `harborlogi/`에서 별도 플랫폼 시험 환경으로 실행했다.

```text
python -m tools.integration.real_fleet_http --platform-root <고정 플랫폼 checkout> --platform-sha bb38fdcdd0140873e4e9d0133662982c1d6a30e0 --app-python <웹 Python> --arm-only
python -m tools.integration.real_fleet_http --platform-root <고정 플랫폼 checkout> --platform-sha bb38fdcdd0140873e4e9d0133662982c1d6a30e0 --app-python <웹 Python> --web-arm-only
```

각 통합 실행은 합성 UDS Submit 1회·Get 1회, 물리 근거 `PRE_DISPATCH`·`POST_ACTION`
각 1건을 남겼다. 웹 시험의 미수집 `web-static` 디렉터리 경고는 있었으며 실패는 없었다.
이는 브라우저 렌더링·정적 배포 검증이 아니다. 원본 로그는 개발 PC의 저장소 밖
`harbor-reconciliation-evidence-20261029/`에 회수했고 시험 PC의 임시 checkout·환경은
정리했다. 개발 PC의 `harbor-reconcile-http-20261029/` 임시 소스 폴더는 자동 승인
검토가 재귀 삭제를 정책상 거부하여 보존했다.

## 한계와 다음 작업

- 최신 플랫폼 main 전체의 호환성, 실물 실행, 인증된 현장 producer 및 운영 수용은 미검증이다.
- Fleet HOLD·취소 대기는 유지하고, 결과 불명은 원래 request key로 조회한다.
- 플랫폼의 공정 컴파일 중복 개선은 이번 변경에 포함하지 않았다. 점검 당시 제품
  워크트리는 main 포함 17개로 운영 상한 15개를 넘었다. 슬롯을 확보하기 전 새 작업을 만들지 않는다.
- 이 기록과 코드는 로컬 작업 브랜치의 결과이며 push·PR·병합을 뜻하지 않는다.

CE: 해당 없음 — 재현 조건과 버전 보존 판단이 코드·회귀 시험에 드러나므로 별도 해결 교훈을
추가하지 않았다. 기존 Linux 운반 교훈은 그대로 적용했으며 실물 복구를 주장하지 않는다.
