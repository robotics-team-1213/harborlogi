# Harbor Compose를 R 호스트에 설치 (ADR 0021)

2026-11-04. 통합관제 R이 도는 OMX 관제 PC에 Harbor를 Compose로 설치하고 확인했다. 설치 스크립트와 작업 기록은 저장소 밖 작업 공간 기록에 있다. 주소·토큰·인증서 값은 여기에 적지 않는다.

## 설치 상태

| 항목 | 값 |
|---|---|
| 소스 | `feat/harbor-compose-deploy` 커밋 `44519cc` (팀 리뷰 전. 병합 뒤 `main` 커밋으로 다시 설치한다) |
| 위치 | 릴리스 `/opt/harbor/releases/<커밋>`, `current` 링크, 설정 `/etc/harbor` (root 전용) |
| 네트워크 | 호스트 네트워크 덮어쓰기(`/etc/harbor/compose.site.yaml`). Harbor 9443, Waitress `127.0.0.1:8060` |
| R 연결 | origin은 R의 사이트 TLS 이름과 8443, CA는 R의 공개 CA, principal은 `harbor-lan-service`(2026-10-29 발급) |
| binding | 빈 목록. 팔 작업은 HOLD다 |

## 확인한 것

| 확인 | 결과 |
|---|---|
| migrate 전 기동 | 거절됨(exit 1) |
| migrate·`bootstrap_roles` | 완료 |
| 컨테이너 상태 | `harbor` healthy, `proxy` up |
| 호스트에서 HTTPS 로그인 화면 | 200 |
| 운영자 노트북에서 tailscale을 거쳐 같은 이름으로 접속 | 200 |
| Harbor 컨테이너 안에서 R `GET /api/fleet/session` | `principal_id=harbor-lan-service`, `role=service` |
| 격리 시험 프로젝트(19443, 버리는 자료) | `/` → 302(포트 유지), 로그인 200, CSS 200, Origin을 실은 로그인 POST 200(CSRF 통과) |

## 찾아서 고친 것

- Waitress가 신뢰하지 않는 `X-Forwarded-Proto`를 지워 HTTPS로 무한히 다시 보냈다. [교훈](../solutions/2026-11-04-waitress-drops-proxy-scheme.md)에 기록했다.
- 기본 포트 8443이 R과 겹쳤다. 기본값을 9443으로 바꿨다.
- 사이트 방화벽이 브리지에서 호스트 주소의 R 포트로 가는 연결을 버린다. 그래서 R 호스트에서는 호스트 네트워크로 덮어쓴다([설치 안내](../../deploy/compose/README.md)).

## 아직 아님

- 사람 계정이 없다. 관리자가 `createsuperuser`로 직접 만든다.
- 현장 R 이미지에는 아직 ARM 위임 경로가 없다. 플랫폼 공개 게시와 서명 뒤에 들어온다. 그 전까지 R을 거친 팔 작업은 HOLD다.
- Harbor 자기 TLS 인증서는 자체 서명이라 브라우저가 경고한다. 팀 CA나 공인 인증서로 바꾸는 일은 남았다.
- 실물 업무 흐름(이송·팔 작업)은 시험하지 않았다.
