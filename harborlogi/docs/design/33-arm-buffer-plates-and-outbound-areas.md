# 팔별 칸 판과 반출 영역

2026-11-08 초안이다. 판 구성과 지도 구성은 사용자가 승인했다. 계획 규칙 L1~L6은 팀 결정 전의 제안이다. 결정 기록은 [ADR0030](../adr/0030-arm-buffer-plates-and-outbound-areas.md) 초안이다.
이 문서는 물리 도달성·칸 좌표·설치 binding·실행 허가를 만들지 않는다. 구현은 논리 지도와 계획 계산만 다룬다.

## 실물 구성

팔 A·B마다 임시 칸 판이 하나씩 있다.

- 판 하나는 부채꼴 줄 3개(R1~R3)와 줄마다 1x1 칸 3개(C1~C3)를 갖고, 칸은 최대 3단까지 쌓는다.
- 지금은 1x3 화물만 쓰므로 줄마다 화물이 하나 놓인다.
- 두 팔이 함께 닿는 반출대 자리는 셀에 없다(계산 근거: `Rosy/rosy-modeling/parts/omx-outbound-stand/README.md`, ROSY 작업 공간 문서). 그래서 반출 자리는 따로 두지 않고 각 팔 판의 줄 하나를 반출 줄로 쓴다.

## 지도 구성

판의 줄 하나가 영역 하나다. 영역 이름은 팔 접두사를 쓴다.

| 영역 | 크기(행×열×단) | 역할 |
|---|---|---|
| `a_out` | 1×3×1 | 반출(outbound) |
| `a_tmp` | 1×3×3 | 임시(temporary) |
| `a_sto` | 1×3×3 | 보관(storage) |
| `b_out`·`b_tmp`·`b_sto` | 같음 | A와 같은 역할, B 판 |

- 야드(`yard`)는 지금처럼 가운데에 둔다. 화면 배치(`area_presentation`)는 A 판을 A쪽, B 판을 B쪽에 놓는다.
- 반출 줄은 평소 고정한다. 반출 줄을 바꾸려면 지도 새 버전(새 Condition)을 만든다. 새 Condition은 전체 재관측이 필요하다([설계21](21-versioned-map-editor.md)).
- 예시 설정은 `config/spatial-policy.arm-plates.example.json`이다. 기존 `spatial-policy.example.json`은 단일 반출대(`stand`)를 쓰는 브라우저 점검 도구(`tools/integration/*`)가 읽으므로 그대로 둔다.
- 반출 영역이 둘이어도 조건 payload의 `outbound_stand`는 그대로 필수이고 첫 반출 영역을 뜻한다. 나머지는 선택 키 `extra_outbound_stands`(목록)에 둔다. 이 키가 없는 기존 payload는 바뀌지 않고 읽힌다.

## 계획 규칙(팀 결정 필요)

아래는 모두 제안이다. 팀이 정하기 전에는 구현의 근거가 아니다.

| 규칙 | 내용 | 구현 |
|---|---|---|
| L1 | 방해 화물은 옮긴 곳에 둘 수 있다. 되돌리지 않아도 되는 곳은 보관 역할 줄이다 | 없음. 지금은 복귀 단계가 보관 영역으로 되돌린다. 보관 줄이 있으면 후보가 되는 수준이다 |
| L2 | 팔마다 임시 칸에 1x3 예비 2자리를 보장한다(H−1, H=3단) | 없음 |
| L3 | 반출 순위가 빠른 화물이 위에 놓인다 | 기존 재배치 기준([설계32](32-cargo-rearrangement-criteria.md))의 연장. 새 코드 없음 |
| L4 | 스테이징 상한은 가장 앞 우선 그룹과 반출 영역 용량 중 작은 값이다 | 없음 |
| L5 | 반출은 그 화물을 옮기는 팔의 반출 영역으로 간다 | 있음. 이동마다 두 위치(출발·도착)를 모두 닿는 팔을 고르므로, 반출 영역 중 그 팔이 닿는 곳만 후보가 된다 |
| L6 | 방해 화물 처분은 재배치, 같이 반출, 거부(HOLD와 사유) 셋 중 하나다 | 재배치와 HOLD만 있다. 같이 반출은 없음 |

L5의 후보가 여럿이면(두 팔 모두 야드 칸에 닿고 각자 반출 줄에 닿음) 기존 계획의 비용 순서(막힘, 구역 변경, 이동 거리, 최종 단)가 같을 때 후보 서명 순서로 정한다. 결정적이지만 업무 의미는 없다. 팀이 "더 가까운 팔", "한가한 팔" 같은 기준을 정하면 그때 바꾼다.

