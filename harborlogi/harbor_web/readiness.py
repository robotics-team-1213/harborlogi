"""Production execution readiness gate: every condition must hold, from a fresh stored check (ADR0022)."""
import os
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.utils import timezone

MAX_AGE = timedelta(minutes=10)  # A stored check older than this blocks execution until re-checked.

# key, label, gating, reason code when unmet, owner, next action
CONDITIONS = [
    ('binding', '설치 binding이 계획에 대응', True, 'READINESS_BINDING_REQUIRED', '설치 담당자',
     '검토된 binding을 설치해 이 계획에 대응시키세요.'),
    ('session', 'R 세션이 설정 신원·service 역할과 일치', True, 'READINESS_SESSION_UNMET', 'Harbor 설치 담당자',
     '서비스 신원과 토큰을 확인한 뒤 연결을 다시 확인하세요.'),
    ('arm', 'R ARM 경로 설치', True, 'READINESS_ARM_UNMET', 'ROSY',
     'R에 ARM 작업 경로를 설치하세요.'),
    ('evidence', 'R 물리 근거·재확인 경로 응답', True, 'READINESS_EVIDENCE_UNMET', 'ROSY',
     'R 중계의 physical-evidence·rechecks 경로 착지를 기다리세요.'),
    ('arm_live', '플랫폼이 운영 프로필에서 실물 ARM 실행을 공개', True, 'READINESS_ARM_EXECUTION_UNANNOUNCED', 'ROSY',
     '플랫폼 공개 신호를 요청합니다(ADR0022 「플랫폼에 요청」).'),
    ('producer', '근거 생산자: 사람 확인 켜짐+현장 확인자 1명 이상, 또는 인증된 자동 생산자 공개', True,
     'READINESS_EVIDENCE_PRODUCER_REQUIRED', 'Harbor 관리자',
     '관리자가 사람 확인 생산자를 켜고 현장 확인 권한 사용자를 지정하세요.'),
    ('observation', '계획 관측이 합성이 아님(사람 선언 관측은 사람 확인 생산자가 켜졌을 때만)', True,
     'READINESS_OBSERVATION_UNAUTHENTICATED', 'Harbor',
     '합성 관측으로는 운영 실행하지 않습니다. 사람 선언 관측은 사람 확인 생산자를 켠 동안에만 씁니다(2026-11-04 사용자 결정).'),
    ('proposal', 'R 이송 제안 경로 설치', False, 'READINESS_PROPOSAL_UNMET', 'ROSY',
     '표시 전용입니다. 웹 실행기는 ARM만 쓰므로 이송 단계가 생길 때 관문에 넣습니다.'),
]
REASONS = {code for _, _, _, code, _, _ in CONDITIONS if code} | {'READINESS_CHECK_REQUIRED', 'READINESS_CHECK_STALE'}


def _binding_state(plan):
    from harbor_web.command_binding import _bindings, _bound, _config
    try:
        config = _config()
        if not _bindings(config):
            return 'UNMET'
        _bound(plan, config)
        return 'MET'
    except (ValidationError, ValueError, KeyError, TypeError, OSError):  # unreadable or non-matching keeps the gate shut
        return 'UNMET'


def human_confirmation_ready():
    """Admin switched on named-person confirmation and at least one active field user can give it."""
    from django.contrib.auth import get_user_model
    from harbor_web.models import EvidenceProducerSetting
    setting = EvidenceProducerSetting.objects.first()
    return bool(setting and setting.human_confirmation
                and get_user_model().objects.filter(is_active=True, groups__name='field').exists())


def _observation_state(provenance):
    if provenance == 'SYNTHETIC':
        return 'UNMET'
    if provenance == 'DECLARED':
        return 'MET' if human_confirmation_ready() else 'UNMET'
    return 'MET'


def states(plan, check):
    """condition key -> MET | UNMET | UNKNOWN for one plan against the stored check."""
    found = (check.detail.get('conditions') or {}) if check else {}
    value = dict(
        binding=_binding_state(plan) if plan is not None else 'UNKNOWN',
        session='MET' if check and check.detail.get('session') == 'MATCHED' else 'UNKNOWN' if not check else 'UNMET',
        evidence='MET' if found.get('evidence') == found.get('recheck') == 'MET' else
                 'UNMET' if 'UNMET' in (found.get('evidence'), found.get('recheck')) else 'UNKNOWN',
        # SYNTHETIC never runs in production. A person-DECLARED observation runs only while named-person
        # confirmation is the evidence producer (user decision 2026-11-04); other provenances are authenticated.
        observation='UNKNOWN' if plan is None else _observation_state(plan.observation.provenance))
    value['producer'] = 'MET' if found.get('auto_producer') == 'MET' or human_confirmation_ready() else 'UNMET'
    for key in ('arm', 'arm_live', 'proposal'):
        value[key] = found.get(key) if found.get(key) in {'MET', 'UNMET'} else 'UNKNOWN'
    return value


def first_unmet(plan):
    """The first unmet gating condition's reason code, or '' when production execution may open."""
    from harbor_web.fleet_connection import last_check
    check = last_check()
    value = states(plan, check)
    if value['binding'] != 'MET':
        return 'READINESS_BINDING_REQUIRED'
    if check is None:
        return 'READINESS_CHECK_REQUIRED'
    if not timedelta(0) <= timezone.now() - check.created_at <= MAX_AGE:
        return 'READINESS_CHECK_STALE'
    for key, _, gating, code, _, _ in CONDITIONS:
        if gating and value[key] != 'MET':
            return code
    return ''


def table(check):
    """Rows for the settings page; the binding row needs a plan, so it reports only the installed file."""
    value = states(None, check)
    value['binding'] = 'MET' if os.environ.get('HARBOR_BINDINGS_PATH') and _installed_nonempty() else 'UNMET'
    age = timezone.now() - check.created_at if check else None
    stale = age is None or not timedelta(0) <= age <= MAX_AGE
    return dict(stale=stale, checked_at=check.created_at if check else None, max_minutes=int(MAX_AGE.total_seconds() // 60),
        rows=[dict(label=label, gating=gating, state=value[key], owner=owner, action=action)
              for key, label, gating, _, owner, action in CONDITIONS])


def _installed_nonempty():
    from harbor_web.command_binding import _bindings
    try:
        return bool(_bindings(dict(bindings=os.environ['HARBOR_BINDINGS_PATH'])))
    except (ValidationError, ValueError, KeyError, TypeError, OSError):
        return False
