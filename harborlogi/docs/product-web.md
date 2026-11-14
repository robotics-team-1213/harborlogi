# Harbor 제품 웹 실행과 운영 경계

Fleet의 UI 연결 프로필·서버 토큰·명시적 신원/ARM 조회 검사는 [연결 설정](design/25-fleet-connection-bridge.md)을 따른다. schema migration0011 적용 후 서버를 시작한다. 기존 배포 origin은 우선하며 UI에서 바꿀 수 없다. 운영 Fleet 주소·서비스 principal은 해당 설치 담당자가 제공해야 한다.

같은 LAN에서는 **Fleet 자동 찾기**로 승인된 사이트를 확인한다. 설치된 `.local` 사이트 이름과 `HARBOR_FLEET_CA_FILE`의 CA를 사용하면 이후 IP가 바뀌어도 같은 사이트를 다시 찾는다. [설치 예시](../deploy/web/site-link.env.example)와 [발견·재접속 규칙](design/25-fleet-connection-bridge.md#같은-lan-자동-발견-2026-10-29)을 따른다. 최초 사이트 확인과 서비스 자격 설치는 필요하며 다른 사이트로 자동 이관하지 않는다.

현재 페이지별 업무 목적과 다음 조치는 [운영 흐름](design/17-operator-page-flow.md), 실제 ROSY 호출과 승인 화면 배포 기준은 [파이프라인](design/18-harbor-rosy-pipeline.md)을 따른다. 웹에서는 `/workflow/`로 역할별 안내, 관리자는 `/settings/integration/`로 설정·수용 조건을 확인한다. 실행 메뉴 이름은 ‘실행·대조’다.

새 Fleet ARM 화면을 포함한 배포에서만 `HARBOR_FLEET_ARM_CONSOLE_ENABLED=1`을 설정한다(기본 미선언). 이 값은 설치 선언이며 API 호환성·승인·물리/업무 수용의 증거가 아니다. 미선언 상태에서는 작업 ID와 일반 Fleet 콘솔로 인계하고 지원되지 않는 승인 바로가기를 약속하지 않는다.

상태: 지원된 웹 범위 구현·검증 완료. 운영 계약·실물 수용 게이트는 별도다. 기존 조회 preview와 달리 계정/역할과 영속 업무 입력을 가진 앱이다. [ADR0007](adr/0007-authenticated-harbor-web.md), [화면·동작 계약](design/13-product-web-surface.md), [완성 계획](design/12-web-product-completion.md)을 따른다. 실물 장치 수용/외부 게시를 의미하지 않는다.

## 로컬 화면 검토

```powershell
cd harborlogi
python -m venv runs/web-venv
runs/web-venv/Scripts/python.exe -m pip install -e '.[web]'
runs/web-venv/Scripts/python.exe tools/product_preview.py
```

같은 PC에서 `http://127.0.0.1:8060/`을 연다. 최초 실행이 만든 `runs/product-preview/access.json`의 username/password로 로그인한다. 이 파일과 DB는 비공개 로컬 자료이며 커밋/공유하지 않는다. preview 계정은 **화면 검토를 위해 네 역할을 명시적으로 함께 부여한 개발 계정**이다. 운영 계정으로 재사용하지 않는다. Windows에서는 파일 ACL도 로컬 사용자에게 제한한다.

이 도구는 별도 개발 DB에 합성 화물·관측·계획·현장 배정을 만들고 실행한다. 상속된 Fleet token/원장 설정은 제거하며 실제 ROSY로 제출하지 않는다. 시연 관측 신선도 30분은 합성 화면 검토 설정으로 운영 기준이 아니다. 다시 실행하면 새 합성 관측 revision과 계획을 추가하고 이력은 보존한다. 기존 `python -m harbor.web --demo --port 8050`은 표준 라이브러리 조회 preview로 남는다.

## 직접 초기화와 계정 발급

환경 변수 `HARBOR_WEB_MODE`를 `development` 또는 `live`, `HARBOR_WEB_SECRET_KEY`를 비공개 난수 50자 이상으로 설정한다. live는 절대 경로 `HARBOR_WEB_DATABASE`와 실제 호스트만 담은 `HARBOR_WEB_ALLOWED_HOSTS`가 필요하다. 개발/시험/운영 DB를 바꿔 사용하지 않는다. 기존 장부나 타 Django DB를 웹 DB로 지정하면 거절한다.

```powershell
runs/web-venv/Scripts/python.exe -m harbor_web.manage migrate
runs/web-venv/Scripts/python.exe -m harbor_web.manage bootstrap_roles
runs/web-venv/Scripts/python.exe -m harbor_web.manage createsuperuser
```

관리자는 자기 계정에 명시적 admin membership을 초기 발급한다. 대화형 관리 shell에서 `User.objects.get(username=...).groups.add(Group.objects.get(name='admin'))`를 사용하고 비밀번호는 명령줄 인자로 전달하지 않는다. 이후 `/admin/auth/user/`에서 계정을 발급하고 필요한 역할만 부여한다. admin은 active staff와 Django 계정 관리 권한도 필요하며 bootstrap은 admin group에 해당 기본 권한을 부여한다. operator·field는 별도로 부여해야 한다.

비밀번호 변경은 본인 계정 링크, 초기화는 계정 관리자의 Django 비밀번호 변경 절차를 사용한다. 비활성·역할 변경은 다음 요청에 적용된다. 로그인 실패는 영속 제한을 적용한다. 초기 관리자 비밀번호·고정 운영 seed 계정은 소스에 없다.

## ROSY 연결

서버만 `HARBOR_LEDGER_PATH`, `HARBOR_FLEET_ORIGIN`, `HARBOR_FLEET_TOKEN`, `HARBOR_FLEET_PRINCIPAL`, `HARBOR_BINDINGS_PATH`를 갖는다. binding 파일은 설치된 `ArmTransferBinding` 목록이다. 브라우저 입력으로 origin·신원·binding을 바꾸지 않는다. 일부만 설정한 실행 profile은 시작을 거절한다. 장부와 웹 DB는 다르며 단일 서버가 각각 writer lease를 소유한다.

현재 운영 인증 관측/업무 근거 producer가 없어 live 제출은 차단된다. 분리된 test PC의 loopback simulation만 `HARBOR_WEB_ALLOW_SIMULATION=1`과 SYNTHETIC 자료로 단일 ARM 제안/조회/receipt 경로를 시험한다. 자동 admit/rearm·관측 생성·장치 명령은 앱에 없다. TRIP는 Fleet named operator 실행으로 인계하며 Harbor 역할로 대리하지 않는다.

## 배포·백업·검증

로컬 합성 미리보기도 Waitress 프로세스는 소스 변경을 자동 재적재하지 않는다. 템플릿 캐시가 남아 새 CSS와 이전 HTML이 섞여 보일 수 있으므로 화면 변경 후에는 해당 미리보기 owner만 확인해 종료하고 다시 시작한 뒤 캡처한다. `tools/capture_operations.py`는 현재 운영 요약의 필수 항목이 실제 DOM에 있는지 확인하여 이전 템플릿의 캡처를 거절한다. 이 절차는 로컬 합성 preview용이며 실행 중 운영 작업을 임의로 종료하는 절차가 아니다.

실행은 `python -m harbor_web --port 8060`이다. live에서 Waitress 한 프로세스만 실행하고, [nginx 예시](../deploy/web/nginx.conf.example)처럼 TLS proxy 뒤 loopback에 둔다. proxy 신뢰는 명시적 `HARBOR_WEB_TRUSTED_PROXY=1`일 때만 연다. 예시 인증서 경로와 호스트를 실제 설치값으로 교체하고 브라우저 인증/CSRF/TLS를 현장에서 검증해야 한다. 운영 설치 위치(R과 같은 운영 PC)와 Compose 절차는 [ADR0021](adr/0021-harbor-runs-beside-integrated-control-r.md)과 [Compose 설치 안내](../deploy/compose/README.md)를 따른다.

writer를 종료한 뒤 `python -m harbor_web.manage backup_product --output <새 절대 폴더>`로 짝 백업한다. 복원은 `restore_product --backup <백업 폴더> --web-database <새 절대 파일> --ledger-database <새 절대 파일>`을 사용한다. 기존 파일을 덮어쓰지 않으며 hash/환경/짝을 확인한다. 복원 뒤 새 제출은 차단된다. 원래 실행 key 대조와 운영 복구 검토 전 장부/상태를 지워 새 요청으로 재시도하지 않는다. 두 파일의 게시 전체는 OS 원자 연산이 아니므로 중단 시 불완전한 새 대상 세트가 있을 수 있고 보호된 복원 절차로 확인해야 한다.

```powershell
python -m unittest discover -s tests -v
runs/web-venv/Scripts/python.exe -m harbor_web.manage test harbor_web.tests --noinput
runs/web-venv/Scripts/python.exe tools/integration/product_web_browser_check.py --output runs/product-browser-review
```

마지막 두 명령에도 시험 환경 MODE/secret 설정이 필요하다(browser 도구는 자체 격리 설정). browser는 Playwright와 설치된 Edge가 필요하며 누락 시 실패한다. 기본 unittest의 선택 통합 skip은 실제 연결 통과가 아니다. 실제 Fleet HTTP는 소유 저장소 정책에 따라 지정 test PC·양쪽 고정 commit으로 별도 실행한다. 판정·건수·미검증은 검증 기록에 남긴다.
