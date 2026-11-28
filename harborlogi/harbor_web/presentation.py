import re

from django.conf import settings
from harbor_web import access
from harbor_web.auth import roles


COMMAND_LABELS = {
    'PREPARED': '제안 검토 대기', 'DISPATCHING': '제출 대조 필요', 'UNKNOWN': '결과 불명',
    'CANCEL_PENDING': '취소 대조 필요', 'BLOCKED': '인계 보류', 'RECONCILED': '원본 대조됨',
}


def context(request):
    groups = roles(request.user)
    return dict(app_roles=sorted(groups), can_operate='operator' in groups,
                can_admin='admin' in groups, can_field='field' in groups,
                can_read=bool(groups & {'viewer', 'operator', 'admin'}),
                web_mode=settings.MODE, nav_groups=access.nav_groups(groups),
                persona_names=access.persona_names(groups), menu_pages=access.MENU_PAGES)


_REASON_TEXTS = {
    'PLACEMENT_RECOMMENDATION_REQUIRES_EXECUTION_BINDINGS': '반출 순서를 고려한 입고 위치를 계산했습니다. 실제 입고에는 설치 대응과 승인·관측 근거가 필요합니다.',
    'INBOUND_ALREADY_OBSERVED': '이 화물은 이미 관측에 배치되어 있습니다. 새 입고 위치를 중복 생성하지 않습니다.',
    'NO_LEGAL_PLACEMENT': '화물의 전체 크기·지지·도달 조건을 만족하는 보관 위치가 없습니다.',
    'PLACEMENT_SEARCH_LIMIT_REACHED': '배치 후보 검토 한도에 도달했습니다. 위치를 임의로 확정하지 않고 보류합니다.',
    'OBSERVATION_CARGO_LIMIT_REACHED': '관측 화물 수 한도에 도달해 입고 배치를 보류합니다.',
    'SOURCE_CONDITION_CHANGED': '결속한 출처의 설치 기준이 바뀌었습니다. 새 기준의 관측을 직접 선택해 다시 계산하세요.',
    'INCOMING_FOOTPRINT_REQUIRED': '입고 화물의 점유 크기 원본이 필요합니다.',
    'OTHER_COMMAND_REQUIRES_RECONCILIATION': '다른 인계 작업의 실제 실행 상태를 먼저 대조해야 합니다. 자동 계산으로 실행 중인 배치를 추측하지 않습니다.',
    'SPATIAL_OBSERVATION_REQUIRED': '이 화물의 다칸 점유 크기를 확인할 수 있는 공간 관측 원본이 필요합니다.',
    'NO_LEGAL_SPATIAL_PLAN': '전체 점유 면적과 지지 조건을 만족하는 임시 배치·반출·복귀 계획을 찾지 못했습니다.',
    'SPATIAL_SEARCH_LIMIT_REACHED': '공간 계획의 검토 한도에 도달했습니다. 공간이 없다고 단정하지 않고 보류합니다.',
    'PROPOSAL_REQUIRES_ADMISSION_AND_EVIDENCE': '계획을 만들었습니다. 실행 전 설치 대응과 승인·근거 조건을 확인하세요.',
    'PRIORITY_ORDER_BLOCKED': '앞선 반출 지시가 있습니다. 우선순위와 그룹 안 허용 순서를 확인하세요.',
    'INBOUND_PLANNER_NOT_AVAILABLE': '이 원본 형식으로는 입고 배치를 계산할 수 없습니다. 전체 구역·점유 크기가 포함된 공간 관측을 선택하세요. 입력한 요청은 보존됩니다.',
    'REVISION_CONFLICT': '검토한 관측 revision과 현재 기준이 다릅니다.',
    'OBSERVATION_NOT_CURRENT': '관측이 오래되었거나 시각을 확인할 수 없습니다. 최신 원본이 필요합니다.',
    'PHYSICAL_HOLDING_REQUIRES_RECONCILIATION': '들고 있는 화물이 있어 실제 인계 상태 확인이 필요합니다.',
    'OUTBOUND_NOT_OBSERVED': '목표 화물을 관측 원본에서 찾을 수 없습니다.',
    'OUTBOUND_NOT_IN_A_BAY': '목표 화물이 보관 칸에서 확인되지 않았습니다.',
    'OUTBOUND_ALREADY_ON_STAND_REQUIRES_EVIDENCE': '반출대에 있는 화물의 완료 근거를 확인해야 합니다.',
    'OUTBOUND_STAND_FULL': '반출대에 빈자리가 없습니다.',
    'OUTBOUND_MOVE_UNAVAILABLE': '보관 칸에서 반출대로 허용된 이동이 없습니다.',
    'NO_LEGAL_TEMPORARY_BAY': '방해 화물을 둘 유효한 임시 공간이 없습니다.',
    'ARM_REACH_UNVERIFIED': '팔의 도달 가능성 검증 참조가 없습니다.',
    'NO_ARM_REACHES_MOVE': '이 이동을 수행할 수 있는 팔 대응이 없습니다.',
}


