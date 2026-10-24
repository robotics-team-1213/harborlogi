# HarborLogi

![HarborLogi — Cooperative Container Robotics](harborlogi/assets/brand/harborlogi-github-banner-20261023.png)

*프로젝트 컨셉 이미지이며 실제 장비·시연 사진이 아닙니다.*

회수 순위가 바뀌어도 덜 파내는 소형 컨테이너 야드.

커널아카데미 로보틱스 심화 캠프 주말 야간반 1기 2팀 프로젝트다.

개발 정본은 [robotics-team-1213/harborlogi](https://github.com/robotics-team-1213/harborlogi)의 `main`이다. 팀 저장소에서 개발 PR을 리뷰·병합하고, 확정 변경을 [kernel-robot-8th/kernel-robot-repo-2](https://github.com/kernel-robot-8th/kernel-robot-repo-2)에 제출 PR로 반영한다. 자세한 절차는 [함께 작업하기](CONTRIBUTING.md)를 따른다.

## 해결하려는 문제

야드에 컨테이너를 처음 계획에 맞게 쌓아도, 선박·육상 운송 작업계획이 바뀌면 먼저 꺼내야 할 화물이 아래에 묻힌다. HarborLogi는 회수 순위가 바뀌었을 때 위의 방해 화물을 어디에 언제 둘지 정해, 묻힌 목표를 꺼내며 생기는 추가 취급과 출고 준비 지연을 줄인다.

모형으로 다룬다. 장비는 로봇팔 OMX-F 3대(암 S는 공급·회수, 암 A·B는 야드 적층·반출)와 이동 로봇 Pinky 3대(운송), 작업물은 규격 컨테이너 모형이다. 실제 선박·선사 시스템과는 연동하지 않고, 변경된 회수 지시를 입력으로 받는다.

## 구성

| 구성 | 맡는 일 | 위치 |
|---|---|---|
| **Harbor** (이 저장소) | 컨테이너·슬롯·적층 관계, 회수 순위와 변경 지시, 방해 화물 처리 순서, 업무 장부 | [`harborlogi/`](harborlogi/README.md) |
| **ROSY Platform** | 작업 접수·자원 배정, 관측, 실행 근거, 로봇 실행·정지 권한 | [robotics-team-1213/rosy-platform](https://github.com/robotics-team-1213/rosy-platform) |

Harbor는 ROSY 위에서 도는 별도 애플리케이션이다. ROSY 코드를 가져오지 않고 Fleet HTTP API로 작업을 제안·조회만 하며, 로봇에 직접 명령하지 않는다.

## 빠른 시작

```bash
cd harborlogi
python -m unittest discover -s tests -v
python -m harbor
```

자세한 설명은 [harborlogi/README.md](harborlogi/README.md)에 있다. CI는 push·PR마다 `harborlogi/`에서 Python 3.10·3.12 unittest를 실행한다.

무엇을 Harbor와 ROSY 중 어디서 고치는지, 둘을 이어서 확인하는 단계, PR 절차는 [함께 작업하기](CONTRIBUTING.md)를 따른다.

## 진행 상태

- 회수 제안 정책, 운송 후보 배정, 업무 장부, 공정 제안 HTTP 연결을 구현하고 단위 시험으로 확인했다.
- 고정된 ROSY 소스의 Fleet 시뮬레이션 서버를 상대로 HTTP 계약과 업무 대조를 시험했다([검증 기록](harborlogi/docs/validation/)).
- 물리 관측을 업무 완료 근거로 받는 연동과 실물 로봇 수용 시험은 아직 하지 않았다([수용 조건](harborlogi/docs/observation-acceptance.md)).
- 회수 순위 변경 시 처리 방식(한 팔 + 임시 보관, 회수 전 사전 재정렬, 두 팔 일시 보유)은 비교 전이며 어느 쪽도 확정하지 않았다.

## 라이선스

[Apache-2.0](LICENSE)
