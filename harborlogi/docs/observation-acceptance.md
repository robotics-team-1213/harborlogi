# 물리 관측 연동의 수용 조건

2026-10-29 개발 상태: D-915/API v1.330의 불변 원문 proof를 소비하는 `PhysicalEvidenceAdapter`와 선택형 executor provider를 구현했다([설정과 제한](execution.md#원본-물리-근거-provider-d-915)). 원문 hash·측정 시각·A/B owner 대응·관련 팔 coverage를 검사한다. 실제 HTTP 검증, 운영 관측 생산자, Gazebo 원본 시계 결속과 실물 수용은 각각 별도 근거가 필요하다. 아래의 이전 미구현 서술은 당시 조사 이력이며, 이 구현으로 운영 수용이 끝났다는 뜻은 아니다.

2026-10-25 재대조: 중립 ARM 시뮬레이션 API에는 독립 observer의 실행 전후 관측과 `physical-receipt`가 이미 구현돼 있다. 아래 구버전 비교표는 조사 이력이며, 현행 호출과 Fleet 승인 UI는 [파이프라인 설계](design/18-harbor-rosy-pipeline.md)를 따른다. 미완료 범위는 운영 관측·Harbor 업무용 배치/복귀/관련 팔 퇴피 어댑터·다칸 설치 대응·실물 수용이다. 물리 receipt의 존재만으로 이 범위를 충족하지 않는다.

Harbor는 실제 Fleet HTTP를 소비하지만, 물리 관측을 `MoveEvidence`(내려놓기 `PlacementEvidence` + 퇴피 `RetreatEvidence`, ADR 0005 결정 6)로 변환하는 운영 어댑터는 아직 없다. 이 문서는 그 어댑터의 입력·소유·수용 조건이며, 관측 wire schema나 장치 활성화 계약을 새로 정의하지 않는다.

## 현재 생산자와 소비자의 차이

조사 소스는 Harbor `2f365ea`(이전 비공개 저장소 `rosy-port`의 커밋), 플랫폼 `6b7dd876289ecb5ffe121eb983a90b36f15c9eba`다. 플랫폼 내부 경로는 근거이며 Harbor runtime import 대상이 아니다.

| 기존 근거 | 확인할 수 있는 것 | Harbor에서 대신 주장할 수 없는 것 |
|---|---|---|
| `core_common.observation.ObservationSnapshot`, World `api/observation.py` | 출처·원본 시각·좌표계·보정 revision·근거 참조 | 특정 화물의 도착, 팔 퇴피, 내려놓기 완료. VALID는 물리 사실의 참 판정이 아니다. |
| World `state/projections/arm_state.py:ArmState` | 관측된 관절·그리퍼 값과 rig/calibration revision | 물체 상태. 현재 `object_state`는 `unobserved`만 허용하며 그리퍼 열림만으로 퇴피를 만들 수 없다. 관절값은 `ArmRetreat`의 측정 자세 입력이다. |
| `core_common.protocol.cell_goal_evidence.CellGoalEvidence` | `sim_model_pose` 평가와 작업·시도·공정 digest·물체 pose | 운영 카메라 관측·실물 내려놓기. simulation 사실을 실물 증거로 변환하지 않는다. |
| `rosy.execution.site.goal_evidence.GoalEvidence`와 Fleet evidence service | 제한된 object/destination predicate, producer·observation·action/attempt 결속과 그리퍼 OPEN | Harbor의 팔 전체 퇴피와 최종 내려놓기. Fleet `GOAL_CONFIRMED`만으로 업무 완료를 만들지 않는다. |
| Harbor `harbor/workflow.py:BusinessLedger.refresh/_validate/confirm/finish` | 최신 Fleet mission/attempt·업무 revision·허용 출처·설정된 freshness(기본 5초)·한 번만 소비한 관측의 대조 | 관측 내용의 센서 진실성 또는 producer 인증. source_id 허용 목록만으로 인증이 성립하지 않는다. |

검토한 계약에는 Harbor 필수 물리 사실 전체를 제공하는 운영 producer와 인증된 전달 경로가 없다. 일부 필드의 이름을 바꿔 붙이는 어댑터로 이 결손을 해결할 수 없다.

## 책임과 필드 대응

| 근거 값 | 어댑터가 받아야 하는 근거·검사 | 책임 |
|---|---|---|
| 두 근거의 `evidence_id`, `source_id`, `observed_at` | 인증된 producer identity, 원본 observation ID·시각·revision/digest의 보존과 검증. 수신 시각으로 원본 시각을 대체하지 않는다. | 관측 producer·설치 통합 담당 |
| `mission_id`, `attempt_id` | Fleet의 현재 job과 해당 step의 실제 attempt. 재시도·재접수 뒤 과거 관측은 거절한다. | Fleet 실행 장부 + Harbor 대조 |
| `revision` | 승인된 Harbor 업무 계획 revision과 원본 관측 revision의 명시적 대응. 서로 다른 revision을 하나로 간주하지 않는다. | Harbor 업무 모델·어댑터 |
| `PlacementEvidence.cargo_id`, `location` | 카메라가 관측한 실제 물체 identity와 위치(top 카메라, D-797 §5 `camera_item_pose`, 미착지). 설치자가 검증한 업무 ID·공정 ID·좌표/보정 대응이 필요하다. | 관측 producer·설치 통합 담당 |
| `RetreatEvidence.arms[]` (`ArmRetreat`) | 관련 팔마다 Fleet zone claim 해제(`zone_id`, `claim_released`)와 owner 측정 관절값·측정 시각·revision, 그 자세가 구역 밖이라는 플랫폼 판정(`outside_zone`). 카메라가 필요 없다. 대기 자세 도달·action 성공·정지·그리퍼 OPEN으로 추정하지 않으며 Harbor가 FK를 계산하지 않는다. | Fleet claim 장부 + 팔 owner 측정 + 플랫폼 구역 판정(조회 계약 D-856 P1, 미착지) |
| `finish`의 `PlacementEvidence` | 반출 대상 화물이 반출대에 내려놓아졌다는 카메라 관측. 반출 대상 mission/attempt와 결속돼야 한다. 같은 호출의 퇴피 근거는 이번 요청에 쓰인 팔 전부를 덮어야 한다. | 카메라 관측 producer + Harbor finish 대조 |

Harbor는 공개 계약의 인증된 입력을 소비해야 한다. World·CORE 내부 Python/DB/ROS를 import하는 구현이나 앱이 producer credentials를 임의 생성하는 구현은 채택하지 않는다. 최종 장치 명령과 admission 소유권은 기존 owner/Fleet에 남는다.

## 운영 어댑터 구현 전 필요한 산출물

1. 설치 통합 담당이 원본 관측 transport·인증·producer 등록·시계·revision 계약을 확정한다. producer는 화물 identity/도착·관련 팔 퇴피·반출대 내려놓기 각각의 독립 근거를 제공해야 한다.
2. Harbor 담당이 해당 계약의 고정 버전·공개 payload를 업무 ID·계획 revision과 대응시키고, 원본 근거 ID/시각/revision/digest를 보존하는 어댑터를 구현한다. 모호한 값은 HOLD 또는 거절하며 자동 진행시키지 않는다.
3. 계약 시험에서 위조 출처, 누락 필드, 미래/오래된 시각, 재사용 ID, 이전 attempt/revision, 다른 화물/목적지, zone claim 미해제, 구역 안 자세, 오래된 측정 자세, 출처 종류 바꿔치기, 내려놓기 없음이 업무 진행·COMPLETED를 만들지 못함을 검증한다. 정상 예도 실제 인증 경로를 통과해야 한다.
4. 실물 담당이 해당 설치의 물체 관측·보정·퇴피·내려놓기를 확인하고 별도 수용 기록을 남긴다. HTTP·simulation 시험은 이 수용을 대신하지 않는다.

현재 상태는 **실제 HTTP 계약 검증 완료, 물리 관측 producer·운영 어댑터·실물 수용 미완료**다. 공유 모듈 통합 완료와 물리 작업 완료를 같은 상태로 표시하지 않는다.