## 사례 비교

| 사례 | 판과 닮은 점 |
|---|---|
| ASC 블록 끝 transfer point | 크레인 하나가 블록 끝 자리 몇 개를 교환 지점으로 쓴다. 반출 줄이 팔 판 안에 있는 것과 같다 |
| 창고 outbound staging lane | 출고 순서대로 줄에 모아 둔다. L3·L4와 같은 생각이다 |
| pre-marshalling(Lee & Chao 2009, EJOR 196) | 반출 순서에 맞춰 미리 쌓는 순서를 바꾼다. L2·L3·설계32의 재배치와 같은 문제다 |

## 크기를 섞어 쌓지 않는 규칙

컨테이너의 쌓기 돌기와 바닥 오목은 30 mm 간격이고, 판의 1x1 칸은 32 mm 간격이다. 그래서 크기를 섞어 쌓으면 실물이 맞지 않는다.

| 쌓기 | 계산 결과 |
|---|---|
| 1x1 셋 위 1x3 | 1.75 mm 떠서 앉지 못한다 |
| 1x1 셋 위 1x2 | 0.85 mm 뜬다 |
| 1x3 위 1x1 셋 | 앉기는 하지만 끝 칸이 1.8 mm 끌려간다 |
| 같은 종류끼리 | 맞는다 |

근거는 `Rosy/docs/workspace-records/2026-11-08/temp-slot-review/`의 `q3_*.py`, `q3_out30.txt`, `q3_out32.txt`다(ROSY 작업 공간 기록).

영역마다 쌓기 규칙을 고른다. 규칙이 없으면 기존 동작(빈틈 없는 지지만 요구)과 같다.

| 규칙 | 지지로 인정하는 경우 |
|---|---|
| `any`(기본) | 아래가 빈틈 없이 받치면 된다 |
| `same` | 아래 화물이 위 화물과 `length_cells`·방향·앵커(행·열)가 모두 같을 때만 |
| `same_1x1_on_wide` | `same`에 더해 1x1이 더 긴 화물 위에 앉는 경우. 끝 칸이 끌려가는 것을 감수하는 설정이다 |

- 조건 payload의 선택 키 `stack_rules`에 `{영역 ID: 규칙}`으로 둔다. 예시 설정은 판 영역(`a_*`·`b_*`)에 `same`을 켜고 야드는 두지 않는다. 야드에 켤지는 설정으로 정한다.
- 판정은 `harbor/spatial.py`의 `stack_ok` 한 곳이다. 관측 검증(`SpatialSnapshot`), `plan_spatial`의 목적지 투영, `plan_rearrangement`의 후보와 빈자리 계산, 자동 배치(`harbor/placement.py`, 관측 검증기를 최종 권한으로 쓴다), 지도 편집기의 payload 재구성이 이것을 거친다.
- 1x3 위 1x1 허용 여부는 팀 결정이다. 기본은 거절한다.
- 지도 편집기는 지운 영역의 규칙을 payload에서 함께 지운다. 편집 화면에서 규칙을 바꾸는 입력은 후속이다.

### 층별 실물 위치 보정(후속)

층이 올라가면 같은 칸이라도 실물 좌표가 달라진다. 쌓기 돌기와 칸 간격 차이, 컨테이너 높이 때문이다. 이 보정은 논리 지도가 아니라 설치 binding 쪽에서 층마다 가진다(설계29의 이동별 칸 대응). 이 문서의 구현은 보정값을 만들지 않고, 측정은 판이 설치된 뒤 한다.

## 구현 범위와 후속

바꾼 것(코드는 `harborlogi/` 안):

- `SpatialPolicy`에 `extra_outbound_areas`와 `outbound_areas` 속성을 더했다. 반출 영역은 유일해야 하고 야드·임시·보관과 겹치면 거절한다.
- `plan_spatial`은 반출 단계의 후보 영역으로 `outbound_areas`를 쓴다. 이미 어느 반출 영역에 있는 화물은 기존처럼 근거 확인 HOLD다.
- `plan_rearrangement`의 반출 적합 지표(`fits_outbound`)는 반출 영역 중 하나에 맞으면 맞는 것으로 본다.
- `site_config.py`가 `extra_outbound_stands`를 받아 검증한다(유일, 텍스트, 공간 영역 필요, 역할 겹침 금지).
- 지도 편집기(`map_config.py`)는 반출 영역을 하나 이상 받는다. 하나만 있으면 `extra_outbound_stands` 키를 만들지 않아 기존 payload와 같다.
- 화면 표시(`yard_view.py`, `operations.py`)는 모든 반출 영역을 "반출대"로 표시한다.
- `Area.stack_rule`과 `stack_ok`로 영역별 쌓기 규칙을 넣었다(위 절).
- 시험: `tests/test_spatial_outbound_lines.py`(`OutboundLineTests`, `StackRuleTests`), `harbor_web/tests/test_map_domain.py`의 `MultiOutboundMapTests`.

