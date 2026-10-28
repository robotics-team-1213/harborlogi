# 원본 물리 증거의 HTTP 연결 검증

2026-10-29, 팀 시험 PC. 플랫폼 `ad164043f64814b02543b575caf0ef3a4e19818c` (D-915/API v1.330), Harbor 코드·시험 하네스 `76844268f40e0b595d1d8821dc7b347b183ec1dd`를 고정했다. 설치·배포·실물 수용 기록이 아니다.

## 실행과 결과

플랫폼 의존성이 있는 Python에서 다음 하네스를 실행하고, Harbor 소비자는 별도 Python 프로세스로 실행했다. 양쪽 checkout의 전체 SHA와 clean 상태는 하네스가 검사한다. 시험용 자격증명·주소는 실행마다 생성하며 기록하지 않는다.

```text
python -m tools.integration.real_fleet_http --platform-root <pinned-platform-checkout> --platform-sha ad164043f64814b02543b575caf0ef3a4e19818c --app-python <isolated-harbor-python> --evidence-only
```

- 어댑터 8개, 실제 Fleet HTTP 통합 1개 통과. 원격 job terminal exit 0과 보존된 시험 로그를 함께 확인했다.
- 실제 UDS SubmitAction 1회·조회 1회, 독립 시험 관측자의 PRE_DISPATCH/POST_ACTION proof를 사용했다.
- 관절 표본이 없는 이전 fixture에서는 실제 physical-evidence GET이 409로 거절됐다. 합성 관절 표본을 명시적으로 추가한 뒤 GREEN을 확인했다. 구형 `--arm-only`의 기본 입력에는 새 필드를 추가하지 않는다.
- Harbor service는 원래 proof의 SHA·시각·시도·A/B 설치 대응을 검사하고 기존 장부의 한 단계를 확인했다. 다른 service의 proof 조회는 403, 동일 원문 재조회는 동일 결과였다.
- 마지막 이동 확인 후 상태는 `AWAITING_FINAL_EVIDENCE`다. 같은 proof로 `finish()`를 시도하면 거절되고 상태를 유지한다. 최종 업무 완료를 증명하지 않는다.
- 같은 Harbor 코드의 웹 회귀 234개 통과. 단독 표준 라이브러리 discovery는 177개 중 175개 통과·별도 연동 2개 skip이다. 이 skip은 실제 HTTP 통과 증거와 구분한다.
- 플랫폼 관련 계약·변환·observer·World 및 이전 시간 초과 2개 항목의 표적 회귀는 150개 통과, NEW/known/flaky 0이다. 전체 플랫폼 행렬은 별도이며 타이밍 항목 실패와 부하로 인한 skip이 남아 있어 전체 GREEN으로 보고하지 않는다.

원본 로그는 실행 세션의 외부 scratch에 보존했다: `%TEMP%/harbor-g1-76844268f4-f8639b.log`, `%TEMP%/rosy-pipeline-g1-harbor-web-full.txt`, `%TEMP%/rosy-pipeline-g1-compat-green/run-1.txt`. 이전 HTTP 실행은 SSH 연결 종료 뒤 원격 terminal 0을 확인했지만 stdout 전체를 확보하지 못해 수용 근거로 쓰지 않았다. 위 성공은 분리 실행·지속 로그·terminal code를 모두 확보한 재검증이다.

## 수용하지 않은 범위

이 하네스는 실제 HTTP·인증·Fleet DB·UDS를 사용하지만 관절·물체·link·그리퍼 값은 합성 fixture다. Gazebo 물리 생산자, 원본 source clock의 UTC 대응, 운영 카메라, 실물 팔, 전체 Harbor 업무 완료, 브라우저 화면 수용을 증명하지 않는다. provider는 명시적 설정이 있어야 선택되고 최종 근거를 생성하지 않는다. 기존 Fleet 승인·명령 권한을 바꾸지 않았다.

CE: 해당 없음 — 소비자 변경의 판단은 코드·회귀에 남고, 운영·장치의 해결되지 않은 source-clock 결손은 별도 ROSY 작업 기록으로 관리한다.
