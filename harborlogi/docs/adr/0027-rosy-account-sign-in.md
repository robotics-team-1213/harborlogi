# ADR 0027: 사람 로그인은 Rosy 계정으로 하고, Harbor 역할은 Harbor가 판단한다

날짜: 2026-11-07

상태: 수락(2026-11-07, 사용자 결정 "모든 어플리케이션 rosy는 core 인 account 나 기타를 해서 서로 연동이 되어야해", Harbor 진행 방식 권고 승인). 구현과 단독 시험 완료. 운영 client 등록·설치·실사용 수용은 미완료다.
- 번호: ADR 0024~0026은 열린 PR이 쓴다. 병합 시점 `main`의 번호를 다시 확인한다.
- 고치는 결정: [ADR 0015](0015-service-identity-and-independent-integration.md) 결정 1(사람 인증). 결정 2~8(서버 간 service 신원, 매 호출 결속 등)은 그대로다.
- 플랫폼 쪽 근거: rosy-platform D-955 결정 7과 `docs/reference/rosy-account-protocol.md`의 「바깥 앱 수신 계약」.

## 배경

ADR 0015는 Harbor와 Fleet이 각자 로그인·세션·역할을 갖고, 공통 IdP가 생기면 issuer·주체 대응·로그아웃·역할 발급을 따로 검증하기로 했다. 이제 Rosy 허브(Fleet의 Account)가 그 IdP다. 팀원은 Rosy 계정 하나로 Fleet·로봇·학습·Monitoring에 들어가는데, Harbor만 따로 계정을 받아야 했다.

## 결정

1. **로그인:** Harbor는 Rosy 허브의 authorization code + S256 PKCE로 사람을 로그인시킨다. Harbor 서버가 state·verifier를 세션에 두고, 토큰 교환과 재확인은 서버에서 HTTPS로 한다. 브라우저 URL에 토큰을 싣지 않는다. ROSY 코드는 import하지 않는다.
2. **역할 대응:** 접근 토큰의 resource(`site:<id>`) grant에서 `harbor.view`·`harbor.operate`·`harbor.field`·`harbor.admin`만 읽어 Harbor 그룹 viewer·operator·field·admin에 그대로 대응시킨다. 업무 권한 판단(`require_roles`)은 지금처럼 Harbor 안에서 한다. `harbor.*`가 하나도 없으면 로그인시키지 않고 허브 세션 family를 회수한다.
3. **주체:** Harbor 사용자 이름은 `rosy-<account_id>`이고 비밀번호는 쓸 수 없다. 표시 이름은 허브 `userinfo`의 `display_name`이며 권한 판단에 쓰지 않는다. `harbor.admin`일 때만 Django 관리 화면(`is_staff`)을 연다.
4. **재확인과 로그아웃:** 최대 10초마다 허브 introspect로 확인한다. 비활성이면 refresh로 회전을 시도하고, 실패하거나 `harbor.*`가 사라지면 Harbor 세션을 끝낸다. 허브에 닿지 않으면 마지막으로 확인한 토큰의 만료까지만 유지한다. Harbor 로그아웃은 허브 family를 회수한다.
5. **로컬 계정:** 기존 Harbor 계정 로그인은 복구용으로 남긴다. `HARBOR_ROSY_ACCOUNT_FILE`이 없으면 지금과 똑같이 로컬 계정만 쓴다.
6. **서버 간 연결:** ADR 0015 결정 2대로 Fleet이 발급한 named service 신원이다. 사람 Rosy 세션을 서버 호출에 쓰지 않는다.

## 설정

관리자가 Rosy 연동 등록부에 Harbor client를 만든다(정확한 callback `https://<harbor>/accounts/rosy/callback/`, resource `site:<id>`, 앱 목록 주소). Harbor에는 `{issuer, client_id, resource, callback}` JSON 파일 하나를 `/config` 아래 두고 `HARBOR_ROSY_ACCOUNT_FILE`로 가리킨다. 파일이 틀리면 Harbor가 시작하지 않는다. 팀원에게 Harbor 권한을 주는 일은 Rosy 계정 화면에서 한다(팀원 기본 묶음에 `harbor.view`·`harbor.operate`·`harbor.field`, 관리자 묶음에 `harbor.admin`).

## 한계

- 접근 토큰 서명은 Harbor가 직접 검증하지 않는다. 토큰은 Harbor 서버가 허브 token endpoint에서 HTTPS로 직접 받은 것이고, 10초 introspect가 유효성을 다시 확인한다. 브라우저가 건넨 토큰은 받지 않는다.
- 로그아웃 때 허브에 닿지 않으면 회수를 다시 시도하지 않는다. 그 family는 허브 세션 만료로 끝난다.
- 같은 사람이 예전 로컬 계정과 Rosy 계정을 함께 가지면 Harbor에는 두 사용자로 보인다. 로컬 계정을 정리하는 일은 관리자가 한다.

## 검증

`harbor_web/tests/test_rosy_account.py`: capability→그룹 대응, `harbor.*` 없음 거부와 회수, 위조 state·다른 site 토큰 거부, 허브 회수 뒤 세션 종료, 허브 장애 때 만료까지만 유지, 로그아웃 회수, 설정 파일 검사, 설정이 없을 때 로컬 로그인만.

2026-11-07 교차 확인: rosy-platform `0aa9ffc8b8`의 실제 허브 라우트(`AccountAuthority`와 `/api/fleet/auth/*`)를 같은 프로세스에 띄우고, Harbor를 가짜 허브 없이 연결해 6건이 통과했다. 확인한 것은 `site:*` grant로 로그인과 역할 대응, 300초 만료 뒤 refresh 회전, 허브에서 바꾼 권한이 회전 때 반영되는 것(관리자 승격 포함), 권한이 사라졌을 때와 허브가 회수했을 때 세션 종료, `harbor.*` 없음 거부와 회수, 로그아웃 회수다. 이 시험은 ROSY 코드를 import하므로 Harbor 저장소에 넣지 않았다. 네트워크를 거친 실제 배치와 현장 사용은 아직 확인하지 않았다.
