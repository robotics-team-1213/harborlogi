# Harbor 연결 준비 진단 보강

2026-10-29. 기본 변경 `4b9d7e1` 이후의 후속 개선이다. [연결 설계](../design/25-fleet-connection-bridge.md#같은-lan-자동-발견-2026-10-29)를 따른다.

## 수정과 근거

실제 운영 조사에서 사이트의 mDNS·CA/TLS health는 정상이어도 이전 Fleet의 공개 ARM API는 없을 수 있었다. 서비스 자격이 없는 설치에서도 공개 API 문서의 광고 상태를 먼저 확인하도록 했다. 경로 있음·문서상 미공개·미확인을 구분하며 이 값은 인증된 API 검사나 실행 준비를 대신하지 않는다. 문서 오류는 사이트 발견을 취소하지 않는다.

CA 경로 없음/잘못된 PEM이 검색 전에 분류되지 않던 사례와 잘못된 origin 포트·공백 허용을 재현하고 수정했다. 실제 TLS 서버의 잘린 chunked 응답으로 선택적 API 점검의 HTTP500을 재현했다. 선택적 문서는 미확인으로, health 오류는 상태 미확인으로, API의 잘린 응답은 기존 공유 client의 검증 오류로 처리한다. POST 재전송·TLS 우회·자격 노출은 추가하지 않았다.

마지막 검색 결과는 시각과 함께 저장하고 설정·CA 파일 내용·승인 목록이 바뀌면 숨긴다. 페이지 GET은 네트워크를 조회하지 않는다. 운영자 화면은 다른 서브넷/VPN으로 TCP가 닿아도 mDNS는 전달되지 않을 수 있음을 안내한다.

## 검증

- `python -m unittest discover -s tests`: 195 수집, 194 실행 통과, 기존 실제 Fleet 통합 1 skip.
- `python -m django test harbor_web.tests --settings=harbor_web.settings --noinput`: 246 통과.
- 실제 TLS fixture: CA/Host/SNI, 자격 없는 public schema, 비공개/과대/손상 schema, 잘린 chunked 응답, health 실패와 bearer 미전송 검사.
- 실제 Edge 브라우저: 검색 결과 fixture의 시각·API 안내, 무통신 GET, 인증 검사와의 분리, 역할·자격 비노출, 1440/1280/768/390px overflow 없음. Fleet은 합성 HTTP double이며 공개 schema fixture는 저장된 관측의 렌더링 검증이다.
- 독립 검토에서 잘린 문서 응답의 미처리 예외를 지적했고 실제 소켓 재현 시험으로 수정했다. 검토자가 발견/TLS 시험15개를 별도로 실행해 통과를 확인했다.

기존 합성 Fleet 브라우저 보고서와 캡처는 Git 제외 `runs/connection-readiness-browser/run-a813f3403a3f4f25897d8775ae746eba/`에 있다. 실제 운영의 주소·자격·호스트 정보는 공개 기록에 옮기지 않았다.

## 운영 수용 범위

현장 호스트에서 기존 신뢰 키로 서명 검증한 공식 `site_users.py`와 실행 중 Fleet image의 parser를 이용해 먼저 복사본에 `service` 발급을 검증했다. 실제 반영은 기존 update-freeze 검사·작업 원장 비어 있음·registry mount/CLI 대응·원래 계정 보존·backup/secret 권한을 확인한 뒤 수행했다. 실제 Fleet session200에서 전용 신원과 service 역할을 확인했으며 ARM 조회는 일반404였다. 토큰은 현장 호스트의 root 전용 파일에만 두고 합성 preview에는 넣지 않았다. 재실행은 원래 token과 일치하는 registry 상태를 확인해 회전 없이 resume했다. 이 과정은 자격·읽기 연결 확인이며 새 Harbor 운영 서버나 물리 실행의 수용이 아니다.

운영 API는 여전히 이전 릴리스다. 최신 공개 서명 릴리스에는 필요한 API/발급 도구가 있지만, 호스트 검증 도구의 배포 파일 형식과 별도 ARM-S credential overlay가 업데이트를 막는다. 기존 ARM-S 설정을 지우거나 서명 검사를 약하게 해서 배포하지 않았다. 호환되는 서명 payload에 해당 설치를 보존하고 검증 도구·복구 조건을 맞추는 후속 배포가 필요하다. 실제 장치 동작은 수행하지 않았다.

CE: 미해결 — 클라이언트 오류 처리·공개 상태 진단·전용 서비스 자격은 검증했으나, ARM-S를 보존하는 운영 릴리스 전환과 Windows의 현장 LAN 경로는 남아 있다. 해결한 판단은 코드·회귀 시험·연결 설계에 기록했고 별도 성공 교훈은 중복 생성하지 않았다.
