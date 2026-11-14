# Harbor 운영 설치 (Docker Compose)

Harbor는 OMX 관제 PC(팔 셀 호스트)에서 돌리지 않는다. 2026-11-12부터 통합관제 R 호스트에서 R 옆에 둔다(ROSY 운영 기록 기준, 2026-11-14 점검으로 확인. 그 전에는 모델 PC에 임시로 있었다). 이미지는 CI가 만들고 PC는 받아서 실행만 한다. 결정과 아직 수용되지 않은 조건은 [ADR 0021](../../docs/adr/0021-harbor-runs-beside-integrated-control-r.md)에 있다.

이 폴더의 구성은 R·CELL-A와 섞이지 않는다. Compose project 이름(`harbor`), 공개 포트, 데이터 volume(`harbor_harbor-data`), TLS 인증서, R이 발급한 서비스 principal을 각자 따로 둔다.

| 서비스 | 하는 일 |
|---|---|
| `harbor` | Waitress 한 프로세스. 웹과 상시 실행기가 함께 돌고, 컨테이너 안 `127.0.0.1:8060`에서만 듣는다 |
| `proxy` | nginx. `harbor`의 네트워크를 함께 써서 TLS를 끝내고 loopback으로 넘긴다. 규칙은 [nginx 예시](../web/nginx.conf.example)와 같다 |
| `manage` | 일회성 관리 실행(migrate·계정·백업·복원). 실행기를 켜지 않는다 |

## 처음 설치

R 호스트(R과 같은 호스트)에서 이 저장소의 고정 커밋을 체크아웃한 뒤 실행한다. 비밀 값은 저장소 밖에 둔다.

