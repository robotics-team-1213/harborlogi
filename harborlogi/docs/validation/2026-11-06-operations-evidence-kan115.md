# KAN-115 이후 관제 화면 증거 캡처

[이슈 #35](https://github.com/robotics-team-1213/harborlogi/issues/35)의 1번 항목 기록이다. `main` `316ce7a`의 소스로 띄운 합성 미리보기를 Edge headless로 캡처했다. 합성 자료만 썼고 Fleet·실물 로봇은 연결하지 않았다.

## 결과

`python tools/capture_operations.py`가 끝까지 통과했다. 이미지 8장과 재생 영상 1개를 만들었고 페이지 오류는 0건이다. 캡처 전후로 Plan·Observation·Command 행 수가 같아 읽기와 재생이 원본을 바꾸지 않았다.

| 파일 | 확인한 내용 |
|---|---|
| `01-operations-overview.png` | 운영 현황 물량·계획·실행 |
| `02-cargo-separation-routes.png` | 화물별 분리·임시 이동·반출·재적재 경로 |
| `03-plan-before-after.png` | 이전·현재 계획의 경로 비교 |
| `04-plan-sequence-playback.png` | KAN-115의 '배치에서 전체 순서 보기'를 누르면 재생 상태가 된다 |
| `05-slow-midair.png` | 4.5초 이동의 중간 프레임 |
| `06-restacking-plane.png` | 복귀·재적재 후 예상 배치의 평면도 |
| `07-overview-768.png`, `07-overview-390.png` | 태블릿·모바일 폭의 운영 현황, 가로 넘침 없음 |
| `slow-plan-playback.webm` | 느린 재생 영상 |

`04`, `06`, `07-overview-390`은 사람이 읽는 방식으로 화면을 직접 열어 확인했다. 나머지는 도구의 검사 결과로 판단했다.

## 실행하면서 고친 것

- **도구가 오래됐다.** 도구는 평면도 전환 버튼을 눌렀는데, 폭 1200px 이상에서는 평면도가 3D 옆에 항상 표시돼 버튼이 숨겨져 있다. 화면 회귀가 아니라 도구가 설계를 따라가지 못한 것이어서, 표시된 평면도(`[data-yard-linked-flat]`)를 찍도록 고쳤다.
- **증거 폴더 날짜가 고정돼 있었다.** 새 캡처가 `2026-10-26` 폴더에 쌓이지 않도록 실행한 날짜로 만든다.

## 재현 조건

`tools/product_preview.py`를 **두 번 실행해야** 한다. 비교 화면(`.plan-comparison`)은 이전 계획이 있어야 나타나는데, 새 DB의 첫 실행에는 계획이 하나뿐이다. 첫 실행에서는 `03` 단계에서 시간 초과로 실패한다. 서버를 재시작하면 관측과 계획이 하나씩 더 생긴다.

## 증거 위치

이미지·영상·접근 파일은 공개 Git에 넣지 않는다. 캡처한 PC의 `runs/operations-evidence-2026-11-06/`와 그 안의 `report.json`에 있다.

## 확인하지 못한 것

- 실제 터치 기기에서 반출 순서 카드 끌기(#35의 2번). 사람이 기기로 확인해야 한다.
- `tools/capture_outbound.py`에도 같은 평면도 버튼 클릭이 있다. 이번 범위가 아니라 돌려 보거나 고치지 않았다.
