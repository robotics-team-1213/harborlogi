---
module: Harbor Fleet integration harness
date: '2026-10-29'
problem_type: integration_issue
component: testing_framework
severity: medium
symptoms:
  - 'ARM harness startup fails with OSError: AF_UNIX path too long.'
  - 'No synthetic owner submission occurs; cleanup reports an incomplete attempt.'
root_cause: config_error
resolution_type: environment_setup
tags: [unix-socket, tmpdir, remote-testing, fleet]
---

# 원격 ARM 하네스는 임시 경로의 소켓 길이까지 계산한다

## 증상과 원인

고정 Fleet와 Harbor의 깨끗한 소스를 원격 시험 PC로 옮긴 뒤 ARM 하네스가
`AF_UNIX path too long`으로 시작하지 못했다. 제출·조회·물리 proof는 모두 0건이었다.
종료 시의 “exactly one admitted physical attempt” 오류는 이 시작 실패의 후속
검사이며, 승인 정책이나 application 제출의 실패 증거가 아니다.

`harborlogi/tools/integration/real_fleet_http.py`는 임시 디렉터리 아래 `fleet`을 만들고,
`arm_fleet_harness.py`는 그 아래 `sockets/<instance>/control.sock`를 연다.
긴 run 이름에 `TMPDIR`와 임시 prefix까지 더하면 pathname Unix socket의 경로
제한을 넘을 수 있다. 기존 Windows→Linux 운반 교훈의 CRLF·ensurepip·Git
자기완결성 검사만으로는 이 제약을 찾지 못한다.

## 해결과 재발 방지

원격 저장소 규칙의 `runs/<이름>-<6 hex>/` 안에서 짧은 작업 전용 임시 경로를
정하고 `TMPDIR`로 전달한다. 실제 장부·소스·venv와 임시 소켓의 위치는 같을
필요가 없다. 예를 들어 긴 소스 run과 별도로 `runs/hb-<6 hex>/t/`를 사용할 수 있다.
공유 `/tmp`나 다른 작업의 폴더를 빌리지 않는다.

실행 전에 최종 소켓 경로 전체의 UTF-8 바이트 길이를 확인한다. 가상 경로만
계산하지 말고 임시 디렉터리 prefix와 instance ID도 포함한다. 실패했으면 기존
로그를 보존하고 다음 시도에서 제출 0건과 정상 완료를 각각 확인한다.

## 검증과 복구

검증 기록에 고정한 Harbor·Fleet revision의 소스 변경 없이 임시 경로만 줄였다.
ARM·인증 웹 ARM은 각각 1개 시험이 통과했고 각 실행의 synthetic UDS submit 1,
lookup 1, PRE_DISPATCH/POST_ACTION proof를 확인했다. TRIP 1개와 Cell 3개도 통과했다.
이 결과는 simulation 하네스 검증이며 실물 수용이 아니다.

필요 로그를 회수한 뒤 종료된 작업의 소스 run과 짧은 임시 run만 정리한다.
재실행할 때는 새 작업 전용 경로를 만들고 원래 SHA를 다시 확인한다.
실행·설치 검증과 로그 hash는
[경계 분리 검증 기록](../../../harborlogi/docs/validation/2026-10-29-complete-application-boundaries.md)에 둔다.

다음 적용 조건: 저장소 정책에 맞는 긴 원격 run 경로 아래에서 ARM UDS 하네스를
시작하거나, 코드 변경 없이 owner socket 시작만 실패할 때.
