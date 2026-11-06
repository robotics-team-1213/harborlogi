from django import forms
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db.models import Q
from harbor_web.models import Cargo, Condition, Measurement, Observation, Order


class FootprintForm(forms.Form):
    length_cells = forms.TypedChoiceField(label='점유 크기', coerce=int,
        choices=[(n, f'{n} × 1칸') for n in (1, 2, 3)], initial=1)


class CargoForm(forms.ModelForm):
    length_cells = FootprintForm.base_fields['length_cells']
    def clean_code(self):
        code = self.cleaned_data['code']
        if Cargo.objects.filter(code=code).exists():
            raise forms.ValidationError('이미 등록된 화물 ID입니다. 기존 화물의 연결된 요청을 확인하거나 다른 ID를 입력하세요.')
        return code

    class Meta:
        model = Cargo
        fields = ['code', 'name', 'weight_kg', 'volume_m3', 'destination']
        labels = dict(code='화물 ID', name='화물 이름', weight_kg='중량 (kg)', volume_m3='부피 (m³)', destination='목적지')
        help_texts = dict(code='등록 뒤에도 원래 ID를 유지합니다. 실제 위치는 관측으로 확인합니다.')


class FixedOrderCheckbox(forms.CheckboxInput):
    """Tie policy as one box: checked (the default) keeps FIXED, unchecked sends ANY.

    Explicit FIXED/ANY values from older forms and scripts are still read as given.
    """
    def __init__(self, attrs=None):
        super().__init__(attrs, check_test=lambda value: value in (None, '', 'FIXED'))

    def format_value(self, value):
        return 'FIXED'

    def value_from_datadict(self, data, files, name):
        value = data.get(name)
        if value in ('FIXED', 'ANY'):
            return value
        return 'FIXED' if value else 'ANY'


TIE_POLICY_LABEL = '그룹 안 순서 고정'
TIE_POLICY_HELP = '기본은 고정입니다. 해제하면 같은 그룹의 요청을 순서와 관계없이 처리할 수 있습니다.'


class OrderForm(forms.ModelForm):
    make_first = forms.BooleanField(label='맨 앞에 등록', required=False,
        help_text='반출 대기열 맨 앞에 새 그룹으로 넣고, 기존 요청은 한 그룹씩 뒤로 미룹니다.')

    class Meta:
        model = Order
        fields = ['cargo', 'kind', 'title', 'priority_group', 'sequence', 'tie_policy', 'note']
        labels = dict(cargo='화물', kind='요청 종류', title='요청 제목', priority_group='우선순위 그룹', sequence='그룹 안 순서', tie_policy=TIE_POLICY_LABEL, note='요청 메모')
        widgets = {'note': forms.Textarea(attrs={'rows': 3}), 'tie_policy': FixedOrderCheckbox(),
                   'title': forms.TextInput(attrs={'placeholder': '예: 본선 1차 선적분'})}
        help_texts = dict(title='목록에서 화물 ID 대신 먼저 보이는 이름입니다. 비우면 화물 이름을 씁니다.',
                          priority_group='작은 그룹부터 검토합니다.', sequence='순서 고정 그룹에서 작은 값부터 처리합니다.',
                          tie_policy=TIE_POLICY_HELP)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['kind'].choices = [('INBOUND', '입고'), ('OUTBOUND', '반출')]
        self.fields['tie_policy'].choices = [('FIXED', '순서 고정'), ('ANY', '같은 그룹 안에서 선택 허용')]

    def clean(self):
        data = super().clean()
        # The DB check constraint is the backstop; this gives the user a plain field error first.
        if data.get('kind') in ('INBOUND', 'OUTBOUND') and not data.get('cargo') and 'cargo' not in self.errors:
            self.add_error('cargo', '입고·반출 요청은 화물을 선택해야 합니다.')
        return data


class PriorityForm(forms.Form):
    expected_version = forms.IntegerField(widget=forms.HiddenInput)
    title = forms.CharField(label='요청 제목', max_length=80, required=False)
    priority_group = forms.IntegerField(label='우선순위 그룹', min_value=1)
    sequence = forms.IntegerField(label='그룹 안 순서', min_value=1)
    tie_policy = forms.ChoiceField(label=TIE_POLICY_LABEL, choices=[('FIXED', '순서 고정'), ('ANY', '그룹 안 선택 허용')],
                                   widget=FixedOrderCheckbox(), help_text=TIE_POLICY_HELP)
    note = forms.CharField(label='변경 메모', max_length=500, required=False, widget=forms.Textarea(attrs={'rows': 3}))

    def __init__(self, *args, **kwargs):
        kwargs.setdefault('auto_id', 'id_priority_%s')  # pages render several forms; ids must stay unique
        super().__init__(*args, **kwargs)


class PlanForm(forms.Form):
    expected_version = forms.IntegerField(widget=forms.HiddenInput)
    observation = forms.ModelChoiceField(label='검토할 관측 원본', queryset=Observation.objects.none(), empty_label='관측을 선택하세요')
    automatic = forms.BooleanField(label='지시·관측이 바뀌면 자동 재계획', required=False, initial=True,
        help_text='선택한 출처와 설치 기준에 결속합니다. 계산 결과만 새 버전으로 보존하며 이미 인계한 실행은 바꾸지 않습니다. 해제 후 계산하면 자동 재계획을 끕니다.')

    def __init__(self, *args, **kwargs):
        review_observation = kwargs.pop('review_observation', None)
        kwargs.setdefault('auto_id', 'id_plan_%s')
        super().__init__(*args, **kwargs)
        recent = Observation.objects.order_by('-id').values('pk')[:100]
        selected = Q(pk=review_observation.pk) if review_observation else Q(pk__in=[])
        self.fields['observation'].queryset = Observation.objects.filter(Q(pk__in=recent) | selected).select_related('condition').order_by('-id')
        self.fields['observation'].label_from_instance = lambda item: item.display_label


