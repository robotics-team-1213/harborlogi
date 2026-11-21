from django import forms as django_forms
from django.db.models import Q
from django.http import Http404, HttpResponseBadRequest
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_http_methods
from harbor_web import forms, services
from harbor_web.auth import require_roles
from harbor_web.record_text import OBJECT_LABELS, record_text
from harbor_web.models import Audit, Command, DepartureIntent, FieldConfirmation, FieldEpisode, Measurement, Order, Plan
from harbor_web.views import form_page, page, paginate


class ComparisonForm(django_forms.Form):
    left = django_forms.ModelChoiceField(label='기준 자료 A', queryset=Measurement.objects.none())
    right = django_forms.ModelChoiceField(label='비교 자료 B', queryset=Measurement.objects.none())

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in ('left', 'right'):
            self.fields[name].queryset = Measurement.objects.order_by('-id')
            self.fields[name].label_from_instance = lambda item: f'{item.label} · {item.scenario} · {item.provenance}'


@require_roles('viewer', 'operator', 'admin')
@require_http_methods(['GET'])
def records(request):
    q = request.GET.get('q', '')[:96]
    object_type = request.GET.get('object_type', '')
    object_types = ('Cargo', 'CargoFootprint', 'Condition', 'Observation', 'Order', 'Plan', 'Command',
                    'FieldEpisode', 'FieldConfirmation', 'DepartureIntent', 'Measurement')
    if object_type and object_type not in object_types:
        return HttpResponseBadRequest('지원하지 않는 기록 종류입니다.')
    order_scope = None
    items = Audit.objects.select_related('actor').order_by('-id')
    if 'order' in request.GET:
        value = request.GET['order']
        if not value.isascii() or not value.isdigit():
            raise Http404('Invalid order.')
        order_scope = get_object_or_404(Order.objects.select_related('cargo'), pk=value)
        episodes = FieldEpisode.objects.filter(order=order_scope)
        scopes = [(Order, [order_scope.pk]), (Plan, Plan.objects.filter(order=order_scope).values_list('pk', flat=True)),
                  (Command, Command.objects.filter(order=order_scope).values_list('pk', flat=True)),
                  (FieldEpisode, episodes.values_list('pk', flat=True)),
                  (FieldConfirmation, FieldConfirmation.objects.filter(episode__in=episodes).values_list('pk', flat=True)),
                  (DepartureIntent, DepartureIntent.objects.filter(episode__in=episodes).values_list('pk', flat=True))]
        related = Q(pk__in=[])
        for model, ids in scopes:
            related |= Q(object_type=model.__name__, object_id__in=[str(pk) for pk in ids])
        items = items.filter(related)
    if object_type:
        items = items.filter(object_type=object_type)
    if q:
        items = items.filter(object_id=q) if object_type else items.none()
    pagination = paginate(request, items)
    for item in pagination['page']:
        item.sentence = record_text(item.action)
        item.target_label = OBJECT_LABELS.get(item.object_type, '기타')
    return page(request, 'records.html', section='records', q=q, object_type=object_type,
                object_types=[(value, OBJECT_LABELS[value]) for value in object_types], order_scope=order_scope,
                scope_warning='번호로 찾으려면 대상 종류를 함께 선택하세요.' if q and not object_type else '',
                **pagination)


@require_roles('operator')
@require_http_methods(['GET', 'POST'])
def measurement_new(request):
    return form_page(request, form_type=forms.MeasurementForm, title='측정 자료 등록', section='records', description='같은 조건을 비교할 수 있도록 원본 참조를 보존합니다. 실제 미측정 값은 비워 두세요.', save=lambda data: services.record_measurement(request.user, **data), destination='/records/compare/')


@require_roles('viewer', 'operator', 'admin')
@require_http_methods(['GET'])
def comparison(request):
    form = ComparisonForm(request.GET or None)
    result = None
    if form.is_bound and form.is_valid():
        result = services.compare_measurements(form.cleaned_data['left'], form.cleaned_data['right'])
    return page(request, 'comparison.html', section='records', form=form, comparison=result)
