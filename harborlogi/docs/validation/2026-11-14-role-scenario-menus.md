# 역할 시나리오별 권한·메뉴 검증

2026-11-14. 기준은 `feat/role-scenario-menus`의 `ebf4db5`(코드)·`3162d11`(문서 수정 뒤)이며 origin/main `65d7cca`에서 시작했다. 설계는 [설계34](../design/34-role-scenario-access-and-menus.md)다. 로컬 개발 PC의 합성 미리보기 증거이며 현장 태블릿·운영 계정·LAN/TLS 수용이 아니다.

## 실제 구현

- `harbor_web/access.py`가 화면 경로·허용 역할·메뉴 묶음·페르소나 이름의 단일 원본이다. view의 `require_roles`는 바꾸지 않았고 `test_access_policy`가 둘을 HTTP로 대조한다.
- 메뉴는 업무 처리 / 재고·근거 / 현장 / 관리 / 도움말로 묶고 열 수 있는 화면만 보인다. 머리의 현재 위치와 사이드바 아래 담당 이름도 같은 원본에서 읽는다.
- 403은 역할에 따라 운영 현황·현장 확인으로 돌아가고, 역할이 없으면 역할 안내와 비밀번호 변경만 둔다. 설정의 계정 관리 링크는 staff에게만 보인다.
- 업무 안내는 S1~S11 시나리오에서 내 역할의 일을 먼저 보이고, 열 수 있는 화면만 링크한다. 다른 담당의 일은 접어 둔다.
- `tools/product_preview.py`가 페르소나 계정 4개를 만든다. 비밀번호는 비공개 `access.json`에만 있다.

## 실행한 검사

| 검사 | 결과와 한계 |
|---|---|
| 기준선(origin/main `65d7cca`) `django test harbor_web.tests` | Ran 355 tests, OK |
| 병합본 `django test harbor_web.tests --settings=harbor_web.settings` | Ran 379 tests, OK(새 시험 24개: 접근 행렬·메뉴 8, 403·계정 링크 5, 미리보기 계정 7, 업무 안내 4) |
| `makemigrations --check --dry-run` | No changes detected |
| `python -m unittest discover -s tests` | Ran 264 tests, OK(skipped=2). 실제 Fleet 선택 시험은 미설정 skip이며 통과로 세지 않음 |
| `node tests/*.test.mjs` | 5개 모두 통과 |
| `tools/design_tokens.py --check` | Token CSS matches source(토큰 변경 없음) |
| 역할별 HTTP 행렬(시험 밖 실측, 변경 전) | 4개 역할 × 28개 경로. 권한 위반과 403 링크 없음. 설계34 §4의 근거 |

## 브라우저 확인

Playwright + 시스템 Chrome으로 별도 포트(8061)·별도 DB의 합성 미리보기를 열었다. 비밀번호는 스크립트가 `access.json`에서 직접 읽었고 출력하지 않았다.

| 계정 | 폭 | 확인한 것 |
|---|---|---|
| 겸임(`harbor-preview`) | 1440 펼침·접힘, 1100, 600 메뉴 열기 | 다섯 묶음 제목·구분선, 접힌 메뉴에서 제목 숨김, 600px에서 묶음마다 두 열, 가로 넘침 없음 |
| 현장 확인 | 1440 | 메뉴는 현장 확인·업무 안내만. `/` → `/field/`. 합성 회차가 `harbor-field`로 재배정됨(기존 DB). `/requests/` 403에서 “현장 확인으로 돌아가기” |
| 조회 | 1440 | `/settings/`·`/field/` 403, 업무 안내 200 |
| 업무 담당 | 1440, 600 | 업무 안내 ‘내 일’이 S1부터, 화물 등록 링크. `/inventory/import/` 403 |
| 관리(staff) | 1440 | 설정에 팀 계정 관리·역할별 권한 관리 링크 |

## 확인하지 못한 것

- staff가 아닌 admin의 설정 화면은 브라우저로 보지 않았다(시험 `test_account_admin_link_needs_staff`로만 확인).
- 역할 없는 계정의 화면은 브라우저로 보지 않았다(시험으로만 확인).
- 태블릿 실기기, 화면 읽기 프로그램, 운영 계정·실제 Fleet 연결은 확인하지 않았다.
