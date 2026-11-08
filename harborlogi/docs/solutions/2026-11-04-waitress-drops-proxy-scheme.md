# TLS 프록시 뒤 Harbor가 HTTPS로 무한히 다시 보낸다

2026-11-04. Harbor Compose 구성(ADR 0021)을 통합관제 R이 도는 PC에서 처음 실제로 띄웠을 때 찾았다. 단위·브라우저 시험은 모두 통과하는 상태였다. 그 시험들은 TLS 프록시 없이 개발·시험 모드로 돌기 때문에 이 결함을 볼 수 없었다.

## 증상

- `live` 모드 + `HARBOR_WEB_TRUSTED_PROXY=1` + nginx TLS 프록시 뒤에서 모든 요청이 `301 → https://<host>/`로 돌아온다. 로그인 화면도, 정적 파일도 같다.
- 프록시를 `9443:443`처럼 다른 포트로 공개하면 리다이렉트 주소에서 포트가 빠진다.
- 컨테이너 healthcheck(TCP)는 `healthy`라서 상태만 봐서는 정상처럼 보인다.

## 원인

1. Waitress 3의 기본값은 `clear_untrusted_proxy_headers=True`, `trusted_proxy=None`이다. 신뢰할 프록시가 지정되지 않으면 Waitress가 `X-Forwarded-Proto`를 지운 뒤 앱을 부른다. Django의 `SECURE_PROXY_SSL_HEADER`는 이미 지워진 헤더를 읽으므로 요청을 HTTP로 보고 `SECURE_SSL_REDIRECT`가 다시 HTTPS로 보낸다. 같은 요청이 계속 반복된다.
2. 프록시가 `Host`를 포트 없이 넘긴다. 브라우저의 `Origin`에는 공개 포트가 들어가므로, 포트를 바꿔 공개하면 리다이렉트 주소와 CSRF Origin 대조가 모두 어긋난다.

`harbor_web/__main__.py`는 처음부터 `serve(application, host='127.0.0.1', ...)`만 호출했다. 따라서 기존 [nginx 예시](../../deploy/web/nginx.conf.example)를 따른 설치에도 같은 결함이 있었다. 문서는 "현장에서 검증해야 한다"고만 적었고 실제로 검증한 적이 없었다.

## 해결

- `HARBOR_WEB_TRUSTED_PROXY=1`이면 `serve_options()`가 `trusted_proxy='127.0.0.1'`과 `trusted_proxy_headers={'x-forwarded-proto'}`를 넘긴다. loopback의 프록시 하나와 그 헤더 하나만 믿는다. 다른 주소에서 온 같은 헤더는 계속 지워진다.
- Compose 프록시는 `Host`를 `${HARBOR_HOSTNAME}:${HARBOR_HTTPS_PORT}`로 고정해 보낸다. 클라이언트가 보낸 Host는 그대로 쓰지 않는다.

## 검증

- `harbor_web/tests/test_web_entry.py`는 Waitress 자신의 `proxy_headers_middleware`와 기본값으로 판정한다. 수정 전 옵션에서는 실패하고 수정 후에는 통과하는 것을 확인했다. 다른 주소에서 온 헤더가 지워지는 것도 확인한다.
- R 호스트에서 격리 Compose 프로젝트(포트 19443, 버리는 자료)로 실제 요청을 보냈다. 결과는 `/` → `302 /accounts/login/?next=/`(포트 유지), 로그인 200, CSS 200, Origin을 실은 로그인 POST 200(CSRF 통과)이다. 확인 뒤 컨테이너·볼륨·이미지·비밀값을 지웠다.

## 다음에 적용할 조건

- 웹 서버, 프록시, Waitress 버전, 공개 포트를 바꿀 때는 healthcheck가 아니라 **프록시를 거친 실제 HTTPS 요청**으로 확인한다. 최소 범위는 `/`의 리다이렉트 대상, 로그인 GET, 정적 파일 하나, Origin을 실은 POST 하나다.
- Waitress를 직접 부르는 다른 진입점(preview 등)을 프록시 뒤에 둘 때는 `serve_options()`를 재사용한다.
- `ssh host 'bash -s' < script`로 원격 점검을 돌릴 때 `docker compose run`은 stdin을 읽어 남은 스크립트를 먹는다. 스크립트를 파일로 올려 실행하거나, `run`에 `< /dev/null`을 붙인다.
