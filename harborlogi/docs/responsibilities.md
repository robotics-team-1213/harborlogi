# Harbor와 ROSY의 책임

어느 저장소·서비스를 고칠지 정할 때 이 표를 본다. Harbor의 항만 업무, ROSY의 관제와 장치 실행, 공통 서비스, 실행 호스트는 서로 다른 책임이다([ADR0028](adr/0028-control-units-device-owners-and-hosts.md)). 같은 PC에서 실행해도 원장·계정·명령 권한을 합치지 않는다.

## 누가 무엇을 소유하는가

| 구성 | 소유하는 것 | 다른 주체에 남는 것 |
|---|---|---|
| Harbor | 항만 업무 위치·화물·지시·계획·업무 장부·단계 의존·다음 업무 판단·완료 조건 대조 | 로봇 최종 명령, Fleet admission·장치 claim·안전 원장 |
| 통합관제 R | 자기 작업 원장·직접 소유 장치, 하위 정보·위임·취소·허용된 정지 요청 중계, 외부 앱 입구 | 하위 셀의 원장·기명 승인·재무장·최종 장치 실행 |
| 셀 관제 CELL-x | 같은 물체·공간을 다루는 암 세트의 원장·admission·claim·작업 조정·정지/복구 조정 | 팔의 최종 joint/action 수락·실행·안전 검사는 OMX owner |
| Pinky CORE | 로봇 외부 API·현재 상태·안전 재검사·최종 Base 명령 | 항만 업무 완료·하위 셀 승인·팔 관절 명령 |
| OMX owner | 팔별 joint/action 실행·상태·안전·정지·복구 | Harbor 업무 순서·완료 판정, Fleet의 셀 원장 |
| World | 관측·odom의 현재 상태·자세 투영과 출처·신선도 | 미래 계획·작업 원장·최종 장치 명령 |
| Decision | 규칙·정책·판단 후보. 채택·admission은 기존 Fleet 경계에서 처리 | 독립 장치 명령이나 Harbor 업무 원장 쓰기 |
| Vision | 허용된 영상 수신·마커/객체 관측과 보정 근거 | 업무 완료 판정·작업 승인·로봇 최종 명령 |
| Account | 공통 사람 신원·로그인·권한·세션·회수 | 기계 자격·장치 admission·앱별 업무 허용 판단 |
| Monitoring | 상태 수집·집계·읽기 API | 설정 변경·서비스 재시작·장치 복구·명령 |
| Media | 영상 세션 권한·시청 요청·미디어 프로세스 관리 | 업무 원장·장치 조종 권한. 영상 변환·전달 비용은 실제 구성으로 검증 |
| Learning / 모델 PC | 자료·사람 정답·학습·독립 평가·승격 증거 | 현장 admission·운영 모델 활성화·장치 명령의 기존 owner |
| AI PC | 승인된 고정 버전 추론·시각과 출처가 붙은 사실·후보 | Fleet 원장·승인·장치 실행 |
| Robot / Pilot / Cam / Games | 각각 한 대 관리 / 직접 운전 요청·운전석 / 영상 캡처·송신 / 경기 업무 | 화면이 원장·최종 장치 명령 owner를 대신하지 않음. 기존 대상별 권한·수용 범위 유지 |
| 물리 호스트·운영 브라우저 | 호스트는 프로세스 실행, 브라우저는 허용된 화면 조회·요청 | PC 관리자 권한·화면 접속이 다른 관제의 승인·장치 실행 권한을 만들지 않음 |

`owner`는 해당 자원·상태·명령을 책임지는 소프트웨어 주체다. “owner” 공용 로그인 계정이나 모든 장치를 직접 제어하는 중앙 관리자를 뜻하지 않는다. 각 앱을 합친 하나의 DB나 모든 상태의 단일 writer도 만들지 않는다.

## 연결과 승인

