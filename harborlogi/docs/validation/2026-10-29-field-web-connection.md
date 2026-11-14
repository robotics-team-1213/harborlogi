# 실제 Fleet에 대한 Harbor 웹 연결 점검

2026-10-29. 실행 소스는 기존 검증 commit `403e3b6`의 wheel이며 핵심 모듈이 Git 객체와 일치함을 확인했다. 기존 로컬 preview와 업무 자료를 사용하지 않고 현장 호스트의 일회성 container·tmpfs DB에서 실행했다.

## 확인한 흐름

실제 관리자 웹 GET, CSRF 검사를 켠 자동 발견 POST, 공통 연결 점검의 실제 Fleet HTTPS 조회를 수행했다. 이 시험의 관리자는 일회성 DB의 합성 사용자이며 운영 사용자 계정을 만들거나 바꾸지 않았다. 기존 서비스 자격은 현장 호스트에서 read-only 파일로 읽어 시험 프로세스 안에서만 사용했다. 전송 payload·container 설정 환경·로컬 preview에는 토큰을 넣지 않았다.

| 항목 | 결과 |
|---|---|
| 웹 자동 발견 POST | HTTP 200, 새로운 점검 결과 보관 확인 |
| 실제 서비스 신원 | session MATCHED |
| HTML의 토큰 원문 | 없음 |
| 운영 API 메서드 | GET만 사용 |
| 새 업무 Command / 연결 프로필 | 0 / 0 |
| 기존 Fleet container와 candidate | 보존 |
| 최종 연결 진단 | FAILED / ARM_API_UNCONFIRMED |

현재 설치 source `c47d7c3`에는 ARM 경로가 없다. 따라서 이 결과는 자동 발견·CA/TLS·서비스 인증·웹 경로를 확인한 것이며 ARM 읽기 API의 준비 완료를 뜻하지 않는다. 새 서명 후보를 적용한 뒤에는 같은 실제 연결 경로에서 별도 확인해야 한다. 이번 시험은 작업 제출·승인·팔 동작을 수행하지 않았다.

## 실행과 한계

wheel과 기존 local CI 환경의 동일 버전 Django/web 의존성을 read-only로 제공하고, 설치된 Fleet image의 일회성 환경에서 실행했다. root filesystem은 read-only, capabilities는 모두 제거했고 CPU0.5·memory512MiB·pids96·tmpfs256MiB로 제한했다. 업무 volume·장치·Docker socket·게시 포트는 제공하지 않았다. 끝난 뒤 시험 container와 임시 자료를 제거했다.

Git 밖 배포 작업 기록의 `check_web_connection.py`와 `web-connection-result.json`에 실행과 결과가 있다. harness는 실행 전에 독립 읽기 전용 계약·자격 검토를 받았다. 원격 pytest가 아니라 실제 웹/인증/통신 경로의 통합 점검이다. 사이트 호스트 자체의 mDNS 성공을 다른 PC·DHCP 변경·현장 업무·물리 수용으로 사용하지 않는다.

CE: 해당 없음 — 이 점검 결과는 기존 연결 설계와 검증 기록을 보완한다. 플랫폼 통합 후보에서 별도로 발견한 실행·녹화 문제와 그 교훈은 해당 플랫폼 저장소가 소유한다.
