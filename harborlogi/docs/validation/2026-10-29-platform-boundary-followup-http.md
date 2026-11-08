# 플랫폼 경계 분리 후속 HTTP 검증

2026-10-29, 승인된 팀 시험 PC에서 실제 Fleet 서버와 설치된 Harbor 패키지를 연결했다.
과거 검증 기록은 수정하지 않았다. 이 기록은 합성 실행·호스트 계약 검증이며
운영 서버 연결, 실제 관측 producer의 진위나 실물 수용을 뜻하지 않는다.

## 후속 수정 후보의 고정 실행

- Harbor: `1e0dc6667cbed25fa41a57c7261654ff496e804a` (PR #48 병합 main).
- ROSY: `4841c731229ffede702758ac9476c40f05e1364e`.
- 양쪽 source clone은 실행 전후 clean이었고, harness가 플랫폼의 전체 SHA를 확인했다.
- 부모 harness는 승인된 플랫폼 시험 환경을, 자식은 별도 venv의 설치된 Harbor wheel을 썼다.
  설치 import가 `site-packages`에서 왔고 `pip check`가 통과했다.
- 기존 [Unix 소켓 경로 교훈](../../../docs/solutions/integration-issues/unix-socket-path-budget-2026-10-29.md)에
  따라 task 전용 짧은 임시 경로를 사용했다. 시스템 패키지와 공용 venv는 수정하지 않았다.

명령은 `python -m tools.integration.real_fleet_http --platform-root <clone>
--platform-sha <전체 SHA> --app-python <설치 Python>`이며 모드별로
`--arm-only`, `--web-arm-only`, `--transport-only`, 플래그 없음을 사용했다.

| 모드 | 실행 | 결과 |
|---|---:|---|
| ARM | 1 | OK, skip0, runner exit0 |
| 인증 웹 ARM | 1 | OK, skip0, runner exit0 |
| TRIP | 1 | OK, skip0, runner exit0 |
| Cell | 3 | OK, skip0, runner exit0 |

ARM과 웹 ARM은 각각 합성 UDS submit1/lookup1 및 PRE_DISPATCH·POST_ACTION 근거를 확인했다.
Cell은 설치 compile proposal과 job을 HTTP로 소비했다. Harbor 내부의 업무 장부·명령
서비스·스케줄러는 기존 정규화된 실행 snapshot 경계를 유지한다.

환경과 runner 종료 코드까지 한 번에 결속하기 위해 같은 두 pin으로 6개를 재실행했다.
앞선 네 로그도 보존했으며, 아래 표는 별도 재실행 결과다. 서로 다른 두 실행의
건수를 합산하지 않는다. 전체 receipt에는 전후 clean과 SHA 재확인, 두 interpreter의
분리된 prefix, 자식의 site-packages import, pip check와 각 runner exit0이 함께 있다.
원본은 실행자의 외부 임시 폴더 `harbor-boundary-http-receipt-20261029`에 보존했다.

| 로그 | SHA256 |
|---|---|
| `arm.log` | `37ec37594f2d9292806902556f3b5b88979cafbe161d5284821b05270c8a5547` |
| `web-arm.log` | `4431c3a5ecb52bb5537ec5ac8de8177975236e913e36724a416867014f7a8598` |
| `transport.log` | `5562d98fc7dfcc9ec38228fe30dbe764af590d883c1707c89787383ae8fa5ddf` |
| `cell.log` | `1d302891de1ae402d9b43df9b17aa989a4f5200fdbf58ed91f91fe719fb9e33a` |
| `receipt.log` | `2e09ba796e31a73d39854f437e1b384687942702e8b3e481c7c29cbffd49a573` |

## 2026-10-30 후속 정책 후보의 호환성

새 profile·observer 구현과 게시 정책을 포함한 ROSY
`799ad9c9df7a41a8c300898c4d4eecc4b40f42e8`에서도 같은 Harbor
`1e0dc6667cbed25fa41a57c7261654ff496e804a`를 독립 설치해 실행했다.
공식 배치 도구가 선택한 AI 역할의 팀 시험 PC에서 검증했다.
ARM1·인증 웹 ARM1·TRIP1·Cell3 총6개가 각각 OK/skip0/runner exit0였다.
두 ARM의 합성 UDS는 submit1/lookup1/PRE_DISPATCH·POST_ACTION을 기록했다.
전후 clean·SHA·분리된 interpreter prefix·site-packages·pip check와 결과 해시는
동일 receipt에 결속했다. 선행 실행과 이 실행의 건수는 합산하지 않는다.

원본: 실행자의 외부 임시 폴더 `harbor-boundary-http-799ad9c9df-20261030`.

| 로그 | SHA256 |
|---|---|
| `arm.log` | `d2ecc94dc8d7c6230f341208c99474c358abbb10e8bedc6557578abf4e5249cf` |
| `web-arm.log` | `c6c44827157b797dd80180a27a7e53bd6697b705f6d617d6a4849b0f212744b6` |
| `transport.log` | `3725b9122cd6a5d70b174cdb465a36e1a9a664259a11840476c1fca363bedbfb` |
| `cell.log` | `b46e4867258accfd170fa8b85c14fe9a7de1e72823c777b94c77ffbbaec7edc3` |
| `receipt.log` | `f64276a3f928c3e3820105258f708dae6c3a8b5311446952638e78cd43bf81fc` |

## 최종 게시 후보

최종 플랫폼 FULL과 게시 SHA는 아직 확정되지 않았다. 확정된 SHA가 최신 HTTP pin인 799ad9c9df와 다르면
새 exact-pin 실행 결과를 이 초안에 반영한 뒤 기록을 커밋한다.

CE: 해당 없음 — 기존 Unix 소켓 경로 교훈을 적용했고, 이번 실행에서 새 원인이나
복구 지식을 추가하지 않았다. 검증 범위와 양 끝 SHA는 이 기록에 직접 남겼다.
