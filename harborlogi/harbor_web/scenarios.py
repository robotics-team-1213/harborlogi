"""Persona scenarios behind the 업무 안내 page (docs/design/34 section 3). Every step names a registered access.PAGES key."""
from dataclasses import dataclass

from harbor_web import access


@dataclass(frozen=True)
class Step:
    page: str
    text: str


@dataclass(frozen=True)
class Scenario:
    key: str
    persona: str
    title: str
    steps: tuple
    handoff: str


SCENARIOS = (
    Scenario('S1', 'operator', '입고·반출 지시', (
        Step('cargo', '화물과 요청의 현재 상태를 확인합니다. 결과는 화물·요청 revision으로 남습니다.'),
        Step('cargo_new', '화물 ID와 점유 크기(1×1·2×1·3×1)를 등록합니다. 크기 등록은 재고 배치와 구별됩니다.'),
        Step('request_new', '입고·반출 지시와 우선순위를 만듭니다.'),
    ), '계획 검토(S2)'),
    Scenario('S2', 'operator', '계획 검토·제안', (
        Step('inventory', '출처·시각·설치 기준을 확인하고 검토할 관측 원본을 선택합니다. 과거 관측을 선택하면 그 선택을 다음 화면에 유지합니다.'),
        Step('cargo', '화물·요청에서 요청 상세를 열고, 요청 상세에서 계획을 만들어 재취급 → 반출 → 복귀 순서를 검토합니다. 미리보기는 예상 위치이며 실제 위치 기록을 바꾸지 않습니다. 계획은 관측과 지시에 고정됩니다.'),
        Step('work', '실행 상세에서 Harbor가 현재 단계의 제안을 제출합니다. ROSY Fleet의 다른 운영자가 원본과 자원을 검토해 별도로 승인하고, 실행 소유자는 ROSY입니다. 결과가 불명확하면 저장된 요청으로 원본을 대조합니다.'),
    ), 'Fleet 승인·운영'),
    Scenario('S3', 'operator', '반출 순서 조정', (
        Step('outbound', '반출 대상과 재취급 화물을 확인합니다.'),
        Step('outbound', '반출 우선순위를 바꿉니다. 순서를 바꾸면 계획을 다시 검토해야 합니다.'),
    ), '계획 재검토(S2)'),
    Scenario('S4', 'operator', '입고 회차 배정', (
        Step('cargo', '요청 상세에서 입고 요청을 확인합니다.'),
        Step('field_new', '입고 차량 ID와 화물로 현장 확인 회차를 배정합니다. 확인이 오래됐거나 대상이 다르면 새 회차를 배정하고 현장에서 다시 확인합니다.'),
    ), '현장 확인 담당(S5)'),
    Scenario('S5', 'field', '현장 확인', (
        Step('field', '배정받은 현장 확인 목록을 엽니다.'),
        Step('field', '회차 상세에서 배정받은 차량 ID·화물·회차가 맞는지 확인합니다.'),
        Step('field', '공급암 S가 적재면을 비웠는지 직접 기록합니다. 확인은 5분 안에서만 출발 전제에 사용할 수 있습니다. 출발 전제 기록 뒤 실제 차량 이송은 Fleet 운영자에게 인계합니다.'),
    ), '업무 담당(출발 전제)'),
    Scenario('S6', 'operator', '완료 근거 대조', (
        Step('work', '실행 상세에서 실행 시도와 결과를 확인합니다.'),
        Step('records', '요청별 변경, 실제 배치, 재취급 복귀와 관련 팔 퇴피 근거를 확인합니다. Action 성공과 물리 receipt만으로 전체 업무를 완료하지 않습니다.'),
    ), ''),
    Scenario('S7', 'admin', '설치 기준·맵', (
        Step('settings', '팀 역할과 구역 맵 상태를 확인합니다.'),
        Step('map_new', '구역 맵을 편집하거나 설치 기준을 등록합니다.'),
        Step('condition_new', '검증한 설치 기준을 등록합니다.'),
    ), '업무 담당(새 기준으로 계획)'),
    Scenario('S8', 'admin', '관측 원본 등록', (
        Step('inventory', '원본 배치와 설치 기준을 확인합니다.'),
        Step('observation_new', '관측 원본을 등록합니다.'),
    ), '업무 담당(S2)'),
    Scenario('S9', 'admin', 'Fleet 연동 확인', (
        Step('settings', '설정·계정에서 연동 설정을 엽니다.'),
        Step('integration', '설정된 연동과 남은 조건을 확인합니다. 다칸 계획은 검토할 수 있지만 실제 운영 인계에는 검증된 공간·장치 대응과 인증된 관측·업무 근거 연결이 필요합니다.'),
    ), 'Fleet 담당'),
    Scenario('S10', 'admin', '계정 발급', (
        Step('settings', '설정·계정에서 계정 관리(/admin/)로 이동해 계정을 발급합니다. 계정 관리는 staff 계정만 열 수 있습니다.'),
    ), ''),
    Scenario('S11', 'viewer', '현황 읽기', (
        Step('overview', '조치가 필요한 업무를 확인합니다.'),
        Step('cargo', '업무 상태를 읽습니다.'),
        Step('inventory', '재고와 관측 원본을 읽습니다.'),
        Step('records', '기록과 비교 결과를 읽습니다.'),
    ), ''),
)


def _owner(key):
    roles = access.page(key).roles
    return next(name for role, name in access.PERSONAS.items() if role in roles)


def _row(scenario, user_roles):
    steps = []
    for step in scenario.steps:
        target = access.page(step.page)
        openable = access.can_open(user_roles, step.page)
        steps.append({'label': target.label, 'href': target.path if openable else None,
                      'owner': '' if openable else _owner(step.page), 'text': step.text})
    return {'key': scenario.key, 'title': scenario.title, 'persona': access.PERSONAS[scenario.persona],
            'handoff': scenario.handoff, 'steps': steps}


def scenario_rows(user_roles) -> dict:
    held = set(user_roles)
    rows = {'mine': [], 'others': []}
    for scenario in SCENARIOS:
        rows['mine' if scenario.persona in held else 'others'].append(_row(scenario, held))
    return rows
