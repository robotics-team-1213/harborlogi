"""Scenario test sheets (KAN-118): every inbound and outbound event is registered up front from one workbook.

Sheet 「초기 배치」 is the yard at the start, one 1×3 container per slot and level (행 · 칸 · 층; 칸 counts the slots of a
row, so a 3×6 yard has two). The sheet is checked against the latest grid condition. Sheet 「이벤트」 lists inbound and
outbound requests from top to bottom with their tier. Importing creates the cargo, a synthetic start observation
and every request at once; inbound requests are planned by the algorithm in event order and wait for a
person's decision (ADR 0012). Synthetic data only: the import is closed in live mode.
"""
import hashlib
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from harbor_web import services
from harbor_web.models import Audit, Cargo, CargoFootprint, Condition, Order
from harbor_web.order_queue import completed_order_ids
from harbor_web.xlsx import SheetError, read_xlsx, write_xlsx

LAYOUT_SHEET, EVENT_SHEET = '초기 배치', '이벤트'
LAYOUT_COLUMNS = ('화물 ID', '화물 이름', '행', '칸', '층')
OPTIONAL_COLUMNS = ('칸',)  # older sheets without it put every container in the first slot
EVENT_COLUMNS = ('순번', '종류', '화물 ID', '화물 이름', '단계', '제목')
KINDS = {'입고': 'INBOUND', '반출': 'OUTBOUND'}
TIERS = {'긴급': 1, '우선': 2, '일반': 3}
MAX_AUTOMATIC_INBOUND = 16  # automatic planning is bounded per site (plan_service.make_plan)
DEFAULT_DIMS = dict(rows=3, slots=1, levels=3, width=3)  # a 3×3 yard: one 1×3 container fills a row


