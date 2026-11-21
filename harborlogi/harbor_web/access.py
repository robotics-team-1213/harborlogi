"""Single record of which role opens which static page and how the main menu groups them (docs/design/34)."""
from dataclasses import dataclass
from typing import Iterable

PERSONAS = {'operator': '업무 담당', 'field': '현장 확인', 'admin': '설치·운영 관리', 'viewer': '조회'}
NAV_GROUPS = (('main', '주 메뉴'),)


@dataclass(frozen=True)
class Page:
    key: str
    path: str
    label: str
    hint: str
    roles: frozenset
    group: str | None = None
    utility: bool = False
    menu_roles: frozenset | None = None


READ = frozenset({'viewer', 'operator', 'admin'})
OPERATE = frozenset({'operator'})
ADMIN = frozenset({'admin'})
EVERYONE = frozenset({'viewer', 'operator', 'field', 'admin'})

PAGES = (
    Page('overview', '/', '상황판', '지금 상태와 오늘 처리량', READ, 'main'),
    Page('work', '/work/', '작업', '오늘 시간표와 반출 순서', READ, 'main'),
    Page('inventory', '/inventory/', '야드', '자리별 배치와 남은 예정', EVERYONE, 'main'),
    Page('alerts', '/alerts/', '처리할 것', '예외와 현장 확인', EVERYONE, 'main',
         menu_roles=frozenset({'operator', 'field', 'admin'})),
    Page('records', '/records/', '기록', '사건 기록과 시험 비교', READ, 'main'),
    Page('settings', '/settings/', '설정', '계정 역할과 설치 기준', ADMIN, 'main', True),
    Page('workflow', '/workflow/', '도움말', '역할별 할 일 안내', EVERYONE, 'main', True),
    Page('outbound', '/outbound/', '반출 관제', '우선순위·재취급 계획', READ),
    Page('cargo', '/requests/', '화물·요청', '화물 등록과 업무 지시', READ),
    Page('rearrangement', '/rearrangement/', '자동 재배치', '허용 범위와 실행 기록', READ),
    Page('field', '/field/', '현장 확인', '회차별 사실 확인', frozenset({'operator', 'field', 'admin'})),
    Page('cargo_list', '/cargo/', '화물 목록', '', READ),
    Page('cargo_new', '/cargo/new/', '화물 등록', '', OPERATE),
    Page('request_new', '/requests/new/', '요청 등록', '', OPERATE),
    Page('observation_new', '/inventory/import/', '관측 원본 등록', '', ADMIN),
    Page('field_new', '/field/new/', '현장 회차 배정', '', OPERATE),
    Page('measurement_new', '/records/measurements/new/', '측정 등록', '', OPERATE),
    Page('comparison', '/records/compare/', '비교 결과', '', READ),
    Page('condition_new', '/settings/conditions/new/', '설치 기준 등록', '', ADMIN),
    Page('map_new', '/settings/maps/new/', '구역 맵 편집', '', ADMIN),
    Page('integration', '/settings/integration/', 'ROSY 연동 조건', '', ADMIN),
)
MENU_PAGES = tuple(p for p in PAGES if p.group)
_BY_KEY = {p.key: p for p in PAGES}


def page(key: str) -> Page:
    return _BY_KEY[key]


def can_open(user_roles: Iterable[str], key: str) -> bool:
    return bool(page(key).roles.intersection(user_roles))


def visible_in_menu(p: Page, user_roles: Iterable[str]) -> bool:
    return bool((p.menu_roles or p.roles).intersection(user_roles))


def nav_groups(user_roles: Iterable[str]) -> list:
    held = set(user_roles)
    groups = []
    for key, label in NAV_GROUPS:
        items = [p for p in MENU_PAGES if p.group == key and visible_in_menu(p, held)]
        if items:
            groups.append({'key': key, 'label': label, 'items': items})
    return groups


def persona_names(user_roles: Iterable[str]) -> list:
    held = set(user_roles)
    return [name for role, name in PERSONAS.items() if role in held]
