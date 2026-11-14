# Fleet 연결 설정과 계약 확인

2026-10-26. [ADR0013](../adr/0013-independent-business-execution-and-platform.md)의 책임 분리를 적용한 설정·조회 어댑터 구현이다. 상시 업무 실행기는 [설계24](24-business-execution-boundary.md)의 별도 구현 대상이다.

## 사용자 흐름

관리자는 설정·계정에서 ROSY 연동으로 이동해 서버가 허용한 Fleet 주소와 서비스 principal ID를 저장한다. ‘Fleet 연결 확인’을 누르면 서버가 공개 API를 GET으로 조회한다. 성공/실패와 확인 시각을 저장하고, 현재 설정·토큰에 대응하는 결과만 표시한다. 페이지 GET은 네트워크나 장부 상태를 바꾸지 않는다.

토큰·장부·설치 binding은 배포 서버에서 관리한다. 브라우저·설정 DB·감사 로그에 서비스 토큰을 저장하지 않는다. 화면의 서비스 신원은 토큰의 principal ID와 정확히 일치해야 한다. 로그인 계정의 Harbor 역할과 Fleet 서비스 역할은 별개다.

## 설정 소유와 적용

| 값 | 소유·적용 |
|---|---|
| `HARBOR_FLEET_ALLOWED_ORIGINS` | 서버가 정한 UI 선택 가능 주소, 쉼표 구분. HTTPS 또는 기존 client가 허용한 loopback HTTP. 화면에서 임의 주소를 추가하지 않음 |
| `HARBOR_FLEET_ORIGIN`·`HARBOR_FLEET_PRINCIPAL` | 기존 배포 관리 방식. origin을 설정하면 이 방식이 UI 프로필보다 우선하고 화면에서 연결 신원을 바꾸지 않음 |
| FleetConnection | origin은 ROSY 계층 꼭대기 관제 하나다(ADR0018). 서버 origin이 없는 경우 Harbor DB에 주소·서비스 신원·ARM 화면 설치 선언·version 저장. 서비스 토큰 없음 |
| `HARBOR_FLEET_TOKEN` | 서버에 설치한 서비스 자격. UI 입력·응답·공개 문서에 실제 값을 넣지 않음 |
| `HARBOR_LEDGER_PATH`·`HARBOR_BINDINGS_PATH` | 기존 업무 장부와 검토된 물리 참조 대응. 경로는 서버에서 관리하고 화면에는 설정 유무만 표시 |
| ARM 화면 설치 선언 | 해당 Fleet 콘솔의 설치 확인. API 지원·승인 성공의 근거로 사용하지 않음 |

예를 들어 설치 담당자가 허용 주소에 `https://fleet.example`을 등록하고 서비스 토큰을 서버에 설치하면, 관리자가 UI에서 그 주소와 발급된 서비스 신원을 선택·저장한다. 실제 주소·토큰·운영 DB는 공개 저장소에 넣지 않는다. 파일 DB는 migration0011 적용 후 시작한다.

같은 resolver가 실행 요청 제출·원래 실행 대조·Fleet 콘솔 링크에 사용된다. UI 프로필을 저장해도 토큰이 없으면 전송하지 않는다. 설정 save는 버전을 검사하고 감사하며, 토큰이나 설정이 바뀌면 이전 검사 성공을 재사용하지 않는다. 기존 Command가 있으면 origin/principal 변경을 거절해 원래 실행 identity를 보존한다. 이력 삭제나 새 identity로의 자동 이관은 제공하지 않는다.

업무 실행 장부의 writer와 웹 DB writer는 기존 단일 프로세스/WriterLease 제약을 따른다. 설정 변경은 제출과 같은 직렬화 경계를 사용한다. 이미 생성된 Command의 원본 key·body·Fleet origin/principal을 새 설정으로 덮어쓰지 않는다. 백업은 해당 웹 DB의 프로필 신원도 manifest에 보존하고 토큰은 포함하지 않는다. 복원 후 실행 대조 차단은 유지한다.

## 운영 실행 조건 점검표 (ADR0022)

‘Fleet 연결 확인’은 세션·ARM 조회에 더해 근거·재확인 경로, 이송 제안 경로, 플랫폼 운영 프로필 신호를 없는 키 GET으로 탐침하고 조건별 결과를 점검 기록에 저장한다. 연결 설정 화면은 조건마다 충족/미충족/미확인, 점검 시각, 소유, 다음 조치를 표로 보인다. 운영 실행은 점검이 10분 안이고 모든 관문 조건이 참일 때만 열린다. 이송 제안 경로는 표시 전용이다. 사람 확인 근거 생산자는 같은 화면에서 관리자가 켜며 변경은 감사 기록에 남는다. 시뮬레이션 실행은 기존 네 조건 그대로다.

## 실제로 검사하는 계약

소비 기준은 기존 ARM API v1.312와 검증된 producer pin이다. `healthz`나 OpenAPI 앱 버전은 ARM 지원 협상이 아니다. 현재 ROSY 공유 main에 필요한 ARM 경로가 없을 수 있으므로 최신 main이라는 이유로 지원을 가정하지 않는다.

