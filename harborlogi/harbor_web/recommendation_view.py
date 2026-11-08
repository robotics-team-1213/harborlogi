"""Present the score saved with a plan; never search or infer physical performance."""
from copy import deepcopy


RESTORATION_LABELS = (
    ('different_area_count', '복귀 구역 변경', '개체'),
    ('different_position_count', '복귀 행·열 변경', '개체'),
    ('same_area_manhattan_distance', '같은 구역 주소 차이 합', '칸'),
    ('final_level_sum', '복귀 층 합계', '층'),
)
# Saved plans keep their own labels; v2 ranks future obstruction first, v3 adds unknown order (design 32).
OBJECTIVE_LABELS = {
    'restoration_lex_v1': RESTORATION_LABELS,
    'restoration_lex_v2': (('earlier_outbound_buried', '먼저 나갈 화물 덮음', '건'),) + RESTORATION_LABELS,
    'restoration_lex_v3': (('earlier_outbound_buried', '먼저 나갈 화물 덮음', '건'),
                           ('unknown_outbound_order', '순서 모르는 화물 위에 놓음', '건'),
                           RESTORATION_LABELS[0], ('rotation_count', '복귀 회전', '개체')) + RESTORATION_LABELS[1:],
}


def _valid_score(score, labels):
    return isinstance(score, dict) and all(type(score.get(key)) is int and score[key] >= 0
                                         for key, _, _ in labels)


def explain_optimization(saved):
    labels = OBJECTIVE_LABELS.get(saved.get('objective_version')) if isinstance(saved, dict) else None
    if labels is None:
        return None
    report = deepcopy(saved)
    score, first = report.get('score'), report.get('first_feasible_score')
    if score is not None and not _valid_score(score, labels):
        return None
    report['score_labels'] = labels
    report['score_rows'] = [dict(label=label, value=score[key], unit=unit,
        first=first.get(key) if isinstance(first, dict) else None) for key, label, unit in labels] if score else []
    report['quality_label'] = ('정의한 작업군 비교 완료' if report.get('search_complete') is True else '탐색 한도 내 추천') if score else '완전한 추천안 없음'
    report['improved'] = bool(score and isinstance(first, dict) and all(
        type(first.get(key)) is int for key, _, _ in labels) and
        tuple(score[key] for key, _, _ in labels) < tuple(first[key] for key, _, _ in labels))
    return report


def candidate_details(result, rank):
    """Read a bounded saved alternative, without replacing the authoritative plan."""
    if not isinstance(result, dict):
        return None
    report = explain_optimization(result.get('optimization'))
    alternatives = report.get('alternatives') if report else None
    if result.get('status') != 'READY' or not isinstance(alternatives, list) or not 1 <= rank <= min(5, len(alternatives)):
        return None
    labels = report['score_labels']
    first, candidate = alternatives[0], alternatives[rank - 1]
    if not isinstance(first, dict) or first.get('moves') != result.get('moves') or first.get('score') != report.get('score'):
        return None
    if not isinstance(candidate, dict) or not _valid_score(candidate.get('score'), labels):
        return None
    moves = candidate.get('moves')
    if not isinstance(moves, list) or not moves or any(not isinstance(move, dict) for move in moves):
        return None
    return dict(candidate=candidate, candidate_count=min(5, len(alternatives)),
        quality_label=report['quality_label'], search_complete=report.get('search_complete') is True,
        score_rows=[dict(label=label, value=candidate['score'][key], recommended=report['score'][key], unit=unit)
                    for key, label, unit in labels]) if _valid_score(report.get('score'), labels) else None
