from datetime import timedelta

from django import forms as django_forms
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from harbor_web import forms, services
from harbor_web.auth import has_role, require_roles
from harbor_web.trip_proposals import trip_status
from harbor_web.models import DepartureIntent, FieldConfirmation, FieldEpisode, Order
from harbor_web.views import form_error, form_page, page, paginate


class ReassignForm(django_forms.Form):
    expected_version = django_forms.IntegerField(widget=django_forms.HiddenInput, min_value=1)
    assigned_to = django_forms.ModelChoiceField(label='새 현장 확인 담당', queryset=get_user_model().objects.filter(is_active=True, groups__name='field').distinct())


def episode_status(episode, *, now=None):
    confirmation = FieldConfirmation.objects.filter(episode=episode).select_related('actor').order_by('-pk').first()
    departure = DepartureIntent.objects.filter(episode=episode).first()
    current = (confirmation is not None and episode.state == 'CONFIRMED' and confirmation.outcome == 'CLEAR'
               and episode.version == confirmation.version + 1 and confirmation.actor_id == episode.assigned_to_id
               and confirmation.actor.is_active and has_role(confirmation.actor, 'field'))
    fresh = bool(current and timedelta(0) <= (now or timezone.now()) - confirmation.observed_at <= timedelta(minutes=5))
    needs_new = not departure and episode.state in {'CONFIRMED', 'MISMATCH', 'UNABLE'} and not fresh
    if departure:
        label, action, reason = '출발 전제 기록됨', 'Fleet 인계 원본 확인', '출발 전제만 기록했습니다. 같은 차량·화물·회차를 Fleet에 인계하고 실제 차량 실행을 확인하세요.'
    elif fresh:
        label, action, reason = '현장 확인 유효', '출발 전제 검토', '현장 CLEAR 확인이 유효합니다. 관제 담당자가 원본과 차량을 확인하세요.'
    elif needs_new:
        label, action, reason = '새 확인 필요', '새 회차로 재확인 배정', '확인 시각이 지났거나 현재 대상·결과가 일치하지 않습니다. 새 회차에서 현장을 다시 확인해야 합니다.'
    elif episode.state == 'REASSIGNED':
        label, action, reason = '이전 회차', '현재 배정 확인', '이 회차는 교체되었습니다. 새 배정의 현장 확인을 사용하세요.'
    else:
        label, action, reason = '현장 확인 대기', '배정·확인 진행 보기', '배정된 현장 담당자의 직접 확인을 기다립니다.'
    return dict(trip=trip_status(departure) if departure else None, confirmation=confirmation, departure=departure, confirmation_fresh=fresh,
                confirmation_expires_at=confirmation.observed_at + timedelta(minutes=5) if confirmation else None,
                needs_new_confirmation=bool(needs_new), can_prepare_departure=fresh and not departure,
                can_reassign=not departure and episode.state != 'REASSIGNED',
                status_label=label, action=action, reason=reason, href=f'/field/{episode.pk}/')


@require_roles('operator', 'admin', 'field')
@require_http_methods(['GET'])
def field_list(request):
    items = FieldEpisode.objects.select_related('order__cargo', 'assigned_to').order_by('-id')
    if not has_role(request.user, 'operator', 'admin'):
        items = items.filter(assigned_to=request.user)
    state = request.GET.get('state', '')
    if state in {'OPEN', 'CONFIRMED', 'MISMATCH', 'UNABLE', 'REASSIGNED'}:
        items = items.filter(state=state)
    return page(request, 'field.html', section='field', state=state, **paginate(request, items))


@require_roles('operator')
@require_http_methods(['GET', 'POST'])
def field_new(request):
    initial = None
    if 'order' in request.GET:
        if not request.GET['order'].isascii() or not request.GET['order'].isdigit():
            raise Http404('Invalid inbound order.')
        initial = {'order': get_object_or_404(Order, pk=request.GET['order'], kind='INBOUND')}
    def save(data):
        return services.assign_episode(request.user, order_id=data['order'].pk, vehicle_id=data['vehicle_id'], assigned_to=data['assigned_to'])
    return form_page(request, form_type=forms.EpisodeForm, title='현장 확인 배정', section='field', description='입고 요청·화물·차량을 특정한 공급 인계 회차를 만듭니다.', save=save, destination=lambda obj: f'/field/{obj.pk}/', initial=initial)


@require_roles('operator', 'admin', 'field')
@require_http_methods(['GET', 'POST'])
def field_detail(request, pk):
    episode = get_object_or_404(FieldEpisode.objects.select_related('order__cargo', 'assigned_to'), pk=pk)
    if not has_role(request.user, 'operator', 'admin') and episode.assigned_to_id != request.user.pk:
        raise PermissionDenied
    form = forms.ConfirmForm(initial={'expected_version': episode.version, 'observed_at': timezone.localtime()})
    reassign = ReassignForm(initial={'expected_version': episode.version, 'assigned_to': episode.assigned_to_id})
    if request.method == 'POST':
        action = request.POST.get('action')
        try:
            if action == 'confirm':
                form = forms.ConfirmForm(request.POST)
                if form.is_valid():
                    services.confirm_episode(request.user, episode_id=pk, **form.cleaned_data)
                    messages.success(request, '이 회차의 확인 결과를 저장했습니다. 차량 실행 허가와는 별개입니다.')
                    return redirect(f'/field/{pk}/')
            elif action == 'reassign':
                reassign = ReassignForm(request.POST)
                if reassign.is_valid():
                    new = services.reassign_episode(request.user, episode_id=pk, **reassign.cleaned_data)
                    return redirect(f'/field/{new.pk}/')
            elif action == 'departure' and request.POST.get('confirmation_id', '').isdigit():
                services.prepare_departure(request.user, episode_id=pk, confirmation_id=int(request.POST['confirmation_id']))
                messages.info(request, '차량과 회차에 결속된 출발 전제를 기록했습니다. Fleet 운영자에게 같은 차량과 원본을 인계하세요.')
                return redirect(f'/field/{pk}/')
            else:
                return render(request, 'error.html', dict(reason='지원하지 않는 현장 확인 요청입니다.', return_url=f'/field/{pk}/'), status=400)
        except services.Conflict:
            return render(request, 'error.html', dict(heading='현장 배정이 변경되었습니다', reason='이전 화면의 확인을 재사용하지 마세요. 현재 대상과 회차를 다시 확인하세요.', return_url='/field/'), status=409)
        except ValidationError as error:
            form_error(reassign if action == 'reassign' else form, error)
    return page(request, 'field_detail.html', section='field', episode=episode, form=form, reassign_form=reassign,
                confirmations=FieldConfirmation.objects.filter(episode=episode).select_related('actor').order_by('-id'),
                **episode_status(episode),
                can_confirm=has_role(request.user, 'field') and episode.assigned_to_id == request.user.pk and episode.state == 'OPEN')