| 요청 | 검사 결과 |
|---|---|
| `GET /api/fleet/session` | principal 정확한 일치, 역할 `operator` 또는 `service`. 역할은 리소스별 proposal grant 전체를 증명하지 않음 |
| `GET /api/fleet/arm-transfers/jobs?request_key=harbor-probe-…` | 새로운 임의 key로 읽기만 수행. 구조화된404 `ARM_TRANSFER_JOB_NOT_FOUND`가 설치된 조회 surface의 응답임을 확인 |
| ARM503 `ARM_TRANSFER_JOB_UNAVAILABLE` | ARM 설치 미준비 |
| 일반404·잘못된 schema·예상하지 못한200 | 호환 조회 계약 미확인. 일반 서버의404를 지원으로 오인하지 않음 |
| 인증/권한 실패·timeout | 해당 실패로 기록. 원본 오류 본문이나 자격을 사용자 화면에 반사하지 않음 |

연결 확인은 POST 사용자 조작이지만 Fleet에는 GET만 보낸다. 작업 생성·승인·취소·owner preflight를 보내지 않는다. 서비스 신원과 ARM 읽기 API가 확인돼도 proposal 권한, 설치 binding, 자원 가용성, 물리 관측·업무 완료 producer, 실물 수용이 준비됐다는 뜻은 아니다. 성공 표시는 ‘조회 연동 확인’이다.

## 실행과 업무의 경계

### 막힌 단계와 다음 조치를 먼저 확인한다

연동 화면은 현재 설정의 마지막 검사 결과를 연결 설정·서비스 신원·작업 조회 API로 구분한다. 신원 확인 후 ARM503이 발생하면 신원은 확인됨으로 유지하고 API는 설치 필요로 표시한다. ARM 조회 중 통신이 끊기면 API는 조회 실패다. 실행 인계 설정의 존재와 조회 검증·실제 실행 수용은 각각 구별한다.

DNS 해석 실패·인증서 검증 실패·기타 TLS 실패·응답 시간 초과는 실제 예외 형식이 확인될 때만 별도 원인으로 기록한다. 원인이 문자열 등으로만 제공되면 일반 연결 실패로 유지한다. 원본 오류·토큰·서버 내부 정보는 표시하거나 감사 로그에 반사하지 않는다. 이 진단은 DNS 설정, 인증서 검증 방식이나 API 권한을 바꾸지 않는다.

각 원인에 담당 역할과 다음 조치를 표시한다. 모바일에서도 검사 결과와 조치를 설정 폼보다 먼저 읽는다. 주소·신원·토큰이나 허용 주소가 빠지면 검사 버튼을 비활성화하고 설정 위치로 안내한다. 서버의 역할·CSRF·버전·허용 주소·fingerprint 검사는 그대로 적용한다. GET 조회는 저장된 결과만 읽으며 설정·토큰 변경 후 이전 결과는 재사용하지 않는다.

이 연결은 기존 plan 확정→Command→ROSY 제출/조회 어댑터의 설정을 일치시킨다. 실제 제출은 기존 관측·revision·설치·권한 검사에 따른다. 승인·claim·장치 제어는 ROSY가 담당한다. 다칸 binding·인증된 업무 근거가 없는 실행을 이 설정 화면에서 활성화하지 않는다. 연속 업무 운용과 confirm/finish 근거 연결도 이번 연결 확인과 구별한다.

실행 모니터 화면의 대조는 원래 요청 key·mission·본문·신원을 검증한다. receipt는 물리 근거이고 전체 업무 완료와 다르다. 현재 상태와 부족한 조건은 원래 업무 화면에서 확인한다. LLM이나 UI의 다른 진입점도 동일한 설정·권한·명령 경계를 사용해야 한다.

## 같은 LAN 자동 발견 (2026-10-29)

