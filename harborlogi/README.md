# Harbor

<img src="assets/brand/harbor-app-icon-20261023.png" alt="컨테이너로 구성한 Harbor H 아이콘" width="128" height="128">

HarborLogi의 항만 업무 앱이다. 선적 순서가 바뀌었을 때 묻힌 컨테이너를 꺼내는 재취급을 동반한 반출 순서와 재취급 화물을 둘 임시 장치 칸을 제안하고, 그 작업이 실제로 끝났는지를 업무 장부에 대조한다. 로봇을 직접 움직이지 않는다. 실행은 ROSY Platform이 맡는다.

커버·배너·아이콘 원본과 사용 위치는 [브랜드 자산](assets/brand/README.md)에 있다.

## 빠른 시작

Python 3.10 이상. domain·CLI·기존 조회 preview는 표준 라이브러리만 사용한다. 계정과 영속 업무를 가진 제품 웹은 선택 의존성 `.[web]`을 설치한다.

```bash
cd harborlogi
python -m unittest discover -s tests -v
python -m harbor
```

`python -m harbor`는 합성 재고로 만든 반출 제안을 출력한다. 반출 대상 화물 `outbound` 위의 재취급 화물 `rehandle`을 임시 장치 칸 `temp`로 옮기고, `outbound`를 반출대 `stand`에 내려놓은 뒤 `rehandle`을 원래 장치 칸으로 돌리는 순서다. 용어는 [CONTEXT.md](CONTEXT.md)를 따른다.

```json
{
  "status": "READY",
  "reason": "PROPOSAL_REQUIRES_ADMISSION_AND_EVIDENCE",
  "moves": [
    {"cargo_id": "rehandle", "source": "yard", "destination": "temp", "primary_arm": "A", "alternate_arms": []},
    {"cargo_id": "outbound", "source": "yard", "destination": "stand", "primary_arm": "A", "alternate_arms": ["B"]},
    {"cargo_id": "rehandle", "source": "temp", "destination": "yard", "primary_arm": "A", "alternate_arms": []}
  ],
  "required_completion": ["outbound_placed_on_stand", "rehandles_restored", "arms_retreated"]
}
```

`READY`는 관측 기준으로 세운 업무 제안일 뿐 실행 허가가 아니다. 이동마다 적층암 A·B 중 주 팔(`primary_arm`)과 대체 팔(`alternate_arms`)을 붙인다. 팔 배정은 설치자가 준 도달 표(`PortConstraints.arm_reach`)로만 하고, 표가 없거나 닿는 팔이 없으면 `HOLD`다. 플랫폼 compile 사전 검증 조회 계약(D-856 P1)이 생기면 그 결과로 바꾼다. 실제 arm claim과 대체 팔 전환은 Fleet이 한다. 업무 완료는 `required_completion`의 세 가지(반출 대상 화물을 반출대에 내려놓음, 재취급 화물 복귀, 관련 팔 퇴피)에 실제 근거가 있을 때만 판정한다. 필요하면 `python -m pip install -e .`로 설치한다.

## 웹 작업 관제

계정/역할·화물/요청·관측/계획·작업 인계·현장 확인·기록/비교를 가진 새 웹은 [제품 웹 안내](docs/product-web.md)를 따른다. 로컬 검토는 `runs/web-venv/Scripts/python.exe tools/product_preview.py`와 `http://127.0.0.1:8060/`이다. 합성 개발 자료이며 운영 관측/전체 장치 수용은 별도다.

기존 `python -m harbor.web --demo --port 8050`과 `http://127.0.0.1:8050/overview`는 호환용 로컬 조회 preview다. 실제 장부와 ROSY ARM job·receipt 읽기는 [조회 웹 안내](docs/web-workbench.md)를 따른다. 이 preview는 계정이나 업무 쓰기를 제공하지 않는다.

## Harbor와 ROSY의 경계