1. Harbor 사용자가 업무 계획을 확정하면 Harbor가 안정된 요청 key와 업무 기록을 보존한다.
2. Harbor 서버는 전용 service 신원으로 R 하나의 공개 Fleet HTTP 계약을 소비한다. 플랫폼 Python·DB·ROS를 import하거나 직접 접근하지 않는다.
3. R이 직접 소유한 Pinky의 이송은 R의 기존 경로를, 하위 셀 소유 workcell의 ARM 제안은 셀 위임 경로를 쓴다. 셀 선택은 플랫폼 manifest가 결정하며 Harbor가 USB 경로·모터·센서 역할을 정하지 않는다.
4. 주행 위임의 `/approve`와 셀 ARM job의 `/admit`은 다르다. 실물 팔은 셀의 기명 승인·현재 부모 증명·기존 자원·안전 검사를 거친다. simulation 정책 admission을 실물 자동 승인으로 확대하지 않는다.
5. R의 ARM physical-evidence·recheck 중계는 현재 직계 자식까지만 지원한다. 더 깊은 셀의 연결은 계약과 구현·시험을 먼저 보강하며 앱이 셀에 직접 붙어 우회하지 않는다.
6. Harbor는 검증된 실행·관측 근거를 업무 완료 조건과 대조한다. HTTP 접수·Action 성공·물리 receipt 하나로 반출대 내려놓기·재취급 화물 복귀·관련 팔 퇴피가 모두 완료됐다고 만들지 않는다. 불명·오래된 근거·미해결 점유는 기존 HOLD·복구 규칙을 따른다.

경로·입력·응답·소비 버전의 정본은 [플랫폼 연결 기준](platform-contract.md)이다. 이 책임표는 API나 admission 정책을 추가하지 않는다.

## 계정 연동의 원칙과 현재 상태

사람 신원은 공통 Account로 연동하고 Harbor는 자기 업무 역할·감사를 유지하는 방향이다. 사람 로그인, Harbor 서비스 principal, 셀 기명 승인, 독립 관측 producer 자격은 구별한다. 사람 비밀번호·operator 토큰·쿠키나 계정 DB를 앱끼리 복제해 연결하지 않는다.

2026-11-08 확인: Rosy Account 로그인 수신기와 ADR0027은 [팀 PR #46](https://github.com/robotics-team-1213/harborlogi/pull/46)에서 검토 중이며 팀 main에 병합되지 않았다. 현재 팀 main의 Django 로그인과 목표 공통 Account 연동을 구별한다. 이 문서 갱신은 로그인 연동 구현·설치·운영 수용 완료를 뜻하지 않는다.

## N100 목표와 실제 배치

N100·16GB / Ubuntu Server 24.04 LTS amd64는 통합관제 R의 목표 호스트다(2026-11-08 사용자 결정). Harbor의 모델 PC 임시 배치와 R 동거 방향은 [ADR0021의 2026-11-06 개정](adr/0021-harbor-runs-beside-integrated-control-r.md)을 따른다. 이번 기록으로 Harbor·셀·장치 서비스를 옮기지 않는다.

동거할 때도 Harbor의 Compose project·DB·TLS·서비스 자격을 구별한다. R은 ROSY의 기존 서명 site candidate, Harbor는 팀 CI가 만든 이미지 digest와 기존 갱신·백업·복원 절차를 따른다. 한쪽 이미지를 갱신했다고 다른 쪽의 배포·계약 수용이 확인되지는 않는다. 역할 격리는 같은 전원·OS의 장애 격리도 아니다.

N100 설치 전에 실제 Fleet·Account·Monitoring·Harbor·영상·DB 서비스 집합과 동시 사용자/장치 수를 정하고 부하·발열·디스크 증가·관측 신선도·백업/복원·단절 복구를 확인한다. 기존 site 묶음 전체가 가볍다고 가정하지 않는다. R 단절 중 셀·장치의 기존 정지·복구 경계를 유지하며 재접속이나 DB 복원으로 자동 재실행·정지 해제·재무장하지 않는다.

ROSY 쪽 실제 관제 배치의 정본은 [관제 단위 배포 안내](https://github.com/robotics-team-1213/rosy-platform/blob/main/docs/deployment/control-units.md), 공통 서비스의 소유·분류는 [공통 서비스 카탈로그](https://github.com/robotics-team-1213/rosy-platform/blob/main/docs/reference/common-services.md)다. 플랫폼의 N100·책임 정리 ADR D-994·D-995는 별도 게시 절차 중이므로 공개 반영과 실물 수용을 확인해서 사용한다.
