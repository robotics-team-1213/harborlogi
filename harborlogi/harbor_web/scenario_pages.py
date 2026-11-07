"""Scenario workbook upload (KAN-118): template download, validation messages, and the import history."""
import re

from django import forms
from django.conf import settings
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import HttpResponse
from django.shortcuts import redirect
from django.views.decorators.http import require_http_methods

from harbor_web import scenario_import, services
from harbor_web.auth import has_role, require_roles
from harbor_web.models import Audit
from harbor_web.views import page


class ScenarioForm(forms.Form):
    name = forms.CharField(label='시나리오 이름', max_length=40, help_text='예: S2-긴급입고. 관측 출처 이름과 요청 메모에 남습니다.')
    workbook = forms.FileField(label='시나리오 파일 (.xlsx)', help_text='양식의 「초기 배치」 · 「이벤트」 시트를 채워 올리세요. 2 MB 이하.')

    def clean_name(self):
        name = self.cleaned_data['name'].strip()
        if not re.fullmatch(r'[0-9A-Za-z가-힣._-]+', name):
            raise ValidationError('이름은 한글 · 영문 · 숫자 · . _ - 만 씁니다.')
        return name


@require_roles('operator', 'admin')
@require_http_methods(['GET', 'POST'])
def scenarios(request):
    form, errors = ScenarioForm(request.POST or None, request.FILES or None), []
    live, dims = settings.MODE == 'live', scenario_import.site_dims()
    if request.method == 'POST' and not live and form.is_valid():
        scenario, errors = scenario_import.parse_workbook(form.cleaned_data['workbook'].read(), dims)
        if scenario is not None:
            try:
                observation, orders = scenario_import.import_scenario(request.user, scenario, name=form.cleaned_data['name'])
            except (ValidationError, services.Conflict) as error:
                errors = getattr(error, 'messages', [str(error)])
            else:
                inbound = sum(order.kind == 'INBOUND' for order in orders)
                messages.success(request, f'시나리오를 등록했습니다: 초기 배치 관측 #{observation.pk}, 요청 {len(orders)}건'
                                          f'(입고 {inbound} · 반출 {len(orders) - inbound}). 입고 위치는 알고리즘이 계산했고, 결정을 기다립니다.')
                return redirect(f'/?observation={observation.pk}')
    history = Audit.objects.filter(action='scenario.import').select_related('actor').order_by('-pk')[:20]
    return page(request, 'scenarios.html', section='cargo', form=form, errors=errors, history=history, live=live, dims=dims,
                can_import=has_role(request.user, 'admin') and has_role(request.user, 'operator'))


@require_roles('operator', 'admin')
@require_http_methods(['GET'])
def scenario_template(request):
    response = HttpResponse(scenario_import.template_workbook(scenario_import.site_dims()),
                            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = 'attachment; filename="harbor-scenario-template.xlsx"'
    return response
