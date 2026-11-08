# 항만 관제 시스템 조사 — Harbor에 보여 줄 관제 요소

작성: 2026-11-04. 웹 공개 자료(뉴스·벤더 문서·논문 요약) 기반 1차 조사. 벤더 주장은 검증되지 않은 홍보 문구로 취급한다.

이 조사를 바탕으로 한 결정은 [ADR 0020](../adr/0020-port-control-patterns-as-planned-display.md)이다. 아래 4절의 제안 중 무엇을 채택·보류·거부했는지는 ADR이 정본이다.

## 1. 결론 요약

세계 항만의 관제는 크게 세 층이다.

| 층 | 대표 사례 | 관제 화면이 답하는 질문 |
|---|---|---|
| 항만 공동체(여러 터미널) | 부산항 체인포털·Port-i, 로테르담 PortXchange | 배가 언제 어느 선석에 오고, 터미널 사이 환적은 막힘이 없는가 |
| 터미널 운영(TOS) | Navis N4, CyberLogitec OPUS(부산 신항 BNCT·DGT), Tideworks, Kalmar | 선석·본선·야드·게이트 계획이 무엇이고, 지금 어디까지 진행됐나 |
| 장비 제어(ECS)·원격 조작 | N4 Equipment Control·XPS, HHLA CTA/CTB 관제실, 부산 신항 2026~2029 자율주행 통합관제 | 어떤 장비가 어떤 작업을 맡았고, 사람이 개입해야 할 예외는 무엇인가 |

Harbor는 3×3×3 야드에서 **양하 → 장치 → 재취급을 동반한 반출**을 다룬다. 실제 항만에 견주면 **TOS의 야드 계획 + ECS의 작업 지시·예외 처리**에 해당한다. 규모를 흉내 내기보다 이 두 층의 관제 패턴을 정확하게 보여 주는 쪽이 설득력이 있다. 특히 재취급 최소화는 실제 터미널 야드 효율의 핵심 지표이자 연구 주제(pre-marshalling)여서 Harbor의 주제와 그대로 겹친다.

## 2. 사례별 핵심

### 부산항

- **체인포털(BPA)**: 선사·운송사·터미널을 잇는 항만물류 통합 플랫폼. 타부두 환적 운송(ITT/TSS)을 실시간 공유하고, 세계은행 보고서에 디지털화 우수 사례로 소개됐다.
- **Port-i 환적모니터링(2026)**: 부산항 9개 컨테이너 터미널의 선석 스케줄 통합 조회, 선박·화물 모니터링, **이상 탐지·알림**. 2026년 3월 체인포털 가입자 전체로 개방, AI 환적 이상탐지·AIS 선박 위치가 로드맵에 있다.
- **신항 자동화 터미널**: BNCT가 국내 첫 수직배열 자동화, 동원글로벌터미널(DGT)이 국내 첫 완전자동화 부두. TOS는 CyberLogitec OPUS.
- **2025-11~**: BPA가 CyberLogitec과 통합 장비제어시스템(ECS)·해양 디지털트윈 플랫폼 개발 착수(진해신항 대상). "장비와 TOS의 매끄러운 통합"을 핵심 가치로 제시.
- **2026~2029 자율주행 스마트항만 사업**(454.6억, DGT 부지): 하역·이송 장비 자율주행 전환과 **AI 기반 통합 관제**.
- **크레인 설비보전 통합관리 플랫폼**(2026-04 성과 세미나): 정비 이력을 디지털트윈으로 DB화·시각화.

### 로테르담 — PortXchange Synchronizer

- ETA가 정해지면 선박마다 **기항 타임라인**을 만들어 입항부터 출항까지 모든 이벤트를 한 줄에 표시.
- 이해관계자 간 계획 차이에 **알림**, 선석 계획 실시간 갱신, 적시 도착(JIT) 속도 제안. 대기시간 20% 절감은 벤더 자체 수치.

### 싱가포르 PSA Tuas

- 개장 전 NUS가 Tuas 디지털트윈 시뮬레이션으로 운영 효율을 예측. AGV 군집 관리 연구(A*STAR). 원격 관제실 구성은 공개 자료로 확인하지 못했다.

### 함부르크 HHLA

