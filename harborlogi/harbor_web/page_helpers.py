from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render

from harbor_web import services
from harbor_web.models import Observation


def page(request, template, **context):
    return render(request, template, context)


def paginate(request, items):
    query = request.GET.copy()
    query.pop('page', None)
    return dict(page=Paginator(items, 25).get_page(request.GET.get('page')), query=query.urlencode())


def selected_observation(request, *, default_latest=False):
    if 'observation' in request.GET:
        value = request.GET['observation']
        if not value.isascii() or not value.isdigit():
            raise Http404('Invalid observation.')
        return get_object_or_404(Observation.objects.select_related('condition'), pk=value)
    return Observation.objects.select_related('condition').order_by('-pk').first() if default_latest else None


def form_error(form, error):
    for message in error.messages:
        form.add_error(None, message)


def form_page(request, *, form_type, title, section, save, destination, description='', initial=None):
    form = form_type(request.POST or None, initial=initial)
    if request.method == 'POST' and form.is_valid():
        try:
            obj = save(form.cleaned_data)
        except ValidationError as error:
            form_error(form, error)
        except services.Conflict:
            return render(request, 'error.html', dict(heading='기준이 변경되었습니다', reason='입력할 때와 현재 원본이 다릅니다. 최신 자료를 다시 확인하세요.', return_url=request.path), status=409)
        else:
            messages.success(request, '저장했습니다. 원래 자료와 변경 기록을 확인할 수 있습니다.')
            return redirect(destination(obj) if callable(destination) else destination)
    return page(request, 'form.html', form=form, title=title, section=section, description=description)