ROSY의 [현장 발견 계약](https://github.com/robotics-team-1213/rosy-platform/blob/main/docs/reference/site-lan-discovery-profile.md)을 소비한다. Fleet은 `_rosy-fleet._tcp.local.`을 광고하고 Harbor가 공개 HTTPS API로 요청·결과 조회를 한다. 현재 결과 전달은 polling이므로 Fleet이 Harbor 주소를 찾거나 callback 서버에 연결할 필요는 없다.

설치 담당자는 사이트 이름·포트와 사이트 CA를 별도 신뢰 경로로 제공한다. Harbor의 `HARBOR_FLEET_ALLOWED_ORIGINS`에 승인한 `https://site-pc.local:8443` 형태의 이름을 등록하고 `HARBOR_FLEET_CA_FILE`에 읽을 수 있는 PEM CA 파일 경로를 설정한다. 이 예시는 설치 형식이며 실제 사이트 정보가 아니다. 배포가 연결을 고정하면 기존 `HARBOR_FLEET_ORIGIN`·`HARBOR_FLEET_PRINCIPAL`을 쓴다. [설정 예시](../../deploy/web/site-link.env.example)를 설치 환경에서 채우며 서비스 토큰은 비밀 관리 경로로 별도 공급한다. `pip install '.[web]'`에 DNS-SD 구현인 python-zeroconf가 포함된다.

관리자의 **Fleet 자동 찾기** POST는 LAN을 검색하고 승인 목록에 있는 사이트의 이름·포트·CA·정상 health만 확인한다. 미승인 광고는 인증 요청·토큰 전송 대상이 되지 않는다. 검색은 프로필·Command를 만들거나 사이트를 자동 선택하지 않는다. 관리자는 기존 폼에서 사이트와 서비스 신원을 저장하고 **Fleet 연결 확인**으로 service principal/ARM 조회 API를 별도 확인한다. 광고의 이름은 설치자가 제공한 예상 사이트 이름을 대신하지 않는다.

같은 POST는 사이트 health 검증 후 자격 없이 `/openapi.json`을 최대3MiB까지 읽어 ARM 작업 루트의 GET/POST가 공개됐는지도 표시한다. 공개 경로 있음·문서 기준 미공개·확인 전을 구분하며, schema 비공개/오류/잘린 응답 때문에 이미 찾은 사이트를 지우지 않는다. 이 표시는 서비스 권한·설치·실행 준비를 증명하거나 인증 검사를 대체하지 않는다. 마지막 검색 결과와 시각은 설정·CA 내용·승인 사이트 목록에 결속해 저장하고 GET에서는 저장된 관측만 표시한다. 설치값이 바뀌면 과거 결과를 재사용하지 않는다.

CA 파일 없음·읽기 실패·잘못된 PEM은 LAN 검색 전에 중단한다. Fleet 상태 요청 실패는 bearer 인증 실패와 구별하고, 잘린 HTTP 응답도 정상 오류 경로로 처리한다. 다른 서브넷/VPN의 TCP 연결 성공은 mDNS 전달을 보장하지 않으므로 Harbor 서버와 Fleet의 같은 LAN 연결 또는 별도 승인된 망 밖 경로를 확인한다.

CA가 설정된 HTTPS `.local` origin은 연결 검사와 실행 Registry, 기존 조회 preview 모두 HTTP 요청마다 새로 DNS-SD를 조회한다. 저장된 origin·Host·TLS 이름·요청 키는 유지하고 socket IP만 발견 결과를 쓴다. 인증서 검증과 무자격 `/healthz` 확인을 통과한 주소로만 bearer를 보낸다. 3초 검색과 기존 요청 timeout이 별도로 적용되므로 여러 API를 조회하는 조작에는 그만큼 시간이 추가된다. 기본 브라우저 DNS가 `.local`을 해석할 수 있어야 콘솔 링크도 열린다. 이 기능은 Harbor 서버의 발견·HTTPS 전송을 제공하며 브라우저 DNS 설정을 변경하지 않는다.

중복 TXT·다른 역할/계약·잘못된 주소는 버린다. 같은 예상 이름의 서로 다른 IP/포트, 승인 포트 불일치, 해석되지 않은 서비스와 64개 서비스 상한 초과는 후보 하나를 임의 선택하지 않고 실패한다. 사이트가 없어지거나 TLS/health가 실패하면 다른 사이트나 수동 DNS로 조용히 전환하지 않는다. POST는 자동 재전송하지 않으며 기존 request key로 원본 대조를 유지한다. IPv4 LAN만 지원한다.

운영 전 Fleet의 기존 `rosy-fleet-advertise.service`, TLS proxy와 인증서 SAN·사이트 CA를 확인하고 두 서버가 같은 링크에서 UDP 5353을 주고받을 수 있어야 한다. VLAN·격리 Wi-Fi에서 mDNS가 차단되면 설치자가 기존 수동 origin/정상 DNS 경로를 별도로 선택한다. 원래 IP origin에 이미 Command가 있다면 DB를 이름 origin으로 덮어쓰지 않는다. 원래 실행의 대조와 복구를 마친 뒤 기존 이관 절차를 따른다. CA/토큰/신뢰 설정 교체 후 서버를 재시작하고 연결을 다시 확인한다.

실패 화면은 사이트 미발견·광고 충돌·포트 차이·검색기 불가·TLS 불신·상태 미확인을 구분한다. 사이트 CA가 없으면 설치를 안내한다. 검색 결과는 그 검색 시점의 관측이며 실행 준비나 물리 수용을 뜻하지 않는다. CA 파일 내용이 바뀌어도 기존 조회 검사 성공을 재사용하지 않는다. [검증 기록](../validation/2026-10-29-fleet-lan-discovery.md)에서 로컬 DNS-SD/TLS·브라우저와 실제 사이트 수용을 구분한다.

## 검증

프로필 버전 충돌, allowlist 거절, 배포 설정 우선, 기존 Command identity 변경 거절, 현재 설정/토큰과 검사 fingerprint 결속, GET 무통신·무쓰기, 역할·CSRF·비밀 비노출을 Django 시험으로 확인한다. 격리 HTTP double+실제 브라우저 시험과 고정된 실제 Fleet HTTP 시험은 별도 근거로 기록한다. 둘 다 합성 장치로 실행하면 실물 수용은 아니다.
