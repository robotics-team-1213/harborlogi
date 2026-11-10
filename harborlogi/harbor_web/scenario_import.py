"""Scenario test sheets (KAN-118): every inbound and outbound event is registered up front from one workbook.

Sheet 「초기 배치」 is the yard at the start, one 1×3 container per slot and level (행 · 칸 · 층; 칸 counts the slots of a
row, so a 3×6 yard has two). The sheet is checked against the latest grid condition. Sheet 「이벤트」 lists inbound and
outbound requests from top to bottom with their tier. Importing creates the cargo, a synthetic start observation
and every request at once; inbound requests are planned by the algorithm in event order and wait for a
person's decision (ADR 0012). Synthetic data only: the import is closed in live mode.

Sheet 「입고 일정」 replaces 「이벤트」 for an inbound-only schedule (ADR 0031): one row per container with the time its
Pinky arrives, the Pinky, the container and its tier. Rows of one Pinky at one time are one delivery, top container
first. The tier then ranks the container for placement and rearrangement (CargoPriority).
"""
import hashlib
from datetime import datetime, timedelta
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from harbor_web import inbound_schedule, services
from harbor_web.coordination import serialized
from harbor_web.models import Audit, Cargo, CargoFootprint, CargoPriority, Condition, Order, ScheduledInbound
from harbor_web.order_queue import completed_order_ids
from harbor_web.xlsx import SheetError, read_xlsx, write_xlsx