def site_dims(condition=None):
    """Rows, slots per row, levels and slot width (cells) of the grid condition a scenario is placed on."""
    if condition is None:
        condition = Condition.objects.filter(payload__has_key='spatial_areas').order_by('-pk').first()
    layout = condition.payload.get('yard_layout') if condition else None
    if not layout:
        return dict(DEFAULT_DIMS)
    width = condition.payload.get('slot_cells') or layout['columns']
    return dict(rows=layout['rows'], slots=max(1, layout['columns'] // width), levels=layout['levels'], width=width)


def _spot(row, slot, level, dims):
    return f'{row}행 {slot}칸 {level}층' if dims['slots'] > 1 else f'{row}행 {level}층'


def template_workbook(dims=None):
    dims = dims or DEFAULT_DIMS
    beside = ['C4', '옆 자리 화물', '1', '2', '1'] if dims['slots'] > 1 else ['C4', '중앙 화물', '2', '1', '1']
    slots = f' · 칸 1~{dims["slots"]}' if dims['slots'] > 1 else ''
    return write_xlsx({
        LAYOUT_SHEET: [list(LAYOUT_COLUMNS), ['C1', '우선 반출 화물', '1', '1', '1'], ['C2', '상단 화물', '1', '1', '2'], beside],
        EVENT_SHEET: [list(EVENT_COLUMNS), ['1', '입고', 'N1', '새 입고 화물', '일반', '1차 입고'],
                      ['2', '반출', 'C1', '', '우선', '본선 1차'], ['3', '반출', 'C2', '', '우선', ''],
                      ['4', '입고', 'N2', '새 입고 화물 2', '긴급', '']],
        '설명': [['시트', '설명'], [LAYOUT_SHEET, f'시나리오 시작 때 야드. 1×3 컨테이너 하나가 한 자리(행 1~{dims["rows"]}{slots})의 '
                                                 f'한 층(층 1~{dims["levels"]})을 차지합니다. 아래층부터 채웁니다. 칸을 비우면 1칸입니다.'],
               [EVENT_SHEET, '위에서 아래 순서로 등록합니다. 종류는 입고 · 반출, 단계는 긴급 · 우선 · 일반입니다. 반출할 화물은 초기 배치나 앞선 입고에 있어야 합니다.'],
               ['처리', '모든 요청을 한 번에 등록합니다. 입고 위치는 알고리즘이 계산하고, 사람이 결정해야 넘어갑니다.']],
    })


def _table(rows, columns, sheet, errors, optional=()):
    if not rows:
        errors.append(f'「{sheet}」 시트가 비어 있습니다.')
        return []
    header = rows[0]
    missing = [name for name in columns if name not in header and name not in optional]
    if missing:
        errors.append(f'「{sheet}」 첫 행에 {", ".join(missing)} 열이 필요합니다.')
        return []
    index = {name: header.index(name) for name in columns if name in header}
    return [(number, {name: (row[index[name]] if name in index and index[name] < len(row) else '') for name in columns})
            for number, row in enumerate(rows[1:], start=2) if any(row)]


def _integer(text, low, high):
    return int(text) if text.isdigit() and low <= int(text) <= high else None


def parse_workbook(data, dims=None):
    """(scenario, errors). Every problem is reported with its sheet and row so the sheet can be fixed at once.

    ``dims`` is the yard the sheet must fit (site_dims); the default is a 3×3 yard with one slot per row.
    """
    dims = dims or DEFAULT_DIMS
    try:
        sheets = read_xlsx(data)
    except SheetError as error:
        return None, [str(error)]
    errors = []
    for name in (LAYOUT_SHEET, EVENT_SHEET):
        if name not in sheets:
            errors.append(f'「{name}」 시트가 없습니다. 양식을 내려받아 쓰세요.')
    if errors:
        return None, errors
    layout, events, seen, slots = [], [], set(), set()
    for number, row in _table(sheets[LAYOUT_SHEET], LAYOUT_COLUMNS, LAYOUT_SHEET, errors, OPTIONAL_COLUMNS):
        code, row_no, level = row['화물 ID'], _integer(row['행'], 1, dims['rows']), _integer(row['층'], 1, dims['levels'])
        slot = _integer(row['칸'] or '1', 1, dims['slots'])
        where = f'「{LAYOUT_SHEET}」 {number}행'
        if not code:
            errors.append(f'{where}: 화물 ID가 비어 있습니다.')
        elif code in seen:
            errors.append(f'{where}: 화물 {code}가 두 번 나옵니다.')
        if row_no is None or level is None:
            errors.append(f'{where}: 행은 1~{dims["rows"]}, 층은 1~{dims["levels"]}입니다.')
        if slot is None:
            errors.append(f'{where}: 칸은 1~{dims["slots"]}입니다.')
        if None not in (row_no, slot, level):
            if (row_no, slot, level) in slots:
                errors.append(f'{where}: {_spot(row_no, slot, level, dims)}에 이미 화물이 있습니다.')
            slots.add((row_no, slot, level))
        seen.add(code)
        layout.append(dict(code=code, name=row['화물 이름'] or code, row=row_no, slot=slot, level=level))
    for item in layout:
        if None not in (item['row'], item['slot'], item['level']) and item['level'] > 1 \
                and (item['row'], item['slot'], item['level'] - 1) not in slots:
            errors.append(f'「{LAYOUT_SHEET}」 {item["code"]}: {_spot(item["row"], item["slot"], item["level"] - 1, dims)}이 비어 있어 받칠 수 없습니다.')
    present, inbound = {item['code'] for item in layout}, 0
    for number, row in _table(sheets[EVENT_SHEET], EVENT_COLUMNS, EVENT_SHEET, errors):
        where = f'「{EVENT_SHEET}」 {number}행'
        kind, code, tier = KINDS.get(row['종류']), row['화물 ID'], TIERS.get(row['단계'] or '일반')
        if kind is None:
            errors.append(f'{where}: 종류는 입고 또는 반출입니다.')
        if tier is None:
            errors.append(f'{where}: 단계는 긴급 · 우선 · 일반입니다.')
        if not code:
            errors.append(f'{where}: 화물 ID가 비어 있습니다.')
        elif kind == 'INBOUND':
            inbound += 1
            if code in present:
                errors.append(f'{where}: {code}는 이미 야드에 있어 입고할 수 없습니다.')
            present.add(code)
        elif kind == 'OUTBOUND' and code not in present:
            errors.append(f'{where}: {code}는 초기 배치나 앞선 입고에 없어 반출할 수 없습니다.')
        events.append(dict(order=_integer(row['순번'], 0, 10 ** 6), kind=kind, code=code, name=row['화물 이름'] or code,
                           tier=tier, title=row['제목'][:80], row=number))
    if inbound > MAX_AUTOMATIC_INBOUND:
        errors.append(f'입고는 시나리오 하나에 {MAX_AUTOMATIC_INBOUND}건까지입니다(자동 계획 한도).')
    if not events:
        errors.append(f'「{EVENT_SHEET}」에 이벤트가 없습니다.')
    events.sort(key=lambda event: (event['order'] is None, event['order'] or 0, event['row']))
    return (None if errors else dict(layout=layout, events=events, dims=dims, digest=hashlib.sha256(data).hexdigest())), errors


def _cargo(actor, code, name):
    cargo = Cargo.objects.filter(code=code).first()
    if cargo is None:
        return services.register_cargo(actor, code=code, name=name, weight_kg=Decimal('1.000'), volume_m3=Decimal('0.0120'),
                                       destination='컨베이어 · 시나리오', length_cells=3)
    footprint = CargoFootprint.objects.filter(cargo=cargo).order_by('-pk').first()
    if footprint is None or footprint.length_cells != 3:
        services.register_footprint(actor, cargo_id=cargo.pk, length_cells=3)
    return cargo


def import_scenario(actor, scenario, *, name):
    """Register the whole scenario in one transaction; returns (observation, orders)."""
    if settings.MODE == 'live':
        raise ValidationError('시나리오 등록은 개발 · 시험 환경에서만 엽니다.')
    services._role(actor, 'admin')
    services._role(actor, 'operator')
    condition = Condition.objects.filter(payload__has_key='spatial_areas').order_by('-pk').first()
    if condition is None:
        raise ValidationError('격자 야드 설치 기준(spatial_areas)이 없습니다. 설정에서 먼저 등록하세요.')
    dims = site_dims(condition)
    if scenario.get('dims', DEFAULT_DIMS) != dims:
        raise ValidationError('파일을 검사한 뒤 설치 기준이 바뀌었습니다. 파일을 다시 올리세요.')
    codes = {event['code'] for event in scenario['events']}
    if Order.objects.filter(cargo__code__in=codes).exclude(kind='REARRANGE').exclude(pk__in=completed_order_ids()).exists():
        raise ValidationError('이 시나리오의 화물에 아직 끝나지 않은 요청이 있습니다. 새 화물 ID로 시나리오를 만드세요.')
    with transaction.atomic():
        placements = []
        for item in scenario['layout']:
            cargo = _cargo(actor, item['code'], item['name'])
            footprint = CargoFootprint.objects.filter(cargo=cargo).order_by('-pk').first()
            placements.append(dict(cargo_id=item['code'], area_id='yard', row=item['row'], column=1 + dims['width'] * (item['slot'] - 1),
                                   level=item['level'], orientation=0, footprint_revision=footprint.pk))
        now = timezone.now()
        observation = services.record_observation(actor, source_id=f'scenario-{name}'[:96], source_revision=now.isoformat(),
            observed_at=now, provenance='SYNTHETIC', condition=condition,
            payload=dict(schema='grid-placement-v1', placements=placements, holdings=[],
                         observed_areas=['yard', *(area['area_id'] for area in condition.payload['spatial_areas'])]))
        orders = []
        for event in scenario['events']:
            cargo = _cargo(actor, event['code'], event['name'])
            group, sequence = services.back_of_tier(event['tier'], event['kind'])
            orders.append(services.create_order(actor, cargo=cargo, kind=event['kind'], priority_group=group, sequence=sequence,
                                                tie_policy='FIXED', title=event['title'], note=f'시나리오 {name} · 이벤트 {len(orders) + 1}'))
        for order in orders:  # the algorithm places inbound in event order; a person decides each one
            if order.kind == 'INBOUND':
                order.refresh_from_db()
                services.make_plan(actor, order_id=order.pk, expected_version=order.version, observation_id=observation.pk, automatic=True)
        Audit.objects.create(actor=actor, action='scenario.import', object_type='Observation', object_id=str(observation.pk),
                             detail=dict(name=name, digest=scenario['digest'], layout=len(scenario['layout']),
                                         inbound=sum(o.kind == 'INBOUND' for o in orders), outbound=sum(o.kind == 'OUTBOUND' for o in orders),
                                         orders=[o.pk for o in orders]))
    return observation, orders
