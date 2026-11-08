"""Standing R2 authorization screen: state, on/off with values, automatic vs other work."""
from django import forms
from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import redirect
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from harbor_web import rearrangement, services
from harbor_web.auth import has_role, require_roles
from harbor_web.models import Audit, Observation, Plan, RearrangeAuthorization
from harbor_web.views import page


class AuthorizationForm(forms.Form):
    k = forms.IntegerField(label='k (이동 수 ≤ k × 개선량)', min_value=1, max_value=10, initial=2)
    max_moves = forms.IntegerField(label='한 계획의 최대 이동 수', min_value=1, max_value=12, initial=6)
    hours = forms.IntegerField(label='유효 시간 (시간)', min_value=1, max_value=72, initial=8)
    daily_limit = forms.IntegerField(label='하루 최대 자동 실행 횟수', min_value=1, max_value=1000, initial=20)


@require_roles('viewer', 'operator', 'admin')
@require_http_methods(['GET', 'POST'])
def rearrangement_page(request):
    form = AuthorizationForm(request.POST or None)
    if request.method == 'POST':
        if not has_role(request.user, 'operator'):
            raise PermissionDenied
        action = request.POST.get('action')
        try:
            if action == 'disable':
                rearrangement.disable_authorization(request.user)
            elif action == 'request':
                observation = Observation.objects.order_by('-pk').first()
                if observation is None:
                    raise ValidationError('관측이 없습니다.')
                plan = rearrangement.request_rearrangement(request.user, observation_id=observation.pk)
                return redirect(f'/plans/{plan.pk}/')
            elif form.is_valid():
                data = form.cleaned_data
                rearrangement.enable_authorization(request.user, k=data['k'], max_moves=data['max_moves'],
                    valid_until=timezone.now() + timezone.timedelta(hours=data['hours']), daily_limit=data['daily_limit'])
            else:
                raise ValidationError('입력값을 확인하세요.')
            messages.success(request, '저장했습니다.')
            return redirect('/rearrangement/')
        except (ValidationError, services.Conflict) as error:
            for message in getattr(error, 'messages', [str(error)]):
                form.add_error(None, message)
    plans = Plan.objects.filter(order__kind='REARRANGE').select_related('order').order_by('-pk')[:25]
    rows = [dict(plan=plan, record=plan.result.get('rearrangement', {}), command=getattr(plan, 'command', None)) for plan in plans]
    return page(request, 'rearrangement.html', section='rearrangement', form=form, rows=rows,
        authorization=rearrangement.current_authorization(), history=RearrangeAuthorization.objects.order_by('-pk')[:10],
        holds=Audit.objects.filter(action=rearrangement.HOLD_ACTION).order_by('-pk')[:10])