LAYOUT_SHEET, EVENT_SHEET, SCHEDULE_SHEET = '초기 배치', '이벤트', '입고 일정'
LAYOUT_COLUMNS = ('화물 ID', '화물 이름', '행', '칸', '층')
OPTIONAL_COLUMNS = ('칸', '종류')  # older sheets without 칸 put every container in the first slot; 종류 goes with 입고 일정
SCHEDULE_COLUMNS = ('시각', '핑키', '컨테이너', '종류')
EVENT_COLUMNS = ('순번', '종류', '화물 ID', '화물 이름', '단계', '제목')
KINDS = {'입고': 'INBOUND', '반출': 'OUTBOUND'}
TIERS = {'긴급': 1, '우선': 2, '일반': 3}
MAX_AUTOMATIC_INBOUND = 16  # automatic planning is bounded per site (plan_service.make_plan)
MAX_UNDATED_SPAN = timedelta(hours=18)  # undated times further apart than this probably cross midnight
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
    """The inbound schedule template (ADR 0031): the start yard with tiers and four containers on three Pinky trips.

    Pinky 1 brings N1 on top of N2, Pinky 2 follows, and the 09:40 trip leaves a gap for a rearrangement; C1 (긴급)
    under C2 (일반) gives that rearrangement something to fix.
    """
    dims = dims or DEFAULT_DIMS
    beside = ['C4', '옆 자리 화물', '1', '2', '1', '우선'] if dims['slots'] > 1 else ['C4', '중앙 화물', '2', '1', '1', '우선']
    slots = f' · 칸 1~{dims["slots"]}' if dims['slots'] > 1 else ''
    gap, per = int(inbound_schedule.MIN_GAP.total_seconds() // 60), int(inbound_schedule.INBOUND_PER_CONTAINER.total_seconds() // 60)
    return write_xlsx({
        LAYOUT_SHEET: [list(LAYOUT_COLUMNS) + ['종류'], ['C1', '긴급 화물', '1', '1', '1', '긴급'], ['C2', '상단 화물', '1', '1', '2', '일반'], beside],
        SCHEDULE_SHEET: [list(SCHEDULE_COLUMNS), ['09:00', '1', 'N1', '일반'], ['09:00', '1', 'N2', '긴급'],
                         ['09:05', '2', 'N3', '우선'], ['09:40', '1', 'N4', '일반']],
        '설명': [['시트', '설명'], [LAYOUT_SHEET, f'시나리오 시작 때 야드. 1×3 컨테이너 하나가 한 자리(행 1~{dims["rows"]}{slots})의 '
                                                 f'한 층(층 1~{dims["levels"]})을 차지합니다. 아래층부터 채웁니다. 칸을 비우면 1칸, 종류를 비우면 일반입니다.'],
               [SCHEDULE_SHEET, '시각은 핑키가 도착하는 시각(9:05 · 09:05:30 · 2026-11-10 09:05)입니다. 날짜가 없으면 등록한 날입니다. '
                                '같은 시각 · 같은 핑키는 한 번 배송이고, 핑키 위에 실린 순서대로 맨 위를 먼저 적습니다. 그 순서로 입고합니다.'],
               ['종류', '긴급 · 우선 · 일반. 반출 요청이 없는 일정이라 입고 자리와 재배치는 이 단계를 꺼낼 순서로 씁니다.'],
               ['자동 재배치', f'앞 배송이 끝날 예상(컨테이너 1개 {per}분)과 다음 도착 사이가 {gap}분 이상이면 그때, 그리고 마지막 입고 뒤에 예정합니다.'],
               ['처리', '모든 입고를 한 번에 등록합니다. 입고 위치는 알고리즘이 계산하고, 사람이 결정해야 넘어갑니다.']],
    })


def event_template_workbook(dims=None):
    """The earlier template with inbound and outbound events in 순번 order (ADR 0025)."""
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
    # isascii() too: '²'.isdigit() is True but int('²') raises; the length bound keeps int() under its 4300-digit limit.
    return int(text) if text.isascii() and text.isdigit() and len(text) <= 9 and low <= int(text) <= high else None


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
    mode = 'schedule' if SCHEDULE_SHEET in sheets else 'events'
    if LAYOUT_SHEET not in sheets:
        errors.append(f'「{LAYOUT_SHEET}」 시트가 없습니다. 양식을 내려받아 쓰세요.')
    if SCHEDULE_SHEET in sheets and EVENT_SHEET in sheets:
        errors.append(f'「{SCHEDULE_SHEET}」과 「{EVENT_SHEET}」 중 하나만 둡니다.')
    elif SCHEDULE_SHEET not in sheets and EVENT_SHEET not in sheets:
        errors.append(f'「{SCHEDULE_SHEET}」 시트가 없습니다. 양식을 내려받아 쓰세요.')
    if errors:
        return None, errors
    layout, events, seen, slots = [], [], set(), set()
    for number, row in _table(sheets[LAYOUT_SHEET], LAYOUT_COLUMNS + ('종류',), LAYOUT_SHEET, errors, OPTIONAL_COLUMNS):
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
        tier = TIERS.get(row['종류'] or '일반')
        if tier is None:
            errors.append(f'{where}: 종류는 긴급 · 우선 · 일반입니다.')
        elif row['종류'] and mode == 'events':
            errors.append(f'{where}: 종류는 「{SCHEDULE_SHEET}」 시트와 함께 씁니다.')
        seen.add(code)
        layout.append(dict(code=code, name=row['화물 이름'] or code, row=row_no, slot=slot, level=level, tier=tier))
    for item in layout:
        if None not in (item['row'], item['slot'], item['level']) and item['level'] > 1 \
                and (item['row'], item['slot'], item['level'] - 1) not in slots:
            errors.append(f'「{LAYOUT_SHEET}」 {item["code"]}: {_spot(item["row"], item["slot"], item["level"] - 1, dims)}이 비어 있어 받칠 수 없습니다.')
    schedule = _schedule(sheets[SCHEDULE_SHEET], errors) if mode == 'schedule' else []
    events = [dict(order=None, kind='INBOUND', code=item['code'], name=item['code'], tier=item['tier'], title='', row=item['row'])
              for item in schedule]
    for number, row in (_table(sheets[EVENT_SHEET], EVENT_COLUMNS, EVENT_SHEET, errors) if mode == 'events' else ()):
        where = f'「{EVENT_SHEET}」 {number}행'
        kind, code, tier = KINDS.get(row['종류']), row['화물 ID'], TIERS.get(row['단계'] or '일반')
        if kind is None:
            errors.append(f'{where}: 종류는 입고 또는 반출입니다.')
        if tier is None:
            errors.append(f'{where}: 단계는 긴급 · 우선 · 일반입니다.')
        if not code:
            errors.append(f'{where}: 화물 ID가 비어 있습니다.')
        order = _integer(row['순번'], 0, 10 ** 6)
        if row['순번'] and order is None:  # blank sorts last; anything else must be a number
            errors.append(f'{where}: 순번은 0~1000000 정수이거나 비워 둡니다.')
        events.append(dict(order=order, kind=kind, code=code, name=row['화물 이름'] or code,
                           tier=tier, title=row['제목'][:80], row=number))
    sheet = SCHEDULE_SHEET if mode == 'schedule' else EVENT_SHEET
    if not events:
        errors.append(f'「{sheet}」에 입고가 없습니다.' if mode == 'schedule' else f'「{EVENT_SHEET}」에 이벤트가 없습니다.')
    if mode == 'events':  # a schedule is already in inbound order (_schedule)
        events.sort(key=lambda event: (event['order'] is None, event['order'] or 0, event['row']))
    present, gone, inbound = {item['code'] for item in layout}, set(), 0
    for event in events:  # presence follows the registration order (순번), not the sheet's row order
        where, code = f'「{sheet}」 {event["row"]}행', event['code']
        if not code:
            continue
        if event['kind'] == 'INBOUND':
            inbound += 1
            if code in gone:
                errors.append(f'{where}: {code}는 앞선 반출로 나간 화물이라 이 시나리오에서 다시 입고할 수 없습니다.')
            elif code in present:
                errors.append(f'{where}: {code}는 이미 야드에 있어 입고할 수 없습니다.')
            present.add(code)
        elif event['kind'] == 'OUTBOUND':
            if code in gone:
                errors.append(f'{where}: {code}는 앞선 반출로 이미 나가 다시 반출할 수 없습니다.')
            elif code not in present:
                errors.append(f'{where}: {code}는 초기 배치나 앞선 입고에 없어 반출할 수 없습니다.')
            present.discard(code)
            gone.add(code)
    if inbound > MAX_AUTOMATIC_INBOUND:
        errors.append(f'입고는 시나리오 하나에 {MAX_AUTOMATIC_INBOUND}건까지입니다(자동 계획 한도).')
    return (None if errors else dict(layout=layout, events=events, dims=dims, digest=hashlib.sha256(data).hexdigest(),
                                     mode=mode, schedule=schedule)), errors


def _schedule(rows, errors):
    """「입고 일정」 rows in inbound order: deliveries by arrival time then sheet order, each one top container first.

    A time without a date is today's local time. Duplicate containers are reported once and left out.
    """
    today, items, seen = timezone.localdate(), [], set()
    for number, row in _table(rows, SCHEDULE_COLUMNS, SCHEDULE_SHEET, errors, ('종류',)):
        where, code, vehicle = f'「{SCHEDULE_SHEET}」 {number}행', row['컨테이너'], row['핑키']
        parsed, tier = inbound_schedule.parse_time(row['시각']), TIERS.get(row['종류'] or '일반')
        if parsed is None:
            errors.append(f'{where}: 시각은 9:05 · 09:05:30 · 2026-11-10 09:05 처럼 적습니다.')
        if not vehicle:
            errors.append(f'{where}: 핑키 번호가 비어 있습니다.')
        elif len(vehicle) > 32 or any(ord(c) < 32 or ord(c) == 127 for c in vehicle):
            errors.append(f'{where}: 핑키 번호는 제어 문자 없이 32자까지입니다.')
        if tier is None:
            errors.append(f'{where}: 종류는 긴급 · 우선 · 일반입니다.')
        if not code:
            errors.append(f'{where}: 컨테이너 번호가 비어 있습니다.')
            continue
        if code in seen:
            errors.append(f'{where}: 컨테이너 {code}가 두 번 나옵니다.')
            continue
        seen.add(code)
        at = timezone.make_aware(datetime.combine(parsed[0] or today, parsed[1])) if parsed else None
        items.append(dict(code=code, tier=tier, at=at, vehicle=vehicle, row=number, dated=bool(parsed and parsed[0])))
    if any(item['at'] is None for item in items):
        return items  # sheet order: the file is refused, and the presence checks still name every row
    undated = [item['at'] for item in items if not item['dated']]
    if undated and max(undated) - min(undated) > MAX_UNDATED_SPAN:  # 23:50 then 00:10 would sort the wrong way
        errors.append(f'「{SCHEDULE_SHEET}」: 날짜 없는 시각이 {MAX_UNDATED_SPAN.seconds // 3600}시간 넘게 벌어져 있습니다. '
                      '자정을 넘는 일정은 2026-11-10 23:50 처럼 날짜를 함께 적습니다.')
    first_row = {}
    for item in items:
        first_row.setdefault((item['at'], item['vehicle']), item['row'])
    items.sort(key=lambda item: (item['at'], first_row[item['at'], item['vehicle']], item['row']))
    size = {}
    for item in items:
        key = (item['at'], item['vehicle'])
        size[key] = size.get(key, 0) + 1
        item['stack_position'] = size[key]
    for item in items:
        item['delivery_size'] = size[item['at'], item['vehicle']]
    return items


def _cargo(actor, code, name):
    cargo = Cargo.objects.filter(code=code).first()
    if cargo is None:
        return services.register_cargo(actor, code=code, name=name, weight_kg=Decimal('1.000'), volume_m3=Decimal('0.0120'),
                                       destination='컨베이어 · 시나리오', length_cells=3)
    footprint = CargoFootprint.objects.filter(cargo=cargo).order_by('-pk').first()
    if footprint is None or footprint.length_cells != 3:
        services.register_footprint(actor, cargo_id=cargo.pk, length_cells=3)
    return cargo


def _record(item):
    item.full_clean()
    item.save()


@serialized
def import_scenario(actor, scenario, *, name):
    """Register the whole scenario in one transaction; returns (observation, orders).

    queue_lock first, the SQLite write lock inside it: the same order as every other mutating service, so the
    "no unfinished request" check and the creation below cannot interleave with another request.
    """
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
    inbound = sum(event['kind'] == 'INBOUND' for event in scenario['events'])
    used = Order.objects.filter(auto_replan=True).count()  # make_plan's site-wide bound counts earlier scenarios too
    if used + inbound > MAX_AUTOMATIC_INBOUND:
        raise ValidationError(f'자동 계획 한도 {MAX_AUTOMATIC_INBOUND}건 중 {used}건을 이전 요청이 쓰고 있어 입고 {inbound}건을 더 등록할 수 없습니다. '
                              '끝난 요청의 자동 계획을 끄고 다시 올리세요.')
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
            payload=dict(schema='grid-placement-v1', observed_areas=[a.area_id for a in services.map_areas(condition.payload)],
                         placements=placements, holdings=[]))
        orders, schedule = [], scenario.get('mode') == 'schedule'
        for event in scenario['events']:
            cargo = _cargo(actor, event['code'], event['name'])
            group, sequence = services.back_of_tier(event['tier'], event['kind'])
            note = f'시나리오 {name} · {"입고 일정" if schedule else "이벤트"} {len(orders) + 1}'
            orders.append(services.create_order(actor, cargo=cargo, kind=event['kind'], priority_group=group, sequence=sequence,
                                                tie_policy='FIXED', title=event['title'], note=note))
        if schedule:  # before planning: the tiers rank the containers the placements are computed for (ADR 0031)
            for order, item in zip(orders, scenario['schedule']):
                _record(ScheduledInbound(created_by=actor, order=order, start=observation, expected_at=item['at'],
                        vehicle=item['vehicle'], stack_position=item['stack_position'], delivery_size=item['delivery_size']))
            for item in scenario['layout'] + scenario['schedule']:
                _record(CargoPriority(created_by=actor, cargo=Cargo.objects.get(code=item['code']), start=observation, tier=item['tier']))
        # Before planning: the latest import decides which schedule and tiers are active (inbound_schedule.active_start).
        Audit.objects.create(actor=actor, action='scenario.import', object_type='Observation', object_id=str(observation.pk),
                             detail=dict(name=name, digest=scenario['digest'], layout=len(scenario['layout']),
                                         inbound=sum(o.kind == 'INBOUND' for o in orders), outbound=sum(o.kind == 'OUTBOUND' for o in orders),
                                         orders=[o.pk for o in orders], mode=scenario.get('mode', 'events')))
        for order in orders:  # the algorithm places inbound in event order; a person decides each one
            if order.kind == 'INBOUND':
                order.refresh_from_db()
                services.make_plan(actor, order_id=order.pk, expected_version=order.version, observation_id=observation.pk, automatic=True)
    return observation, orders
