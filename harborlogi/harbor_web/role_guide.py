"""Role-menu matrix and 3-line guidance cards derived from access.MENU_PAGES and access.PERSONAS."""
from typing import Iterable

from harbor_web import access

_ROLE_ACTIONS = {
    ('work', 'operator'): '읽기·예외 처리',
    ('inventory', 'admin'): '읽기·자료 등록',
    ('alerts', 'operator'): '읽기·예외 처리',
    ('alerts', 'field'): '읽기·현장 확인',
    ('records', 'operator'): '읽기·측정 등록',
    ('settings', 'admin'): '읽기·편집',
}

_ROLE_STEPS = {
    'operator': (
        '상황판과 처리할 것에서 예외·재검토가 필요한 일을 먼저 확인합니다.',
        '작업에서 오늘 시간표와 반출 순서를 살펴보고 예외 시에만 순서를 조정합니다.',
        '기록에서 완료 근거와 조건별 측정 결과를 확인합니다.',
    ),
    'field': (
        '처리할 것에서 내게 배정된 차량과 화물 확인 요청을 엽니다.',
        '현장에서 차량 적재면 상태를 확인하고 결과를 5분 안에 남깁니다.',
        '야드에서 현재 배치와 작업 예정, 탑뷰 화면을 함께 봅니다.',
    ),
    'admin': (
        '설정에서 구역 맵과 설치 기준 버전을 관리하고 계정 역할을 확인합니다.',
        '야드에서 기준별 배치와 카메라 연결 상태를 살펴봅니다.',
        'ROSY 연동 화면에서 서비스 연결 조건을 점검합니다. 작업·처리할 것은 읽기만 합니다.',
    ),
    'viewer': (
        '상황판에서 지금 운전 상태와 오늘 처리량, 야드 현황을 읽습니다.',
        '작업과 야드에서 예정 순서와 자리별 배치 상태를 확인합니다.',
        '기록에서 변경 이력과 조건별 비교 결과를 열람합니다.',
    ),
}


def _cell_text(page: access.Page, role: str) -> str:
    if access.visible_in_menu(page, (role,)):
        return _ROLE_ACTIONS.get((page.key, role), '읽기')
    if role in page.roles:
        return '읽기(메뉴 숨김)'
    return '—'


def role_table() -> dict:
    roles = tuple(access.PERSONAS.items())
    rows = []
    for page in access.MENU_PAGES:
        cells = [
            dict(
                role=role,
                in_menu=access.visible_in_menu(page, (role,)),
                can_open=role in page.roles,
                text=_cell_text(page, role),
            )
            for role, _label in roles
        ]
        rows.append(dict(page=page, cells=cells))
    return dict(roles=roles, rows=rows)


def role_cards(user_roles: Iterable[str]) -> list[dict]:
    held = set(user_roles)
    order = tuple(access.PERSONAS)
    sorted_roles = sorted(order, key=lambda role: (role not in held, order.index(role)))
    return [
        dict(
            role=role,
            title=access.PERSONAS[role],
            is_mine=role in held,
            steps=_ROLE_STEPS[role],
            menus=[page.label for page in access.MENU_PAGES if access.visible_in_menu(page, (role,))],
        )
        for role in sorted_roles
    ]