### 반출 영역이 하나라고 가정하는 곳(조사 목록)

| 위치 | 가정 | 상태 |
|---|---|---|
| `harbor/spatial.py` `SpatialPolicy.outbound_area` | 반출 영역 1개 | 수정: 속성 `outbound_areas` 추가, 기존 필드는 첫 영역 |
| `harbor/spatial.py` `plan_spatial`·`plan_rearrangement` | 목적지·참조·적합 검사가 1개 | 수정 |
| `harbor_web/map_config.py` "Exactly one outbound area" | 편집기 검증 | 수정: 하나 이상 |
| `harbor_web/site_config.py` | 조건 payload 키, 역할 분리 검사, 정책 구성 | 수정 |
| `harbor_web/yard_view.py`, `operations.py` | 반출대 표시 | 수정 |
| `harbor/model.py` `PortConstraints.outbound_stand` | 옛 Bay 경로 제약, 필수 1개 | 후속: 옛 경로는 단일 반출대로 둔다. 판 구성은 공간 영역 경로만 쓴다 |
| `harbor/outbound.py`, `harbor/placement.py` | 옛 Bay 계획과 자동 배치가 `outbound_stand` 1개 | 후속(위와 같은 이유) |
| `harbor/site.py`, `config/site.example.json` | 옛 셀 위치 `cell_locations.outbound_stand` | 후속 |
| `harbor/workflow.py`, `harbor_web/commands.py`, `human_evidence.py` | 장부와 Command가 `outbound_stand` 한 값 | 변경 없음: 값은 계획이 고른 도착 위치(앵커 주소)라서 영역이 둘이어도 맞다 |
| `harbor_web/command_binding.py` 설치 binding | 이동별 설치 대응 | 후속: 새 반출 줄마다 설치 binding과 팔 도달 원본이 필요하다(설계29) |
| `harbor_web/forms.py` 도움말, 3D 뷰(`yard_scene.py`) | 문구, 영역 목록 순회 | 도움말만 후속. 3D 뷰는 영역을 순회하므로 수정 없음 |
| 테스트 fixture(`harbor_web/tests/*`, `tests/*`) | 단일 `outbound_stand`로 구성 | 그대로 둔다(하위 호환 시험을 겸한다) |
| `tools/integration/*`, `tools/product_preview.py` | 예시 `stand` 영역 이름 | 그대로(기존 예시 보존) |

### 후속

- L1·L2·L4·L6 구현과 시험. 팀 결정이 먼저다.
- 옛 Bay 경로의 `PortConstraints`가 반출 영역을 여럿 받을지는 정하지 않았다. 판 구성은 공간 영역 경로만 쓰므로 필요가 확인되면 다룬다.
- 반출 줄·임시 칸의 설치 binding, 팔 도달 원본, 실제 칸 좌표. 판이 설치되고 측정된 뒤에 한다.
- `plan_rearrangement`의 반출 적합 지표(`fits_outbound`)는 팔 도달을 보지 않고 줄 폭만 본다. 줄마다 닿는 팔이 다르면(예: `a_out` 1칸, `b_out` 3칸, 1x3 화물은 A만 닿음) `b_out` 덕에 반출 가능으로 세어 재배치가 더 줄이지 않을 수 있다. 실제 반출 이동은 `plan_spatial`의 팔 검사(L5)가 막으므로 안전 문제는 아니다. L1·L2·L4·L6과 함께 다룬다(#51 리뷰).
- 물리 수용은 하지 않았다.

### 팀 결정이 필요한 항목

1. L1~L6 각각을 채택할지.
2. 반출 줄 후보가 여럿일 때 고르는 기준.
3. 보관 역할 줄(`a_sto`·`b_sto`)을 계획의 복귀 대상으로 쓸지.
4. 반출 줄을 바꾸는 절차(새 지도 버전과 전체 재관측)를 운영 규칙으로 받아들일지.
5. 판 영역의 쌓기 규칙을 `same`으로 둘지, 1x3 위 1x1(`same_1x1_on_wide`)을 허용할지, 야드에 켤지.