1. `harbor.env.example`을 저장소 밖(예: `~/.config/harbor/harbor.env`, 권한 600)에 복사하고 채운다. R origin·principal·token은 R 설치 담당자에게 받는다.
2. 저장소 밖 폴더 두 개를 준비한다.
   - 설정 폴더: 설치 binding `bindings.json`을 넣는다. R 인증서가 사설 CA이면 `r-ca.pem`도 넣는다.
   - TLS 폴더: Harbor 자기 인증서 `fullchain.pem`과 `private-key.pem`을 넣는다. R 인증서를 빌려 쓰지 않는다. 운영자 브라우저가 신뢰하는 인증서여야 한다([연결 규약](../../docs/platform-contract.md#연결-규약) 15). 호스트가 tailnet 이름으로 열려 있으면 `tailscale cert`로 그 이름의 공인 인증서를 이 폴더에 받고, 매일 갱신을 확인한 뒤 `nginx -s reload` 하는 timer를 둔다.
3. 환경 변수를 지정한다: `HARBOR_ENV_FILE`, `HARBOR_CONFIG_DIR`, `HARBOR_TLS_DIR`, `HARBOR_HOSTNAME`, `HARBOR_BACKUP_DIR`, 그리고 R·CELL-A와 겹치지 않는 `HARBOR_HTTPS_PORT`(기본 9443. R 호스트에서 443·8443은 rosy-site가 쓴다. 옛 robttt 설치 때는 8543도 CELL-A가 썼다. 설치 전에 `ss -ltn`으로 다시 확인한다).
4. 다음 순서로 실행한다.

```bash
cd harborlogi/deploy/compose
docker compose build
docker compose --profile setup run --rm manage                      # migrate
docker compose --profile setup run --rm manage python -m harbor_web.manage bootstrap_roles
docker compose --profile setup run --rm manage python -m harbor_web.manage createsuperuser
docker compose up -d harbor proxy
```

migrate를 하지 않으면 `harbor`가 시작을 거절한다. R principal·token·binding 중 하나라도 비어 있으면 Fleet 실행 연결 없이 화면만 뜬다.

### R과 같은 호스트에서의 덮어쓰기

ROSY 사이트 방화벽(`ROSY-SITE-INGRESS`)은 브리지 인터페이스에서 호스트 자신의 주소로 들어오는 R 포트 연결을 버린다(`fib daddr type local drop`). 그래서 기본 브리지 네트워크의 Harbor는 같은 호스트의 R에 닿지 못하고 시간 초과가 난다. 방화벽은 사이트가 정한 경계이므로 열지 않는다. 대신 저장소 밖에 덮어쓰기 파일을 두고 Harbor를 호스트 네트워크로 돌린다. 이러면 R로 가는 연결이 `lo`를 지난다.

```yaml
# /etc/harbor/compose.site.yaml — docker compose ... -f compose.yaml -f /etc/harbor/compose.site.yaml
services:
  harbor:
    network_mode: host
    ports: !reset []
    extra_hosts: ["<R의 TLS 이름>:127.0.0.1"]   # mDNS 이름을 lo로 고정한다. 인증서 이름 검사는 그대로 유지된다
  proxy:
    environment:
      HARBOR_LISTEN_PORT: "9443"                 # 호스트 네트워크에서는 443이 R 것이다
```

Waitress는 그대로 `127.0.0.1:8060`에서 듣는다. 호스트의 8060 포트가 비어 있는지 먼저 확인한다.

## 갱신

PC에서 직접 빌드하지 않는다. 팀 `main`의 `test`가 통과하면 `publish-image` workflow가 이미지를 한 번 빌드해 `ghcr.io/robotics-team-1213/harborlogi:<커밋>`과 `:main`으로 올린다. Harbor PC의 `harbor-autoupdate.timer`가 5분마다 새 digest를 확인하고 다음 순서로 바꾼다.

1. `harbor`와 `proxy`를 멈춘다.
2. `/backup/auto-<UTC 시각>`에 짝 백업한다(최근 10개 유지).
3. 새 이미지로 migrate한다.
4. 새 이미지로 띄우고 health를 기다린다.
5. 성공하면 `/etc/harbor/compose.env`의 `HARBOR_IMAGE`와 `/opt/harbor/current`를 바꾼다. 이미지와 release는 현재판과 직전판만 남긴다.

실패하면 다음과 같이 처리한다.

- **DB 스키마 변경 없음:** 직전 이미지로 돌아가고, 그 digest를 `/etc/harbor/autoupdate.failed`에 적어 다시 시도하지 않는다. 더 새로운 이미지가 나오면 다시 시도한다.
- **DB 스키마 변경 있음, 또는 복귀 실패:** 멈춘 채 `/etc/harbor/autoupdate.hold`를 남긴다. 복원은 새 제출을 막아 사람의 대조가 필요하므로 자동으로 하지 않는다. 확인 후 백업에서 복원하고 이 파일을 지우면 다시 시작한다.

점검을 위해 갱신을 멈추려면 이 hold 파일을 직접 만든다. 상태는 `journalctl -u harbor-autoupdate`로 본다.

### 갱신 유닛 설치

처음 설치 때 한 번만 한다. 이후 갱신기 코드는 이미지와 함께 바뀌지만, 유닛 파일이 바뀌면 다시 복사한다.

```bash
sudo cp /opt/harbor/current/harbor-autoupdate.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now harbor-autoupdate.timer
```

`/opt/harbor/current`는 `/opt/harbor/releases/<digest 앞 12자>`를 가리키고, 그 안에 이미지의 `/app/deploy/compose`가 풀려 있어야 한다.

## Rosy 계정 로그인 연결

Rosy 계정으로 로그인하는 수신기는 팀 main에 병합되어 있다([ADR 0027](../../docs/adr/0027-rosy-account-sign-in.md)). 켜지 않으면 로컬 계정 로그인만 쓴다. 켜려면 다음을 한다.

1. Account 등록부에 Harbor client를 만든다. callback은 `https://<Harbor 주소>/accounts/rosy/callback/`와 글자 그대로 같아야 하고, resource는 `site:<id>`다. 등록은 Account 관리자가 한다.
2. 설정 폴더(`HARBOR_CONFIG_DIR`, 컨테이너 안 `/config`)에 JSON 파일을 둔다. 키는 `issuer`, `client_id`, `resource`, `callback` 네 개뿐이고 비밀 값은 넣지 않는다. 하나라도 틀리면 Harbor가 시작하지 않는다.
3. env 파일에 `HARBOR_ROSY_ACCOUNT_FILE=/config/rosy-account.json`을 적고 `harbor`를 다시 띄운다.
4. 팀원의 Harbor 권한(`harbor.*`)은 Rosy 계정 화면에서 준다.

설치 상태(2026-11-14): 아직 켜지 않았다. 준비된 것은 세 가지다. Harbor 화면 인증서를 공인 인증서로 바꿨고(매일 갱신 확인), 설정 폴더에 `rosy-account.json`을 두었고, Account 설정의 client 목록에 `harbor`를 넣었다(Account가 다시 뜰 때 적용). 남은 것은 Harbor 권한(`harbor.*`)을 아는 Account 판으로 R을 갱신하는 일, 팀원에게 권한을 주는 일(4번), 3번의 env 한 줄이다. 현장 R의 Account는 아직 `harbor.*` 권한을 모르는 판이라 그 전에 3번을 하면 로그인이 거절된다. Harbor 컨테이너에서 Account issuer의 `/api/fleet/auth/keys`에는 닿는다.

## 백업

백업 폴더(`HARBOR_BACKUP_DIR`)는 컨테이너 사용자 uid 10001이 쓸 수 있어야 한다. root 소유로 두면 `backup_product`가 `Permission denied`로 실패한다(2026-11-04 robttt). 처음 한 번 `sudo chown 10001 "$HARBOR_BACKUP_DIR" && sudo chmod 750 "$HARBOR_BACKUP_DIR"`로 맞춘다. 갱신 스크립트에서 백업이 실패하면 다음 단계로 넘어가지 않게 한다(파이프로 출력을 자르면 실패가 가려진다).

writer가 멈춘 상태에서 짝 백업한다. 복원 규칙은 [제품 웹 안내](../../docs/product-web.md#배포백업검증)를 따른다.

```bash
docker compose stop harbor
docker compose --profile setup run --rm manage python -m harbor_web.manage backup_product --output /backup/$(date +%Y%m%d-%H%M)
docker compose up -d harbor proxy
```

## R과 함께 옮기기

R을 옮길 때(D-893 §6) Harbor도 같은 창에서 옮긴다.

1. 진행 중이거나 결과 불명인 인계를 원래 작업 화면에서 대조한다.
2. `harbor`를 멈추고 백업한다.
3. 새 호스트에 같은 커밋을 설치한다.
4. `restore_product`로 새 파일에 복원한다.
5. env의 R origin을 새 주소로 바꾼다.

새 호스트의 volume은 비어 있으므로 compose가 쓰는 경로로 바로 복원한다.

```bash
docker compose --profile setup run --rm manage python -m harbor_web.manage restore_product \
  --backup /backup/<백업 폴더> --web-database /data/web.sqlite3 --ledger-database /data/ledger.sqlite3
```

복원된 DB는 새 제출을 막는다. 원래 실행 key 대조를 마치기 전에는 장부나 상태를 지워 새 요청으로 재시도하지 않는다. 이전 호스트의 `harbor`는 지운 것을 확인한 뒤에야 새 호스트를 연다.
