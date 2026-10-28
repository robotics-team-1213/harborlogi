"""D-908 trip proposal helpers shared by the executor and the field page (no Fleet calls here)."""
from contextlib import closing
from datetime import timedelta
import os
from pathlib import Path
import sqlite3

FRESH = timedelta(minutes=5)  # same window as services.prepare_departure: the S clearance must be current
LABELS = {
    'PLANNED': '제안 준비됨', 'SUBMITTING': '제안 전송 중', 'UNCERTAIN': '제안 결과 불확실 · 원본 key로 대조 중',
    'PROPOSED': '운영자 승인 대기', 'ADMITTED': '운영자 승인됨 · 이송 수행 중',
    'GOAL_CONFIRMED': '이송 도착 사실 확인 (화물 도착 판단은 별도)',
}


def trip_key(departure):
    return 'harbor-trip:' + str(departure.key)


def destination():
    return os.environ.get('HARBOR_TRIP_DESTINATION', '')


def trip_status(departure):
    """Read-only view of the saved proposal state, or None when no proposal exists or the ledger is unavailable."""
    path = os.environ.get('HARBOR_LEDGER_PATH', '')
    if not path or not Path(path).is_file():
        return None
    try:
        with closing(sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=2)) as connection:
            row = connection.execute('SELECT state, mission_id FROM transport_proposals WHERE request_key=?',
                                     (trip_key(departure),)).fetchone()
    except sqlite3.Error:
        return None
    return None if row is None else dict(state=row[0], label=LABELS.get(row[0], row[0]), mission_id=row[1])