class ConditionForm(forms.ModelForm):
    class Meta:
        model = Condition
        fields = ['name', 'calibration_ref', 'payload']
        labels = dict(name='설치 기준 이름', calibration_ref='보정·검증 기록 참조', payload='이동·도달 조건 (JSON)')
        widgets = {'payload': forms.Textarea(attrs={'rows': 15, 'spellcheck': 'false'})}
        help_texts = {'payload': 'outbound_stand, temporary_bays, allowed_moves, max_observation_age_seconds, conveyor, arm_reach가 필요합니다. yard_layout으로 행·열·높이와 각 칸 ID의 행·열을 지정합니다. 등록은 실행 허가나 도달 검증을 뜻하지 않습니다.'}


class ObservationForm(forms.ModelForm):
    class Meta:
        model = Observation
        fields = ['condition', 'source_id', 'source_revision', 'observed_at', 'provenance', 'payload']
        labels = dict(condition='설치 조건', source_id='원본 출처', source_revision='출처 revision', observed_at='관측 시각', provenance='자료 구분', payload='관측 원본 (JSON)')
        widgets = {'observed_at': forms.DateTimeInput(attrs={'type': 'datetime-local'}, format='%Y-%m-%dT%H:%M'), 'payload': forms.Textarea(attrs={'rows': 12, 'spellcheck': 'false'})}
        help_texts = {'payload': '기존 관측은 bays와 holdings를 사용하며 cargo 순서는 아래→위입니다. 다칸 관측은 schema=grid-placement-v1과 observed_areas, placements, holdings를 사용하고 각 배치에 footprint_revision을 지정합니다. 이 수동 등록은 인증된 운영 관측으로 승격되지 않습니다.'}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['provenance'].choices = [('DECLARED', '수동 등록 자료 · 인증 미확인')]
        if settings.MODE != 'live':
            self.fields['provenance'].choices.append(('SYNTHETIC', '합성 시험 자료'))


class EpisodeForm(forms.Form):
    order = forms.ModelChoiceField(label='입고 요청', queryset=Order.objects.none())
    vehicle_id = forms.CharField(label='대상 차량 ID', max_length=96)
    assigned_to = forms.ModelChoiceField(label='현장 확인 담당', queryset=get_user_model().objects.none())

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['order'].queryset = Order.objects.filter(kind='INBOUND').select_related('cargo')
        self.fields['order'].label_from_instance = lambda item: f'요청 #{item.pk} · {item.cargo.code}'
        self.fields['assigned_to'].queryset = get_user_model().objects.filter(is_active=True, groups__name='field').distinct()


class ConfirmForm(forms.Form):
    expected_version = forms.IntegerField(widget=forms.HiddenInput)
    observed_at = forms.DateTimeField(label='직접 확인한 시각', widget=forms.DateTimeInput(attrs={'type': 'datetime-local'}, format='%Y-%m-%dT%H:%M'), help_text='현재 시각 기준 5분 이내의 직접 확인만 제출하세요.')
    outcome = forms.ChoiceField(label='확인 결과', choices=[('CLEAR', 'S가 이 차량의 적재면을 비웠음'), ('MISMATCH', '대상 또는 상태가 일치하지 않음'), ('UNABLE', '현재 확인할 수 없음')], widget=forms.RadioSelect)

    def __init__(self, *args, **kwargs):
        kwargs.setdefault('auto_id', 'id_confirm_%s')
        super().__init__(*args, **kwargs)


class MeasurementForm(forms.ModelForm):
    class Meta:
        model = Measurement
        fields = ['label', 'scenario', 'conditions_ref', 'initial_layout_ref', 'information_ref', 'method', 'baseline_reach_verified', 'provenance', 'source_ref', 'vehicle_transfers', 'human_interventions', 'elapsed_seconds']
        labels = dict(label='측정 자료 이름', scenario='시나리오 ID', conditions_ref='실행 조건 참조', initial_layout_ref='초기 배치 참조', information_ref='정보 제공 시각·버전 참조', method='비교 방식', baseline_reach_verified='한 팔 기준의 실제 도달 가능성을 검증했음', provenance='자료 구분', source_ref='측정 원본 참조', vehicle_transfers='추가 차량 이송·왕복 수', human_interventions='사람 개입 수', elapsed_seconds='소요 시간 (초)')
        help_texts = {'vehicle_transfers': '미측정이면 비워 두세요. 실제 0회와 구분합니다.'}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['method'].choices = [('BASELINE', '한 팔 기준'), ('COLLABORATIVE', '협업 방식')]
        self.fields['provenance'].choices = [('MEASURED', '원본이 있는 측정 자료')]
        if settings.MODE != 'live':
            self.fields['provenance'].choices.append(('SYNTHETIC', '합성 시험 자료'))
