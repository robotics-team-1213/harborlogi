# Harbor 웹 작업 관제

2026-10-25: 기존 이미지·테마와 정식 의미별 토큰을 적용한 로컬 조회 웹 앱. 조치 필요 운영 현황, 업무 목록·개요·계획·실행/근거, 사용 안내를 제공한다. 운영 로그인·쓰기·LAN 태블릿 배포는 후속이다.

## 합성 화면

Python 3.10 이상, 추가 런타임 패키지 없이 `harborlogi/`에서 실행한다.

```bash
python -m harbor.web --demo --port 8050
```

같은 PC의 `http://127.0.0.1:8050/overview`를 연다. 기본 포트는8040이며 위 명령은8050을 명시한다. 합성 업무6개로 검색·보기·상세·탭·복귀를 확인한다. 장부 생성이나 Fleet 통신은 하지 않는다. 아이콘은 전역 메뉴에, 배너는 사용 안내에 [기존 자산](../assets/brand/README.md)을 사용한다.

화면은 `/overview`, `/work`, `/work/{key}`, `/work/{key}/plan`, `/work/{key}/execution`, `/help`다. 점·슬래시 등의 일부 기존 ID는 `/work?key=…&tab=…`로 보존한다. [페이지 구현](design/10-navigation-pages-plan.md)과 [새 검증 기록](validation/2026-10-25-navigation-pages.md)을 따른다.

demo의 `/planning`은 기존 `plan_outbound`를 호출하는 합성 재고·반출 미리보기다. C1/C2 대상과 기본/공간 부족/오래된 관측 조건을 바꾸어 계획 또는 HOLD를 검토한다. 관측과 예상 결과를 구분하며 실행·업무 장부·실제 관측을 바꾸지 않는다. 실제 모드의 `/api/planning`은 재고 관측 source가 없으므로503을 반환하고 데모로 대체하지 않는다. [제품 기능과 검증](validation/2026-10-25-retrieval-planning.md)을 따른다.

## 실제 장부·ROSY 연결

`--demo` 대신 기존 장부 **절대 경로**를 `--ledger`에 전달한다. SQLite `mode=ro`로 읽으며 파일 생성·SUBMITTING 복구·마이그레이션·업무 refresh를 하지 않는다. 현재 최대 200개 업무를 지원하며 초과 시 누락 대신 조회 실패를 알린다.

서버 셸에 외부 설정으로 아래 변수를 공급한다. 실제 값은 공유 소스·명령 예시·URL·스크린샷에 넣지 않는다.

| 변수 | 의미 |
|---|---|
| `HARBOR_FLEET_ORIGIN` | Fleet HTTPS origin 또는 loopback HTTP. path/query/userinfo 금지 |
| `HARBOR_FLEET_TOKEN` | 해당 작업 조회 권한의 Bearer 자격. 서버 전용 |
| `HARBOR_FLEET_CA_FILE` | 선택: 사이트 PEM CA 파일. HTTPS `.local` origin과 함께 쓰면 DNS-SD로 IP를 다시 찾음. `.[web]` 설치 필요 |
| `HARBOR_FLEET_CONSOLE_URL` | 선택: 별도 로그인할 Fleet 콘솔 URL. HTTPS 또는 loopback HTTP, 자격·query·fragment 금지 |

접속 변수 둘 다 없으면 장부만 표시하고 실행은 연결 설정 필요로 남긴다. 하나만 있으면 시작을 거절한다. 연결 실패를 데모로 대체하지 않는다. 콘솔 주소가 없으면 mission ID를 복사해 담당자에게 인계한다.

browser의 `GET /api/work`와 `GET /api/work?key=…`에 대해 기존 업무를 읽는다. 현재 ARM mission만 Fleet `GET /api/fleet/arm-transfers/jobs/{mission_id}`로 조회하고 기존 binding 검사로 request key·물리 참조·digest·mission을 대조한다. `GOAL_CONFIRMED`일 때만 physical-receipt를 GET하고 scope·policy·epoch/generation을 검사한다. 조회 중 업무 버전·단계·mission이 바뀌면 다시 열기를 요구한다.

`ROSY 상태 다시 조회`는 GET이며 업무 `refresh()` 명령이 아니다. receipt·Action·attempt와 업무 완료를 구별한다. Cell 호환 작업은 목록·계획만 표시하고 실행은 기존 도구에서 대조한다. mission이 없는 최종 단계는 새 제안 전 상태와 구분한다.

## 경계와 검증

- 검색·필터·상세·재조회·ID 복사·설정된 Fleet 콘솔 열기를 제공한다. 임의 deep link·제안·승인·TRIP·관측·정지·재무장 쓰기는 없다.
- 원본 발생 시각은 미확인으로 남기고 화면 조회 시각과 구분한다. 조회 실패를 빈 장부로 표시하지 않는다.
- browser에는 표시 필드만 전달한다. token·원본 응답·서버 예외를 노출하지 않는다.
- 개발 WSGI 서버는 `127.0.0.1` 바인딩과 Host·원격 주소 검사를 한다. CORS·캐시·iframe을 막고 업무 query 접근 로그를 남기지 않는다.
- **운영 인증을 대신하지 않는다.** 프록시로 외부 공개하거나 같은 PC의 사용자 격리를 주장하지 않는다. 실제 태블릿 연결은 세션·scope·TLS·감사·배포 경계 후 개방한다.

```bash
python -m unittest discover -s tests -v
```

`test_web.py`는 파일 불변성·GET 전용·불일치·오류 비밀값 비노출·동시 변경·로컬 경계를 검사하고 합성 Fleet까지 실제 HTTP 소켓으로 연결한다. 전체 시험의 실제 Fleet suite skip은 연동 통과가 아니다.

Playwright Python 패키지와 Edge가 있는 환경에서는 위8050 데모 서버를 실행한 뒤 `python tools/integration/web_browser_check.py --output runs/navigation-review`로 폭1440·1280·768·390px의 탐색·탭·직접 URL·복귀·검색·drawer·긴/특수 ID·오류를 검사한다. 다른 포트는 도구의 `--url`로 지정한다. 캡처는 Git 제외 경로 `runs/`에 둔다. 실제 태블릿·사용자 수용과 구분한다.

기존 `tools.integration.real_fleet_http --arm-only`에도 웹 HTTP projection 검사를 추가했다. 고정 소스와 팀 시험 PC에서 명시 실행해야 새 구현의 실제 ROSY 근거가 된다. [검증 기록](validation/2026-10-25-web-workbench.md)에서 결과를 구분한다. 후속 업무 쓰기·Fleet 승인 화면·공급 인계 회차·업무 관측·TRIP 위임·실물은 [단계별 계획](design/04-delivery-and-acceptance.md)을 따른다.
