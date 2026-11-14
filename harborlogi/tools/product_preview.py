"""Start an isolated, loopback-only product preview with private generated access."""
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys


def main():
    root = Path(__file__).resolve().parents[1]
    folder = root / 'runs/product-preview'
    folder.mkdir(parents=True, exist_ok=True)
    access = folder / 'access.json'
    if not access.exists():
        values = dict(secret=secrets.token_urlsafe(64), username='harbor-preview', password=secrets.token_urlsafe(24))
        descriptor = os.open(access, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump(values, stream)
    else:
        values = json.loads(access.read_text(encoding='utf-8'))
    os.environ.update(HARBOR_WEB_MODE='development', HARBOR_WEB_SECRET_KEY=values['secret'], HARBOR_WEB_DATABASE=str(folder / 'product.sqlite3'))
    # The preview never inherits an operational Fleet credential or ledger path.
    for name in ('LEDGER_PATH', 'FLEET_ORIGIN', 'FLEET_TOKEN', 'FLEET_PRINCIPAL', 'BINDINGS_PATH', 'WEB_ALLOW_SIMULATION'):
        os.environ.pop('HARBOR_' + name, None)
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'harbor_web.settings')
    os.chdir(root)
    sys.path.insert(0, str(root))
    import django
    django.setup()
    from django.core.management import call_command
    from django.contrib.auth import get_user_model
    from django.contrib.auth.models import Group
    from django.utils import timezone
    from harbor_web import services
    from harbor_web.models import Cargo, CargoFootprint, Condition, FieldEpisode, Order
    from harbor.ownership import WriterLease
    with WriterLease(folder / 'product.sqlite3'):
        call_command('migrate', interactive=False, verbosity=0)
        call_command('bootstrap_roles', verbosity=0)
        user, created = get_user_model().objects.get_or_create(username=values['username'], defaults={'is_staff': True})
        if created:
            user.set_password(values['password'])
            user.save()
            user.groups.add(*Group.objects.filter(name__in=['viewer', 'operator', 'field', 'admin']))
        for code, name in [('C1', '우선 반출 화물'), ('C2', '상단 재취급 화물'), ('C3', '공급 인계 확인 화물'),
                           ('C4', '옆 자리 보관 화물'), ('C5', '하단 보관 화물'), ('C6', '중단 보관 화물'), ('C7', '2행 둘째 자리 화물'),
                           ('C8', '3행 하단 화물'), ('C9', '3행 상단 화물')]:
            length = 3  # every site container is 1×3 and fills one slot (KAN-115)
            if not Cargo.objects.filter(code=code).exists():
                services.register_cargo(user, code=code, name=name, weight_kg=2, volume_m3='0.012', destination='컨베이어 · 합성 시험', length_cells=length)
            cargo = Cargo.objects.get(code=code)
            footprint = CargoFootprint.objects.filter(cargo=cargo).order_by('-pk').first()
            if footprint is None or footprint.length_cells != length:
                services.register_footprint(user, cargo_id=cargo.pk, length_cells=length)
        # The site map (KAN-115): a 3×6 yard with two 1×3 slots per row, a 3-slot temp tray on each side, the conveyor.
        layout = json.loads((root / 'config/yard-layout-3x6.json').read_text(encoding='utf-8'))
        policy = json.loads((root / 'config/spatial-policy-3x6.json').read_text(encoding='utf-8'))
        policy['yard_layout'] = layout
        name = '3×6×3 사이트 맵 · 합성 관측 검토용'
        condition = Condition.objects.filter(name=name).order_by('-id').first()
        if condition is None or condition.payload != policy:
            condition = services.register_condition(user, name=name, calibration_ref='synthetic-only-not-field-calibration', payload=policy)
        now = timezone.now()
        placements = []
        for code, row, column, level in [('C1', 1, 1, 1), ('C2', 1, 1, 2), ('C4', 1, 4, 1), ('C5', 2, 1, 1), ('C6', 2, 1, 2),
                                         ('C7', 2, 4, 1), ('C8', 3, 4, 1), ('C9', 3, 4, 2)]:
            footprint = CargoFootprint.objects.filter(cargo__code=code).order_by('-pk').first()
            placements.append(dict(cargo_id=code, area_id='yard', row=row, column=column, level=level,
                                   orientation=0, footprint_revision=footprint.pk))
        observation = services.record_observation(user, source_id='synthetic-preview-camera', source_revision=now.isoformat(), observed_at=now, provenance='SYNTHETIC', condition=condition,
            payload=dict(schema='grid-placement-v1', observed_areas=['yard', 'temp_a', 'temp_b', 'stand'], placements=placements, holdings=[]))
        order = Order.objects.filter(cargo__code='C1', kind='OUTBOUND').first()
        if order is None:
            order = services.create_order(user, cargo=Cargo.objects.get(code='C1'), kind='OUTBOUND', priority_group=2, sequence=1, tie_policy='FIXED', note='변경된 반출 지시를 검토하는 합성 사례')
        if not Order.objects.filter(cargo__code='C2', kind='OUTBOUND').exists():  # same tier as C1: the tier shows a recommended order
            services.create_order(user, cargo=Cargo.objects.get(code='C2'), kind='OUTBOUND', priority_group=2, sequence=2, tie_policy='FIXED', note='같은 우선 단계의 두 번째 반출 · 추천 시나리오 검토용')
        inbound = Order.objects.filter(cargo__code='C3', kind='INBOUND').first()
        if inbound is None:
            inbound = services.create_order(user, cargo=Cargo.objects.get(code='C3'), kind='INBOUND', priority_group=3, sequence=1, tie_policy='FIXED', note='현장 확인 화면 검토용 합성 요청')
        if not FieldEpisode.objects.filter(order=inbound, state='OPEN').exists():
            services.assign_episode(user, order_id=inbound.pk, vehicle_id='합성 차량 P1', assigned_to=user)
        order.refresh_from_db()  # plan last, so the plan matches the queue it is shown with
        services.make_plan(user, order_id=order.pk, expected_version=order.version, observation_id=observation.pk)
    print('Product preview: http://127.0.0.1:8060/ · synthetic data; Fleet disconnected.', flush=True)
    print('Private access file: runs/product-preview/access.json (do not publish).', flush=True)
    return subprocess.call([sys.executable, '-m', 'harbor_web', '--port', '8060'], cwd=root, env=os.environ)


if __name__ == '__main__':
    raise SystemExit(main())