제품 웹은 저장된 요청·관측 revision으로 기존 계획 엔진의 재취급→반출→복귀 또는 공간/관측 HOLD를 계산한다. 기존 preview의 `/planning`은 고정 합성 입력이다. 실제 인증 관측 producer·입고 planner·전체 공정/현장 수용은 미완료이며 물리 receipt만으로 전체 업무를 완료하지 않는다.

| Harbor가 하는 일 | ROSY가 하는 일 |
|---|---|
| 컨테이너·장치 칸·단 관계, 반출 순서와 변경 지시의 계획 버전 | 작업 접수(admission)와 자원 배정 |
| 재취급·반출·복귀 순서 제안 | 실제 경로·팔 자세·공유 구역·장치 능력 확인 |
| 중복 요청 방지와 업무 장부(SQLite) | 장치 실행·정지와 실행 근거 |
| 업무 완료 판정 (관측 근거와 대조) | 관측(World)과 작업 진행 기록 |

Harbor는 ROSY 코드를 import하지 않고 ROSY 계층 꼭대기 관제(현장에서는 통합관제 R) 하나의 Fleet HTTP API로 제안·조회만 한다. 팔 작업의 승인은 셀 관제가 한다([ADR-0018](docs/adr/0018-harbor-enters-rosy-through-integrated-control.md)). 자동 승인(admit)·재무장(rearm)·직접 장치 명령은 없다. 오래된 관측, 계획 버전 불일치, 보유 불명, 자리 부족이면 일부만 실행하는 안을 만들지 않고 보류(`HOLD`)한다. 현재 보유 중인 화물이 하나라도 있으면 재관측과 대조가 먼저다. 이송 차량의 출발은 공급암 S의 퇴피 확인(1차 사람이 확인, 이후 카메라)을 근거로만 허가하고, 실제 주행·정지는 Fleet과 CORE가 맡는다.

## 구조

| 모듈 | 책임 |
|---|---|
| `harbor/model.py` | 장치 칸·관측 시각·물리 보유·계획 기준 버전의 불변 값 |
| `harbor/outbound.py` | 재취급·반출·복귀 순서의 결정적 제안 |
| `harbor/site.py` | 적층암 A/B 2대(공급암 S는 외부 단계로 관측만), 이송 차량 3대, 컨베이어(용량 1), 업무 위치·셀 위치 두 층, 충전 한 자리, 직렬 이동 제약 |
| `harbor/availability.py` | 수동 충전·대기·적재·정비·불명 이송 차량의 후보 제외 |
| `harbor/process.py` | 기존 공정 문서 binding과 중립 ARM 물리 참조 binding, 반출 계획의 이동별 대응 |
| `harbor/rosy_http.py` | 기존 Cell·TRIP와 중립 ARM 제안/조회·physical receipt의 HTTP 클라이언트. 승인·관측 제출은 제공하지 않음 |
| `harbor/workflow.py` | 중복 요청 방지·불명 접수 HOLD·현재 시도와 관측 대조의 업무 장부 |
| `harbor/departure.py` | 공급암 S 퇴피 확인(1차 사람, 이후 카메라)의 근거 보존과 이송 차량 출발 허용 (ADR 0005 결정 6) |

장치 칸의 화물 목록은 아래 단부터 위 단 순서다. 장치의 하중·형상·충돌은 이 재고 모델의 범위가 아니다. `allowed_moves`는 업무 계획에서 허용한 칸 사이 이동 방향일 뿐 실행 능력을 만들지 않는다.

[설치 예제](config/site.example.json)는 합성 ID와 개념 위치다. 셀 위치는 공급 작업대·컨베이어·장치 칸·임시 장치 칸·반출대이고, 업무 위치(본선·야드·반출 지점)와 구분한다. 재취급 화물은 컨베이어나 반출대에 두지 않는다. ADR 0005 이전 키(`transport_ids`·`slots`·`waiting_slots`·`charging_slots`·`transfers`, `arms`의 `S`)를 쓰면 새 키 이름을 알려 주는 오류로 거절한다. 특정 Pinky 번호에 이송·충전 역할을 고정하지 않는다. 수동 `available`은 앱의 제외 조건을 푸는 것일 뿐 실제 가용성이나 자동 충전 확인이 아니다. 운영 상태는 호출할 때 명시적으로 전달하고, 누락된 차량은 제외한다.

