"""Read-only intake hold listing and UNREADABLE attestation review (WP14, ADR 0029)."""
import os
from pathlib import Path

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from harbor.port_events import IntakeLedger
from harbor_web import access
from harbor_web.auth import has_role, require_roles
from harbor_web.models import Audit, Cargo, Observation
from harbor_web.topview_proxy import topview_context
from harbor_web.yard_rows import yard_rows

REASON_META = {
    'UNREADABLE': (
        '반입 번호 못 읽음',
        '카메라에서 화물 번호를 읽지 못했습니다. Harbor 야드 평면과 카메라 화면을 비교해 기명 확인하세요.',
        '확인하기',
    ),
    'MISMATCH': (
        'ROSY 인식이 다름',
        '예고된 화물과 실제 반입 인식이 다릅니다. 비교 화면에서 보기만 할 수 있습니다.',
        '보기',
    ),
    'LOST': (
        'ROSY 사건이 안 옴',
        '정해진 시간 안에 반입 사건이 오지 않았습니다. 비교 화면에서 보기만 할 수 있습니다.',
        '보기',
    ),
    'DAMAGED': (
        '화물 손상 보고',
        '손상 보고로 반입이 보류되었습니다. 비교 화면에서 보기만 할 수 있습니다.',
        '보기',
    ),
}


def ledger_path():
    return os.environ.get('HARBOR_LEDGER_PATH', '').strip()


def read_holds():
    path = ledger_path()
    if not path or not Path(path).exists():
        return [], '연동 전'
    return IntakeLedger(path).holds(), ''


def intake_alert_items(can_read):
    holds, status = read_holds()
    items = []
    for row in holds:
        ref = row['object_ref']
        title_suffix, detail, action = REASON_META.get(
            row['reason'],
            ('반입 확인 필요', '반입 상태를 비교 화면에서 확인하세요.', '보기'),
        )
        items.append(
            dict(
                title=f'{ref} · {title_suffix}',
                detail=detail,
                count=1,
                href=reverse('intake_review', args=[ref]) if can_read else None,
                action=action,
                intake_ref=ref,
                can_attest=row['can_attest'],
                reason=row['reason'],
            )
        )
    return items, status


@require_roles(*access.READ)
@require_http_methods(['GET', 'POST'])
def intake_review(request, object_ref):
    path = ledger_path()
    configured = bool(path and Path(path).exists())
    is_fragment = request.GET.get('fragment') == '1'
    template = 'intake_compare_body.html' if is_fragment else 'intake_compare.html'

    if not configured:
        if request.method == 'POST':
            return render(
                request,
                'error.html',
                dict(heading='ROSY 반입 장부 연동 전', reason='반입 장부가 연결되지 않았습니다.', return_url='/alerts/'),
                status=503,
            )
        return render(
            request,
            template,
            dict(
                section='alerts',
                object_ref=object_ref,
                ledger_configured=False,
                status_text='연동 전',
                yard=None,
                topview=topview_context(),
                cargo=Cargo.objects.filter(code=object_ref).first(),
                can_operate=has_role(request.user, 'operator'),
                is_fragment=is_fragment,
            ),
        )

    ledger = IntakeLedger(path)
    try:
        row = ledger.read(object_ref)
    except ValueError as exc:
        raise Http404('반입 기록을 찾을 수 없습니다.') from exc

    reason = row['hold_reason'] or row['attention']
    can_attest = (
        row['attention'] == 'UNREADABLE'
        and row['hold_reason'] is None
        and row['state'] not in ('HOLD', 'COMPLETED')
    )
    can_operate = has_role(request.user, 'operator')

    if request.method == 'POST':
        if not can_operate:
            raise PermissionDenied
        if not can_attest:
            return render(
                request,
                'error.html',
                dict(
                    heading='기명 확인 불가',
                    reason='번호를 읽지 못한 반입 건만 기명 확인으로 진행할 수 있습니다.',
                    return_url=reverse('intake_review', args=[object_ref]),
                ),
                status=400,
            )
        if request.POST.get('confirm') != '1':
            return render(
                request,
                'error.html',
                dict(
                    heading='확인 체크 필요',
                    reason='실제 화물과 일치함을 확인 체크한 뒤 확정하세요.',
                    return_url=reverse('intake_review', args=[object_ref]),
                ),
                status=400,
            )
        changed = ledger.attest_received(object_ref, attested_by=request.user.username, now=timezone.now())
        if changed:
            Audit.objects.create(
                actor=request.user,
                action='intake.attested_received',
                object_type='Intake',
                object_id=object_ref,
                detail={'attested_by': request.user.username, 'reason': reason},
            )
        messages.success(request, f'{object_ref} 반입을 기명 확인했습니다.')
        return redirect('/alerts/' if request.POST.get('next') == 'alerts' else reverse('intake_review', args=[object_ref]))

    observation = Observation.objects.select_related('condition').order_by('-pk').first()
    reason_label, reason_detail, _ = REASON_META.get(
        reason,
        ('반입 확인', '반입 상태를 확인하세요.', '보기'),
    )
    return render(
        request,
        template,
        dict(
            section='alerts',
            object_ref=object_ref,
            ledger_configured=True,
            row=row,
            reason=reason,
            reason_label=reason_label,
            reason_detail=reason_detail,
            can_attest=can_attest,
            can_operate=can_operate,
            cargo=Cargo.objects.filter(code=object_ref).first(),
            yard=yard_rows(observation),
            observation=observation,
            topview=topview_context(),
            is_fragment=is_fragment,
        ),
    )
