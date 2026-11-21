"""Read-only exceptions from existing operations facts and field assignments."""
from django.shortcuts import render
from django.views.decorators.http import require_http_methods

from harbor_web import access
from harbor_web.auth import require_roles, roles
from harbor_web.intake_pages import intake_alert_items
from harbor_web.models import FieldEpisode
from harbor_web.operations import operations_context
from harbor_web.views import selected_observation
from harbor_web.work_state import work_state


ATTENTION_TEXT = {
    '관측 없음': ('야드 현황이 없습니다', '야드 연결 상태를 확인해야 합니다.', '야드 보기'),
    '관측 유효 시간 밖': ('야드 현황을 새로 받아야 합니다', '유효 시간이 지나 최신 야드 현황이 필요합니다.', '현황 선택'),
    '같은 출처에 새 관측 있음': ('더 최근 야드 현황이 있습니다', '새 야드 현황으로 계획을 다시 살펴보세요.', '현황 선택'),
    'HOLD 계획': ('계획이 막힘', '현재 계획에서 진행할 수 없는 이유를 살펴보세요.', '계획 보기'),
    '재검토로 이동 집계 제외': ('계획 재검토 필요', '업무나 순서가 바뀌어 계획을 다시 살펴봐야 합니다.', '계획 보기'),
}
COMMAND_TEXT = {
    'UNKNOWN': 'ROSY 응답을 확인하지 못했습니다. 자동으로 다시 보내지 않습니다.',
    'BLOCKED': '설치 조건이나 야드 현황, 복구 조건을 확인해야 합니다.',
    'CANCEL_PENDING': '취소 결과와 실제 정지 여부를 확인해야 합니다.',
}


def alerts_context(request):
    held = roles(request.user)
    can_read = bool(held & access.READ)
    alerts = []
    if can_read:
        observation = selected_observation(request, default_latest=True)
        operations = operations_context(observation)
        for item in operations['attention']:
            href = item['href']
            if href.startswith('/work/'):
                # Work state owns problem classification; dispatching alone is normal progress.
                if not item['label'].startswith('완료 인계 · '):
                    continue
                title, detail, action = ('완료 기록 확인 필요',
                    '완료 기록에서 반출 이동을 찾지 못했습니다. 작업 기록을 살펴보세요.', '작업 보기')
            else:
                title, detail, action = ATTENTION_TEXT.get(item['label'],
                    ('업무 상태 확인 필요', '현재 업무 상태를 살펴보세요.', '상세 보기'))
            if href.startswith('#'):
                href = f'/?observation={observation.pk}{href}'
            alerts.append(dict(title=title, detail=detail, count=item['count'], href=href, action=action))
        for row in operations['handoffs']:
            command = row['command']
            if work_state(command) != '처리할 것':
                continue
            cargo = command.order.cargo.code if command.order.cargo_id else '자동 재배치'
            alerts.append(dict(title=f'{cargo} · 작업 확인 필요',
                detail=COMMAND_TEXT.get(command.state, '작업 상태를 확인해야 합니다.'),
                count=1, href=f'/work/{command.pk}/', action='작업 보기'))
    episodes = FieldEpisode.objects.filter(state='OPEN').select_related('order__cargo').order_by('created_at', 'pk')
    can_open_field = bool(held & {'operator', 'admin', 'field'})
    if not held & {'operator', 'admin'}:
        if can_read:
            can_open_field = False  # Readers can see the alert, but field details require assignment.
        else:
            episodes = episodes.filter(assigned_to=request.user)
    for episode in episodes:
        cargo = episode.order.cargo.code if episode.order.cargo_id else '자동 재배치'
        alerts.append(dict(title=f'{cargo} · 적재면 확인 대기', detail=f'차량 {episode.vehicle_id}', count=1,
            href=f'/field/{episode.pk}/' if can_open_field else None, action='배정 보기'))
    intake_status = ''
    if can_read:
        intake_items, intake_status = intake_alert_items(can_read)
        alerts.extend(intake_items)
    return dict(alerts=alerts, alert_total=sum(item['count'] for item in alerts), intake_status=intake_status)


@require_roles(*access.EVERYONE)
@require_http_methods(['GET'])
def alerts(request):
    return render(request, 'alerts.html', dict(section='alerts', **alerts_context(request)))