def reason_text(code):
    return _REASON_TEXTS.get(code, '원본과 연결 조건을 확인해야 합니다.')


_GATE_TEXTS = {
    'SPATIAL_EXECUTION_BINDINGS_REQUIRED': '공간 계획의 전체 점유 크기·방향·위치에 맞는 설치 대응 검증이 필요합니다.',
    'SPATIAL_OBSERVATION_REQUIRED': '다칸 화물이 등록되었습니다. 전체 점유 위치와 크기 원본을 포함한 공간 관측으로 다시 검토하세요.',
    'EXISTING_ORDER_EXECUTION_REQUIRES_RECONCILIATION': '이 요청에는 이미 실행 인계가 있습니다. 새로 제출하지 말고 원래 작업을 대조하세요.',
    'ORDER_ALREADY_COMPLETED': '업무 장부가 이 요청을 이미 완료했습니다. 같은 업무를 다시 실행하지 않습니다. 완료 기록을 확인하세요.',
    'RESTORE_RECONCILIATION_REQUIRED': '복원한 자료입니다. 원래 실행과 복구 검토가 끝나기 전 새 제출을 보류합니다.',
    'DEPLOYMENT_PROFILE_REQUIRED': '배포 환경과 데이터베이스 기준을 먼저 확인해야 합니다.',
    'EXECUTION_CONFIGURATION_REQUIRED': 'ROSY 연동 설정과 설치된 이동 대응이 필요합니다.',
    'ORIGIN_NOT_ALLOWED': 'Fleet 주소가 배포 서버의 허용 목록에 없습니다. 설정·연동 담당자가 확인해야 합니다.',
    'PLAN_NOT_READY': '계획의 보류 사유를 먼저 해결해야 합니다.',
    'PLAN_QUEUE_CHANGED': '요청 또는 우선순위가 변경되었습니다. 최신 지시로 다시 검토하세요.',
    'OBSERVATION_NOT_FRESH': '검토한 관측이 오래되었습니다. 최신 원본으로 계획을 다시 만드세요.',
    'OBSERVATION_REVISION_CHANGED': '같은 출처의 새 관측이 있습니다. 최신 원본으로 계획을 다시 검토하세요.',
    'AUTHENTICATED_OBSERVATION_AND_EVIDENCE_PRODUCER_REQUIRED': '인증된 관측·업무 근거 연결이 필요합니다. 수동 자료는 운영 실행 근거가 아닙니다.',
    'READINESS_BINDING_REQUIRED': '설치된 binding이 비었거나 이 계획에 대응하지 않습니다. 설치 담당자가 확인해야 합니다.',
    'READINESS_CHECK_REQUIRED': '운영 실행 조건을 점검한 기록이 없습니다. 관리자가 ROSY 연동에서 연결을 확인하세요.',
    'READINESS_CHECK_STALE': '마지막 연결 점검이 오래되었습니다. 관리자가 ROSY 연동에서 다시 점검하세요.',
    'READINESS_SESSION_UNMET': 'R 세션이 설정한 서비스 신원·역할과 일치하지 않습니다. 연동 점검표를 확인하세요.',
    'READINESS_ARM_UNMET': 'R의 ARM 작업 경로가 설치되지 않았습니다. 연동 점검표를 확인하세요.',
    'READINESS_EVIDENCE_UNMET': 'R의 물리 근거·재확인 경로가 아직 응답하지 않습니다. 연동 점검표를 확인하세요.',
    'READINESS_ARM_EXECUTION_UNANNOUNCED': '플랫폼이 운영 프로필의 실물 ARM 실행을 아직 알리지 않았습니다.',
    'READINESS_EVIDENCE_PRODUCER_REQUIRED': '업무 완료 근거 생산자가 없습니다. 관리자가 사람 확인을 켜고 현장 확인자를 지정해야 합니다.',
    'READINESS_OBSERVATION_UNAUTHENTICATED': '수동·합성 관측은 운영 실행 근거가 아닙니다.',
    'READINESS_CELL_PLAN_ROUTE_UNMET': 'R의 셀 계획 경로가 아직 응답하지 않습니다. 연동 점검표를 확인하세요.',
    'EXECUTION_INPUTS_UNAVAILABLE': '연결 기준을 읽을 수 없습니다. 설치 담당자와 원본을 확인하세요.',
    'BUSINESS_HOLD': '원래 실행을 찾았지만 업무 보류는 유지됩니다. 완료 근거를 확인하세요.',
    'CANCELLATION_REQUESTED': '취소를 요청했습니다. 실제 정지·점유 해제는 아직 확인되지 않았습니다.',
    'CANCELLATION_OUTCOME_UNKNOWN': '취소 요청의 결과가 불명입니다. 원래 실행으로 대조해야 합니다.',
    'RECONCILIATION_REQUIRED': '제출 또는 원본 조회 결과가 불명입니다. 새로 제출하지 말고 원래 요청을 대조하세요.',
    'LEDGER_UNAVAILABLE': '업무 장부를 읽을 수 없습니다. 실행 상태를 정상으로 판단하지 마세요.',
}


