# 이송 제안과 별도 운영자 승인 — 고정 양 끝 HTTP 검증 (2026-10-28)

ROSY D-908(API v1.329)의 이송 제안 경로를 실제 Fleet 코드(simulation profile)에 HTTP로 붙여 확인했다. 합성 하네스 장치이며 실물 수용이 아니다.

| 항목 | 값 |
|---|---|
| 플랫폼 | rosy-platform `0defdedfe299696aa92f68cbfa1abb98cbd301d7` (브랜치 `docs/neutral-trip-proposal-v1329`, main 미착지) |
| Harbor 시험 source | `67fc903837c5f2dc4f5924492bd53499d5b630db` 의 부모 커밋(시험 파일 포함) |
| 실행 | `python -m tools.integration.real_fleet_http --platform-root <고정 clone> --platform-sha 0defdedfe2… --transport-only` (Windows, 임시 깨끗한 clone) |
| 결과 | 2 tests OK |

확인한 것 (`tests/integration/transport_http.py`)

- 기존 운영자 직접 TRIP: 응답 유실 뒤 원래 key 대조와 도착(회귀).
- 제안: Harbor가 제안을 한 번 보내고 응답이 유실돼도 같은 key로 대조해 PROPOSED를 얻는다.
- 제안자 본인의 승인은 거절된다. 그동안 장부는 PROPOSED에 머문다.
- 다른 명명 운영자가 `/api/fleet/dispatch-control`의 열린 세대와 제안 digest로 승인하면 TRIP 작업이 생기고, Harbor는 원래 key 조회만으로 ADMITTED → GOAL_CONFIRMED까지 따라간다. Harbor는 admit를 호출하지 않는다.

남은 것: 플랫폼 main 착지 뒤 같은 시험을 착지 SHA로 다시 돌려 이 기록을 갱신한다. 현장 프로필 개방과 실물 Pinky 수용은 별도다.