## ROSY와 실제로 연결할 때

실행 연결에는 설치자가 검증한 공정 binding과 관측 출처, 지원되는 Fleet 버전이 필요하다. 절차는 [공정 제안과 업무 장부 사용법](docs/execution.md)을 따른다. 운영 장부는 저장소 밖 경로에 둔다([장부 보관](docs/execution.md#장부-보관)).

`tests/integration/`의 실제 Fleet HTTP 시험은 고정된 ROSY Platform 소스와 그 시험 의존성이 있어야 돈다. 시뮬레이션 하네스는 Harbor 저장소 안의 `tools/integration/fleet_harness.py`다([ADR-0003](docs/adr/0003-own-fleet-contract-harness.md)). 플랫폼 소스가 없으면 이 시험은 건너뛰어진다(`skipped=1`). 기본 unittest와 CI는 이 시험 없이 통과한다.

## 문서

전체 자동 검사 실행: `python -m tools.check_all` — 준비 사항·기능별 검증 범위·결과 해석은 [기능 검증 안내](docs/testing.md)를 따른다.

| 문서 | 내용 |
|---|---|
| [../CONTRIBUTING.md](../CONTRIBUTING.md) | 함께 작업하기 — Harbor와 ROSY 중 어디서 고칠지, 연결 확인 단계, PR 절차 |
| [CONTEXT.md](CONTEXT.md) | 항만 업무 용어 |
| [PRODUCT.md](PRODUCT.md) | 관제 PC·현장 태블릿 제품 맥락과 확정/미정 |
| [역할·앱·UI/UX 상세설계](docs/design/README.md) | 역할·업무 흐름, 화면·예외, 앱 구성·계약 결손, 단계별 구현·수용 기준(검토안) |
| [docs/platform-contract.md](docs/platform-contract.md) | 플랫폼 연결 기준과 다음 작업 |
| [docs/execution.md](docs/execution.md) | 공정 제안과 업무 장부 사용법 |
| [docs/observation-acceptance.md](docs/observation-acceptance.md) | 물리 관측 연동의 수용 조건 |
| [docs/implementation-plan.md](docs/implementation-plan.md) | 실행 연결 계획 |
| [docs/adr/](docs/adr/) | 설계 결정 기록 (0001 공정 binding과 완료 근거, 0002 고정 소스 Fleet 계약 시험, 0003 Harbor가 가진 Fleet 계약 시험 하네스, 0004 도메인 중립 ROSY 위의 항만 앱 경계(제안), 0005 Harbor·ROSY 책임 지도, 0018 꼭대기 관제 하나로 연결, 0019 컨테이너 사건 이력, 0021 R 옆 운영 PC에 Compose 설치, 0027 Rosy 계정 로그인) |
| [docs/plans/](docs/plans/) | 작업 계획 (2026-10-21 항만 앱 분할) |
| [docs/validation/](docs/validation/) | 날짜별 검증 기록 |

코드와 함께 바뀌는 문서(용어 · ADR · 사용법 · 검증 기록)는 이 폴더에 둔다. 팀 기획 · 설계 본문과 회의록은 팀 Confluence(SP) 위키, 할 일은 Jira가 정본이다. GitHub 저장소 위키는 쓰지 않는다.

## 데이터와 라이선스

운영 장부·원본 데이터·토큰·개인 자료는 저장소에 넣지 않는다. `.gitignore`가 흔한 로컬 상태 파일을 막지만 모든 개인정보를 검사하지는 않으니, 추가할 파일은 커밋 전에 확인한다. 라이선스는 저장소 루트의 [Apache-2.0](../LICENSE)이다.
