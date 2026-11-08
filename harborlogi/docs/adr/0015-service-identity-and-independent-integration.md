# ADR 0015: 독립 서비스 신원과 Harbor–ROSY 협력 계약

날짜: 2026-10-27

상태: 책임·신뢰 모델 결정 수락. Harbor 호출 전 신원 검사 구현·단독/고정 양 끝 HTTP 검증 완료. 운영 자격 발급·설치·실물 수용은 미완료다. [검증 기록](../validation/2026-10-27-service-trust-boundary.md).

## 문제

Harbor와 ROSY가 협력하려면 HTTP 연결뿐 아니라 누가 요청했는지, 어느 데이터와 판단을 신뢰하는지, 실패 시 누가 복구하는지를 정해야 한다. 계정 DB·쿠키·비밀번호·내부 모듈을 공유하거나 앱의 업무 역할을 플랫폼 실행 권한으로 옮기면 책임이 결합된다. 반대로 한 번 연결을 확인한 뒤 실제 호출의 신원을 확인하지 않으면 다른 자격으로 원하지 않은 요청을 먼저 만들 수 있다.

## 결정

1. **사람 인증:** Harbor와 Fleet은 각자 로그인·세션·역할을 소유한다. 동일 사용자를 쓰더라도 세션/비밀번호 DB나 관리자 권한을 공유하지 않는다. 공통 IdP의 SSO를 도입하려면 issuer/audience·주체 대응·로그아웃·역할 발급을 별도 검증한다. 현재 IdP가 없는 상태에서 SSO를 구현했다고 표시하지 않는다. (2026-11-07: [ADR 0027](0027-rosy-account-sign-in.md)이 이 결정을 고친다. 사람 로그인은 Rosy 계정으로 하고, Harbor 역할 판단은 Harbor에 남는다.)
2. **서비스 인증:** Harbor 서버가 Fleet 발급 전용 named principal과 `service` 역할의 기계 자격을 사용한다. 사람 `operator` 자격을 서버에 넣어 대체하지 않는다. 원문은 비밀 관리자/배포 환경에만 보관한다. Fleet이 발급·검증·폐기하며 Harbor가 Fleet 사용자를 만들거나 암호 원장을 편집하지 않는다.
3. **매 호출 결속:** Harbor 실행 어댑터는 같은 client의 자격으로 공개 `GET /api/fleet/session`을 읽어 고정 principal과 service 역할을 확인한 뒤 요청한다. 과거 UI 검사 결과를 권한 캐시로 쓰지 않는다. 응답의 creator/key/body/mission·receipt 결속 검사도 유지한다. 사전 확인과 실제 요청은 하나의 분산 transaction이 아니다. Fleet의 요청별 인증·권한이 최종이며 자격을 다른 principal에 재할당하지 않는다.
4. **업무와 실행:** Harbor가 목표·우선순위·재취급/반출/복귀 의존 관계·확정·재계획·업무 완료를 판단한다. Fleet은 중립 작업의 접수·admission·자원·실행 수명주기를 소유한다. CORE와 장치 owner가 실제 동작·정지의 최종 권한을 유지한다. 사용자 확정 범위 내 후속 진행은 Harbor 실행기 책임이며 플랫폼에 항만 workflow를 넣지 않는다.
5. **되돌아오는 사실:** Fleet의 공개 조회·검증된 receipt를 Harbor 어댑터가 읽고 원래 의도에 결속한다. 독립 producer의 관측/퇴피/업무 근거와 함께 Harbor가 전이를 판단한다. 내부 ROSY 버스·DB나 Harbor DB를 서로 읽지 않는다. 현재 북향 통신은 공개 조회이며, 수신 callback/event가 없다고 플랫폼 내부 이벤트에 접속하지 않는다.
6. **최소 공통 의미:** 양쪽이 아는 것은 opaque request key, 명시적 object/source/target 설치 참조, 공개 상태·generation·digest·관측 시각과 오류다. Harbor 화물/계획 ID는 앱이 매핑하고 ROSY는 항만 의미를 해석하지 않는다. 물리 기하·장치 trajectory·claim은 ROSY에 남는다.
7. **승인과 근거 분리:** 서비스 신원은 제안과 자기 작업 조회/취소를 수행한다. Fleet이 요구하는 별도 named operator 승인과 독립 expiring observer 자격을 Harbor에 전달하거나 합치지 않는다. 설치 정책 범위 내 자동 진행은 플랫폼에 수용된 정책으로만 가능하며 서비스 자격을 operator로 높여 해결하지 않는다.
8. **자격 수명주기:** 토큰 교체는 같은 principal의 자격 갱신이다. 현재 Registry는 자격 변경 후 재시작·원래 요청 대조를 요구한다. principal/Fleet 변경은 기존 실행을 보존한 별도 이관이며 원래 key를 새 서비스 scope에 재접수하지 않는다. 인증 실패·timeout은 완료·정지·해제를 뜻하지 않는다.

## 실제 지원과 한계

플랫폼은 ARM 제안에 service와 operator를 모두 지원하지만 Harbor의 서버 연결은 service만 수용한다. 현재 Fleet service 역할은 Harbor 전용 세분화 OAuth scope가 아니다. 추가 권한 제약이 필요하면 Fleet이 공개 계약으로 제공해야 하며, Harbor 설정만으로 scope가 생긴다고 주장하지 않는다.

이 결정은 기존 공개 API를 소비한다. 새 범용 execute·공유 로그인·임의 callback endpoint를 만들지 않는다. 중립 ARM 구현의 로컬 main 반영과 공개 main/운영 설치는 구분한다. 현재 ARM 설치는 simulation-only이므로 production 바인딩을 합성해 활성화하지 않는다.

## 검증

잘못된 principal·사람 역할·불완전 identity 응답에서 후속 GET/POST가 발생하지 않아야 한다. 같은 client에서도 각 호출을 다시 확인하며, 자격 변경 뒤 과거 성공을 재사용하지 않는다. Harbor 역할·CSRF와 Fleet 서비스 인증을 각각 시험한다. 원래 key·receipt 결속, 다른 서비스 거절·별도 승인·독립 관측을 고정된 양 끝 실제 Fleet HTTP에서 검증하고 장치 수용과 구분한다.

상세 계약·전환 순서는 [설계27](../design/27-independent-integration-and-service-trust.md), 지속 실행은 [설계24](../design/24-business-execution-boundary.md), 연결 화면은 [설계25](../design/25-fleet-connection-bridge.md)를 따른다. 이 ADR은 미완료 실행기·producer·운영 배포를 완료로 바꾸지 않는다.