def gate_text(code):
    return _GATE_TEXTS.get(code, '현재 연결과 원래 요청을 대조해야 합니다.' if code else '')


_REFUSAL_TEXTS = {
    'ACTIVE_CELL_PLAN_DISPATCH': '이 요청은 이미 ROSY에 셀 계획으로 제안되어 진행 중입니다. 셀 계획의 진행 상태를 확인하거나 취소한 뒤 다시 요청하세요.',
    'INBOUND_DESTINATION_RESERVED': '입고 목적지 칸이 다른 요청에 예약되어 있습니다. 최신 관측으로 입고 위치를 다시 계산하세요.',
    'INBOUND_PLAN_SUPERSEDED': '이 입고 계획은 새 계획으로 대체되었습니다. 최신 계획에서 요청하세요.',
    'INTAKE_ALREADY_OBSERVED': '이 입고 회차의 화물은 이미 야드에서 관측되었습니다.',
    'INTAKE_OCCURRENCE_SUPERSEDED': '같은 화물의 새 입고 회차가 있습니다. 최신 회차로 다시 계획하세요.',
    'INTAKE_REQUIRES_BOUND_PLACEMENT': '입고 회차와 연결된 배치 계획이 필요합니다.',
    'INTAKE_YARD_OBSERVATION_CHANGED': '계획 뒤 야드 관측이 바뀌었습니다. 최신 관측으로 다시 계획하세요.',
}
_CODE = re.compile(r'[A-Z][A-Z0-9_]+')


def refusal_text(exc, fallback):
    """Operator text for a refused action: known codes in Korean, other codes named after the fallback."""
    raw = exc.messages[0] if getattr(exc, 'messages', None) else str(exc)
    text = _REFUSAL_TEXTS.get(raw) or _GATE_TEXTS.get(raw)
    if text:
        return text
    if not _CODE.fullmatch(raw or ''):
        return raw if getattr(exc, 'messages', None) else fallback
    return f'{fallback} (사유 코드 {raw})'
