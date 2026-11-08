# Harbor 기능 검증

`harborlogi/`에서 실행한다. Python 3.10 이상, `python -m pip install '.[web]'`, Node.js, Playwright Python 패키지와 Microsoft Edge가 필요하다. Playwright가 없으면 `python -m pip install playwright`로 설치한다. 브라우저가 없거나 검사가 실행되지 않으면 실패로 보고한다.

```bash
python -m tools.check_all
```

이 실행기는 기존 테스트를 다음 순서로 모두 실행한다. 중간 실패 뒤에도 다음 검사를 계속하며, 하나라도 실패하면 종료 코드는 `1`이다. 별도 의존성을 추가하는 테스트 프레임워크는 사용하지 않는다.

각 검사는 최대 600초 실행한다. 시간 초과 시 살아 있는 검사 부모와 그 자식 서버·브라우저의 종료를 시도하고, 종료 전 출력도 로그에 보존한다. 종료 실패는 진단 로그에 남기고 해당 검사를 실패 처리한다. 이미 끝난 Windows 부모가 따로 남긴 프로세스는 감독하지 않으므로, 개별 하네스가 정상 종료 시 서버를 정리해야 한다. 누락된 실행 파일과 시간 초과는 통과나 건너뜀으로 처리하지 않는다.

| 검사 | 확인하는 기능 | 기존 테스트 위치 |
|---|---|---|
| domain | 입출고·재취급·공간 계획, 팔·이송 후보, 업무 장부, 중복 요청, HOLD·복구, HTTP 계약 | `tests/test_*.py` |
| web | 로그인·역할·권한, 화물·요청·관측, 계획·검토, 현장 확인·출발, 실행·이력·복구, Fleet 연결 | `harbor_web/tests/` |
| migrations | 모델 변경에 필요한 마이그레이션 누락 | Django `makemigrations --check --dry-run` |
| JavaScript 4종 | 맵 편집, 야드 모델·선택, 초안 보존 | `tests/*.test.mjs` 전체 자동 검색 |
| design | 디자인 토큰과 정적 자산 일치 | `tools/design_tokens.py --check` |
| browser | 로그인부터 업무·현장 확인·비교·계정 관리, 권한 거절, 4개 화면 크기·탐색·오류 | `tools/integration/product_web_browser_check.py` |
| map_editor | 실제 브라우저에서 맵 편집과 불변 버전 | `tools/integration/map_editor_browser_check.py` |
| multicell | 여러 칸을 차지하는 화물의 일관된 처리 | `tools/integration/multicell_browser_check.py` |
| fleet_bridge | 합성 HTTP 서버와 서비스 인증·연결·실행 경계 | `tools/integration/fleet_bridge_browser_check.py` |
| automatic_planning | 일정과 관측 변경에 따른 배치 계획 | `tools/integration/automatic_planning_browser_check.py` |

매번 `runs/check-all/run-<고유값>/report.json`과 검사별 `.log`를 남긴다. 브라우저 보고서·스크린샷·격리 DB는 그 아래 각 검사 폴더에 남는다. 기존 자동 계획 브라우저 검사는 `runs/automatic-planning-browser/`에 결과를 남긴다. 출력 상위 폴더를 바꾸려면 `--output <경로>`를 준다. `runs/`는 Git에서 제외한다.

운영 `HARBOR_*` 설정은 자식 프로세스에 전달하지 않는다. 웹 회귀는 메모리 DB, 브라우저 검사는 각 실행의 새 합성 DB를 사용한다. 운영 장부나 실제 로봇에 연결하는 명령은 이 실행기에 포함하지 않는다.

## 통과 결과의 범위

`PASSED`는 위 자동 검사가 통과했다는 뜻이다. 모든 가능한 입력, 실제 Fleet 설치, 실물 로봇, 현장 수용을 보장하지 않는다. 보고서의 `actual_fleet`과 `physical_acceptance`는 항상 `false`다.

실제 ROSY Fleet 소스 기반 계약 검사는 기본 실행에서 `skipped=1`로 남는다. 실행기는 외부 통합 설정도 제거하므로 이 결과를 실제 Fleet 통과로 세지 않는다. 해당 검증은 [실행 연결 문서](execution.md#실제-fleet-계약-시험)의 별도 하네스로 수행한다. 합성 HTTP 계약 검증과 실제 Fleet 검증을 구분한다.

실물 검증은 [물리 관측 수용 조건](observation-acceptance.md)을 충족한 뒤 별도로 수행한다.
