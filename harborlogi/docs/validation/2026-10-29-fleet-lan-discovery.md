# Harbor–Fleet LAN 자동 발견 검증

2026-10-29. 기반 `1e0dc66`, 작업 브랜치 `feat/fleet-lan-discovery`. [연결 설계](../design/25-fleet-connection-bridge.md#같은-lan-자동-발견-2026-10-29)의 소비자 구현이며 Fleet API·광고 계약을 변경하지 않는다.

## 확인한 동작

- 관리자 POST 검색은 승인된 이름·포트만 대상으로 사이트 CA와 `/healthz`를 확인한다. 서비스 토큰·프로필·Command를 만들거나 전송하지 않는다. 저장·서비스 신원/ARM 조회 검사는 기존 별도 단계다.
- `.local` origin+설치 CA로 DNS-SD를 다시 조회하고 socket IP만 바꾼다. 로컬 TLS 서버에 실제 연결해 IP 후보 변경, 고정 SNI·Host, health의 Authorization 부재와 뒤따르는 bearer 요청을 확인했다. 잘못된 인증서 이름은 HTTP 요청 전에 거절됐다.
- 중복 TXT·역할/버전·주소·포트 검사, 중복 인터페이스 병합, 충돌·부분 해석·64개 상한 실패, 서비스 삭제/재등장 후 재해석을 확인했다. 발견 실패 때 POST 재전송이나 수동 DNS fallback이 없다.
- 실제 python-zeroconf 광고와 브라우저를 loopback 인터페이스에만 연결해 DNS-SD 처리도 확인했다. 이 시험의 수신기는 광고 전에 생성했다. 이 Windows 환경에서는 광고 후 생성한 cold loopback PTR browse가 광고를 찾지 못했지만, 직접 ServiceInfo 조회와 수신 중 광고는 도달했다. 이 제한을 현장 LAN cold-start 성공으로 세지 않는다.
- Chromium/Edge 실제 브라우저에서 CA 없는 자동 찾기의 설치 안내·설정 불변·무통신, 관리자 저장·조회, 일반 운영자의 검색/저장/조회 차단, 토큰 비노출을 확인했다. 1440·1280·768·390px에서 가로 넘침과 브라우저 오류가 없었다. Fleet은 합성 인증 HTTP double이었다.

## 실행 결과

`harborlogi/`에서 실행했다. 운영 비밀 대신 별도 시험 환경 변수와 임시 DB·인증서를 사용했다.

| 확인 | 결과 |
|---|---|
| Python 3.12, `.[web]`, `python -m unittest discover -s tests` | 189 수집, 188 실행 통과, 기존 실제 Fleet 통합 1 skip |
| 추가 패키지 없는 Python, 같은 단독 시험 | 189 수집, 184 실행 통과, 5 skip: DNS-SD 패키지 시험 3 + 기존 선택 통합 2 |
| `python -m django test harbor_web.tests --settings=harbor_web.settings --noinput` | 242 통과 |
| 연결·Command·복원 집중 시험 | 41 통과 |
| `python -m django makemigrations --check --dry-run --settings=harbor_web.settings` | 변경 없음 |
| `tools/integration/fleet_bridge_browser_check.py` | PASSED, 4개 폭, 브라우저 오류 0 |
| map-editor·yard-model·yard-draft·yard-selection Node 시험 | 모두 통과 |
| `tools/design_tokens.py --check`, `git diff --check` | 통과 |

초기에는 `harbor_web.tests.settings`로 제품 전체를 실행해 7개 화면 단언이 실패했다. 이 설정은 인증 시험용으로 presentation context를 제거한다. 변경 전 main에서도 같은 실패가 있었으나 제품 결함으로 분류하지 않는다. CI의 `harbor_web.settings`로 전체를 다시 실행한 위 242 통과가 제품 근거다. 정적 파일을 수집하지 않은 시험 환경의 기존 경고는 별도이며 브라우저 harness는 정적 자산을 준비한다.

브라우저 원본 보고서와 합성 캡처는 Git 제외 `runs/lan-discovery-browser/run-cf8e313403774cc0851ffb6573f245dc/`에 있다. 최종 검토는 발견의 부분 결과와 대소문자 authority 문제를 재현하고 수정한 뒤 남은 actionable finding 없음으로 종료했다. 검토자 환경의 zeroconf 없는 3 skip을 실제 광고 통과로 세지 않는다. CI의 web job에는 발견 시험을 명시해 선택 의존성이 있는 환경에서 실행한다.

## 수용 경계와 복구

실제 ROSY Fleet·현장 LAN cold-start·공유기 변경·Pi/팔 동작·물리 receipt·현장 업무 수용은 실행하지 않았다. 설치자는 예시 파일에 실제 승인 사이트 이름·포트를 별도 공급하고 PEM 사이트 CA·서비스 자격을 설치해야 한다. 기존 Fleet Avahi 광고·TLS proxy·UDP 5353 경로를 점검한 뒤 현장 서버에서 자동 찾기와 서비스 조회를 확인한다. 검색 결과는 그 시점의 주소 관측이며 실행 승인이나 물리 수용이 아니다.

실패하면 광고 충돌·동일 링크·CA/인증서·포트·health를 복구한다. 자동 재전송하지 않고 기존 key와 원래 Fleet에서 작업을 대조한다. 미해결 Command가 있는 IP origin을 새 이름으로 덮어쓰지 않는다. 다른 사이트로 이관하려면 기존 복구·이관 절차를 따른다.

CE: 해당 없음 — 이름/CA와 주소를 분리하는 기존 ROSY 교훈을 적용했고, 이번 변경의 판단은 최종 코드·회귀 시험·위 연결 설계에 남아 있다. 별도 교훈 문서를 중복 생성하지 않았다.
