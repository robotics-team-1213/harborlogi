"""Read-only wording for audit events; technical identifiers stay in support details."""

OBJECT_LABELS = {
    'Cargo': '화물',
    'CargoFootprint': '화물 크기',
    'Condition': '설치 기준',
    'Observation': '야드 현황',
    'Order': '업무',
    'Plan': '계획',
    'Command': '실행',
    'FieldEpisode': '현장 업무',
    'FieldConfirmation': '적재면 확인',
    'DepartureIntent': '출발 준비',
    'Measurement': '측정 자료',
    'FleetConnection': 'Fleet 연결 설정',
    'EvidenceProducerSetting': '실행 자료 연결 설정',
    'RearrangeAuthorization': '자동 재배치 설정',
    'HumanAttestation': '사람 확인',
}

ACTION_SENTENCES = {
    'cargo.register': '화물을 등록했습니다.',
    'footprint.register': '화물 크기를 등록했습니다.',
    'condition.register': '설치 기준을 등록했습니다.',
    'condition.map.register': '현장 지도를 등록했습니다.',
    'observation.record': '야드 현황을 등록했습니다.',
    'measurement.record': '측정 자료를 등록했습니다.',
    'order.create': '업무를 등록했습니다.',
    'order.update': '업무 지시를 변경했습니다.',
    'order.auto_replan': '자동 계획 설정을 변경했습니다.',
    'plan.create': '계획을 계산했습니다.',
    'plan.decide': '입고 자리를 결정했습니다.',
    'episode.assign': '적재면 확인 담당자를 배정했습니다.',
    'episode.reassign': '적재면 확인 담당자를 다시 배정했습니다.',
    'episode.confirm': '적재면 확인 결과를 기록했습니다.',
    'departure.prepare': '차량 출발을 준비했습니다.',
    'command.prepare': '실행 요청을 준비했습니다.',
    'command.prepared': '실행 요청을 준비했습니다.',
    'command.dispatching': 'ROSY Fleet에 실행 요청을 보내기 시작했습니다.',
    'command.reconciled': 'ROSY 실행 결과를 가져왔습니다.',
    'command.unknown': '실행 결과를 알 수 없어 검토가 필요합니다.',
    'command.blocked': '실행 조건이 맞지 않아 요청을 진행하지 못했습니다.',
    'command.cancel_pending': '실행 취소를 요청했습니다. 결과를 기다립니다.',
    'command.cancelled': '실행 취소를 기록했습니다.',
    'command.completed': '업무 완료를 기록했습니다.',
    'human_attestation.record': '현장에서 사람이 확인한 결과를 기록했습니다.',
    'scenario.import': '시험 자료를 불러왔습니다.',
    'fleet.discover': 'Fleet 연결 후보를 조회했습니다.',
    'fleet.configure': 'Fleet 연결 설정을 저장했습니다.',
    'fleet.check': 'Fleet 연결 점검 결과를 기록했습니다.',
    'evidence_producer.configure': '실행 자료 연결 설정을 저장했습니다.',
    'rearrange.authorization.enable': '자동 재배치를 허용했습니다.',
    'rearrange.authorization.disable': '자동 재배치 허용을 껐습니다.',
    'rearrange.hold': '자동 재배치를 진행하지 못한 이유를 기록했습니다.',
}


def record_text(action):
    return ACTION_SENTENCES.get(action, '새 사건을 기록했습니다.')