- CTA: IT 시스템이 블록 장치장 내 적치 위치를 지시하고 크레인을 제어. 원격 조작자는 **트럭 인계 지점에서만** 개입(카메라·조이스틱).
- CTB: 관제센터의 원격 조작실에서 영상으로 장치장 크레인을 조작.
- 교훈: 자동화 관제의 화면은 "모든 것을 조작"이 아니라 **자동이 못 하는 예외 지점에 사람을 부르는 것**이 중심이다.

### Navis N4 (TOS·ECS 표준 화면 구조)

- **Point of Work별 작업 목록**: 장비 ID, 배차 상태, 이동 유형, 현재 위치, 작업 시각.
- 작업 지시는 완료·보류·다른 장비 실행 시 목록에서 빠진다.
- **XPS Yard Satellite / Yard Plan**: 야드 평면 위에 자동 크레인과 인계 구역(transfer zone) 위치를 표시. Pools & Equipment 창에서 장비 풀 상태 추적.

## 3. 관제 화면에서 반복되는 요소

1. **시간축 계획**: 선석 간트(berth window), 기항 타임라인, 계획 대 실제.
2. **야드 지도**: 블록·베이·단 단면(bay view), 점유율 색, 예약 칸.
3. **작업 목록(work queue)**: 작업 지시별 상태(대기·배차·실행·완료·보류)와 담당 장비.
4. **장비 상태판**: 장비별 위치·현재 작업·가용/고장/충전.
5. **예외·알림**: 계획 이탈, 이상 탐지, 사람 개입 요청과 다음 조치.
6. **KPI 타일**: 아래 표.

| KPI | 정의(조사 근거) | Harbor 대응 |
|---|---|---|
| 크레인 생산성(GCH) | 첫~마지막 작업 사이 처리 박스 수/시간. 해치커버는 2회로 셈. 순생산성은 지연 차감 | 팔 1대당 이동/시간 |
| 선석 생산성 | 크레인 생산성 × 투입 크레인 수. 재취급·해치커버는 비생산 이동 | 반출 업무당 총 이동 대비 생산 이동 |
| 재취급 비율 | 야드 효율의 대표 지표(논문), 표준 공식은 미확인 | **반출 1건당 재취급 이동 수** — Harbor의 핵심 지표 |
| 야드 생산성·점유율 | 단위 면적당 처리량, 점유율 | 27개 위치 점유율, 칸별 높이 |
| 체류시간(dwell) | 장치 기간 | 화물별 장치→반출 시간 |
| 트럭 회전시간 | 정의 자료 미확보 | Pinky 이송 왕복 시간 |

## 4. Harbor 대응 — 있는 것과 보탤 것

현재 Harbor(2026-11-04 코드 기준): 운영 현황(조치 필요·막힌 이유), 반출 작업 공간(야드·단계별 예상 배치·지시 이력), 3D 야드, 버전 맵 편집기, 자동 배치·재계획 설명·재생(ADR 0010·0011), 사용자 확인 후 지시 전송(ADR 0012), Fleet 연결. 운영 현황의 "실제 완료량·처리율"은 **미집계**로 표시된다.

| 관제 요소 | Harbor 현재 | 보탤 것(작은 것부터) |
|---|---|---|
| 예외·다음 조치 | 있음(지금 필요한 조치, 막힌 이유) | Port-i식 "이상 → 알림 → 조치" 한 줄 형식으로 통일 |
| 야드 지도·단면 | 있음(평면·3D·bay_stack) | 점유율·재취급 예정 칸 색 강조 |
| 작업 목록 | 업무·계획 단계 있음 | N4식 열: 작업·담당 장비(팔 A/B/S, Pinky)·상태·시각 |
| 장비 상태판 | Fleet 연결 정보 일부 | 장비별 현재 작업·가용 상태 한 줄 표(값은 Fleet 근거만, 합성 금지) |
| 시간축 | 지시 이력·재생 있음 | 양하(입항)→장치→반출 업무 타임라인, 계획 대 실제 |
| KPI | 미집계 | 완료 근거로 계산 가능한 것만: 재취급 이동 수, 업무 소요시간, 점유율 |
| 항만 공동체 층 | 없음 | 범위 밖. 필요하면 "선박 도착 예정" 입력 한 칸으로 양하 업무 시작을 표현 |

