"""Saved facts only: reading a pipeline never contacts Fleet or advances a ledger."""
import os
from urllib.parse import urlencode, urlsplit

from harbor.rosy_http import RosyClient
from harbor.execution import execution_phase


COMMAND_LABELS = {
    'PREPARED': '제안 검토 대기', 'DISPATCHING': '제출 대조 필요', 'UNKNOWN': '결과 불명',
    'CANCEL_PENDING': '취소 대조 필요', 'BLOCKED': '인계 보류', 'RECONCILED': '원본 대조됨',
}
JOB_LABELS = {
    'awaiting_approval': '독립 승인 대기', 'awaiting_execution': '승인됨 · 수행 준비', 'executing': '수행 중',
    'awaiting_physical_evidence': '동작 성공 · 물리 근거 대기', 'physical_confirmed': '물리 결과 확인됨',
    'held': '수행 보류',
}


def fleet_links(origin, mission_id=None, robot_id=None, arm_console_enabled=False):
    links = dict(console=None, arm=None, robot=None)
    try:
        if not isinstance(origin, str) or any(c.isspace() or ord(c) < 32 for c in origin):
            return links
        RosyClient(origin, 'link-validation-only')
        urlsplit(origin).port
    except (ValueError, TypeError):
        return links
    base = origin.rstrip('/') + '/console?'
    links['console'] = base + 'view=work'
    if mission_id and arm_console_enabled:
        links['arm'] = base + urlencode(dict(view='work', arm_transfer=mission_id))
    if robot_id:
        links['robot'] = base + urlencode(dict(robot=robot_id))
    return links


def connection_summary():
    from harbor_web.fleet_connection import connection_settings, last_check
    config = connection_settings()
    check = last_check()
    links = fleet_links(config['origin'])
    declared = config['arm_console_enabled']
    configured = bool(links['console']) and config['allowed'] and bool(config['principal']) and all(
        os.environ.get('HARBOR_' + key) for key in ('LEDGER_PATH', 'FLEET_TOKEN', 'BINDINGS_PATH'))
    return dict(configured=configured, verified=bool(check and check.detail['status'] == 'VERIFIED'),
        links=links, arm_console_enabled=declared,
        origin=config['origin'] if links['console'] else '',
        requirements=[
            dict(label='접속 설정', status='설정 있음' if configured else '설정 필요',
                 note='설정 유무만 확인합니다. 인증·API 호환성을 조회하지 않았습니다.'),
            dict(label='Fleet ARM 승인 화면', status='설치 선언 있음' if declared else '설치 확인 필요',
                 note='화면 설치 선언은 실제 제안 조회나 독립 승인 성공의 증거가 아닙니다.'),
            dict(label='실행 원본', status='작업별 대조 필요', note='원래 key·mission·본문·신원과 저장된 조회 시각을 대조합니다.'),
            dict(label='다칸 화물 실행', status='설치 대응 검증 필요', note='크기·방향·전체 점유 위치에 맞는 실행 binding이 필요합니다.'),
            dict(label='업무 완료', status='인증된 업무 근거 연결 필요',
                 note='배치·복귀·관련 팔 퇴피 근거와 현장 수용은 별도로 확인합니다.'),
        ])


def pipeline_for(order, plan=None, command=None, detail=None):
    detail = detail or {}
    job, receipt, ledger = detail.get('job') or {}, detail.get('receipt'), detail.get('ledger') or {}
    state = command.state if command else ''
    status = execution_phase(job) if job else ''
    uncertain = state in {'UNKNOWN', 'DISPATCHING', 'CANCEL_PENDING'} or bool(detail.get('reason') == 'LEDGER_UNAVAILABLE')
    from harbor_web.fleet_connection import connection_settings
    config = connection_settings()
    links = fleet_links(command.fleet_origin, command.mission_id,
        arm_console_enabled=config['arm_console_enabled'] and (not config['origin'] or config['origin'] == command.fleet_origin)) if command else fleet_links(None)
    submitted = bool(command and command.mission_id and state != 'PREPARED')
    approved = status in {'awaiting_execution', 'executing', 'awaiting_physical_evidence', 'physical_confirmed'}
    executed = status in {'awaiting_physical_evidence', 'physical_confirmed'}
    completed = ledger.get('state') == 'COMPLETED'

    def stage(key, label, owner, done, note, active=False, href=None, action=''):
        return dict(key=key, label=label, owner=owner, status='done' if done else 'current' if active else 'pending',
                    note=note, href=href, action=action)

    stages = [
        stage('plan', '업무·계획 검토', 'Harbor 관제', bool(plan),
              '저장한 원본과 지시 revision으로 검토합니다. 계획 READY는 실행 허가가 아닙니다.',
              not plan, f'/requests/{order.pk}/'),
        stage('proposal', '제안 제출', 'Harbor 운영자', submitted,
              COMMAND_LABELS.get(state, '검토한 계획에서 인계를 준비하세요.'), bool(command and not submitted)),
        stage('approval', '독립 승인', 'ROSY Fleet 운영자', approved,
              '제안자와 다른 명명 운영자가 원본 제안을 검토하고 승인합니다.',
              status == 'awaiting_approval', links['arm'], 'fleet'),
        stage('execution', '동작 수행', 'ROSY Fleet · CORE · owner', executed,
              JOB_LABELS.get(status, '검증된 수행 원본이 아직 없습니다.'), status in {'awaiting_execution', 'executing'}),
        stage('physical', '물리 결과 대조', '독립 observer · Harbor 대조', bool(receipt),
              '검증된 receipt 저장됨' if receipt else 'Action 성공 뒤에도 목표 위치·퇴피 근거가 필요합니다.',
              executed and not receipt),
        stage('business', '업무 완료 대조', 'Harbor 업무 장부', completed,
              '장부의 완료 근거를 확인했습니다.' if completed else '배치·재취급 복귀·관련 팔 모두 퇴피 근거가 필요합니다.',
              bool(receipt) and not completed, f'/records/?order={order.pk}'),
    ]
    primary = dict(action='', label='업무 검토로 돌아가기', href=f'/requests/{order.pk}/',
                   note='현재 원본과 보류 사유를 먼저 확인하세요.')
    if command and state == 'PREPARED' and not uncertain:
        primary = dict(action='submit', label='검토한 현재 단계 제안 제출', href='',
                       note='Fleet에 제안을 저장합니다. 독립 승인과 수행은 Fleet에서 진행합니다.')
    elif command and (uncertain or submitted or state == 'RECONCILED'):
        primary = dict(action='reconcile', label='원래 요청으로 최신 원본 대조', href='',
                       note='저장된 같은 요청 key로 조회합니다. 새 제안을 만들지 않습니다.')
        if not uncertain and ledger.get('state') != 'HOLD' and status == 'awaiting_approval' and links['arm']:
            primary = dict(action='fleet', label='Fleet에서 독립 승인 검토', href=links['arm'],
                           note='Fleet 자신의 계정으로 원본을 검토하세요. 제안자는 직접 승인할 수 없습니다.')
    return dict(stages=stages, primary=primary, links=links, read_at=detail.get('read_at'),
                command_label=COMMAND_LABELS.get(state, '인계 준비 전'),
                job_label=JOB_LABELS.get(status, '수행 원본 미확인'),
                business_label='완료 근거 대조됨' if completed else '업무 완료 근거 대조 필요')