권장 순서: **KPI(재취급·소요시간·점유율)** → **작업 목록 장비 열** → **업무 타임라인**. 세 가지 모두 이미 저장된 계획·완료 근거로 계산할 수 있어 새 장치 연동이 필요 없다. 실제 장비 상태판은 Fleet 근거가 들어온 뒤에 한다.

주의: 실제 항만 수치나 화면을 모방하지 않는다. 출처 없는 값(가짜 선박명·가짜 처리량)을 데모용으로 합성하지 않는다(PRODUCT.md의 "담당자를 합성하지 않는다" 원칙과 같음).

## 5. 확인하지 못한 것

- 부산항 PORT-MIS(해수부 입출항 관리)의 화면 구성, BPA 통합관제센터 실화면.
- PSA Tuas 원격 관제실 구성, 칭다오 자동화 터미널 관제실.
- 재취급 비율·트럭 회전시간의 표준 정의(UNCTAD·ITF 지표 문서 추가 조사 필요).

## 출처

- [CyberLogitec, 국내 첫 자동화 터미널 TOS (Port Technology)](https://porttechnology.org/news/cyberlogitec-implements-new-tos-at-south-koreas-first-automated-terminal)
- [DGT, CyberLogitec TOS 선정 (Container News)](https://container-news.com/dongwon-global-terminal-selects-cyberlogitecs-terminal-operating-system)
- [부산 신항 국내 첫 완전자동화 터미널 (Marine Insight)](https://www.marineinsight.com/shipping-news/busan-new-port-unveils-south-koreas-first-fully-automated-terminal/)
- [CyberLogitec, 진해신항 ECS·디지털트윈 착수 (PR Asia)](https://en.prnasia.com/releases/apac/cyberlogitec-launches-development-of-ecs-and-digital-twin-platform-for-jinhae-new-port-s-ai-driven-mega-terminal-511958.shtml)
- [부산 스마트항만 455억 (한국경제)](https://www.hankyung.com/article/2026081399591)
- [부산시 스마트항만 모빌리티 플랫폼 (서플)](https://supple.kr/news/cmsr06prc003olxqtgnbw6ggw)
- [BPA 항만설비 통합관리 플랫폼 세미나 (네이트)](https://m.news.nate.com/view/20260429n26359)
- [부산항 디지털트윈 실증 가상 플랫폼 (부산일보)](https://www.busan.com/view/busan/view.php?code=2021052313551107267)
- [부산항 실시간 환적상황 전체 개방 (헤럴드경제)](https://www.heraldk.com/article/2026030221150427181)
- [BPA, 로테르담 항만 디지털 협력 (헤럴드경제)](https://www.heraldk.com/article/2026041517110849536)
- [체인포털 세계은행 우수사례 (보안뉴스)](https://m.boannews.com/html/detail.html?idx=94322)
- [PortXchange Synchronizer](https://port-xchange.com/?p=3824)
- [Port of Rotterdam Pronto](https://www.portofrotterdam.com/nl/pronto)
- [Tuas 디지털트윈 (Singapore Polytechnic)](https://sp.edu.sg/sp/news/sp/'digital-twin'-to-test-upcoming-tuas-mega-port's-design)
- [HHLA 자동화](https://hhla.de/en/the-power-of-networks/automation), [CTA 작동 방식](https://hhla.de/en/magazine/thats-how-the-cta-works), [CTB 장치장 블록](https://www.hhla.de/en/magazine/storageblock)
- [Navis N4 Job list](https://n4.ptp.com.my/apex/webHelp/apexHelp/Content/N4%20Equipment%20Control/Job%20list.htm), [Using XPS](https://n4.ptp.com.my/apex/webHelp/apexHelp/Content/N4%20Automation%20Guide/Using%20XPS.htm)
- [TOS 기능 개요 (AltexSoft)](https://altexsoft.com/blog/terminal-operating-system)
- [크레인 생산성 정의 (TIC)](https://tic40.atlassian.net/wiki/x/aQBMT), [선석 생산성 (Ashar)](https://www.asafashar.com/Asaf_Berth_Productivity.pdf), [항만 지표 (BTS)](https://www.bts.gov/sites/bts.dot.gov/files/2024-08/PPFSWG%20071516%20Smith%20Port%20Metrics%20Presentation_0.pdf)
- [재취급 최소화·선제 재정렬 (SAGE 2021)](https://journals.sagepub.com/doi/10.1177/1748302621994010)
