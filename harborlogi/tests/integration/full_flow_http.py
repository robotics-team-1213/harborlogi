"""D-927 full first flow on one simulation Fleet: run each stage, report it, stop at the first failure.

The stage-to-call mapping is provisional until the business executor (stream A) owns the sequence.
Progress is the number of the first failing stage, not a green unit test.
"""
from datetime import datetime, timezone
import ipaddress
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from urllib.error import HTTPError
from urllib.parse import urlsplit

from harbor.physical_evidence import fleet_move_evidence, fleet_recheck_evidence
from harbor.process import ArmTransferBinding
from harbor.rosy_http import RosyClient
from harbor.transport import TripIntent, TransportLedger
from harbor.workflow import BusinessLedger

KEY = 'harbor-full-flow'  # the harness approves f'{KEY}:{move index}'
SOURCES = ('fleet-physical-object', 'fleet-physical-retreat')


def _wait(read, done, what, timeout=30):
    deadline = time.monotonic() + timeout
    while not done(value := read()):
        if time.monotonic() > deadline:
            raise TimeoutError(f'{what} did not finish within {timeout} s')
        time.sleep(.1)
    return value


class FullFlow:
    def __init__(self, config, folder):
        self.config, self.notes = config, []
        self.client = RosyClient(config['origin'], config['operator'])
        self.business = BusinessLedger(Path(folder) / 'business.sqlite3',
            placement_sources=SOURCES[:1], retreat_sources=SOURCES[1:])
        self.transport = TransportLedger(Path(folder) / 'transport.sqlite3')
        self.moves = tuple(ArmTransferBinding(**move) for move in config['moves'])

    def stages(self):
        return (('0 한 Fleet', self.one_fleet), ('① 반출 전 변경 지시', self.change_order),
                ('② 재취급 화물 처리', self.arm_move), ('③ 이송', self.trip),
                ('④ S 내려놓기', self.arm_move), ('⑤ 재취급 정리·팔 퇴피', self.restore_and_retreat))

    def one_fleet(self):
        for read in (self.client.trip_request, self.client.arm_request):
            try:
                read(KEY + ':absent')
                raise AssertionError('an absent request key was found')
            except HTTPError as error:
                # A missing route also answers 404, but only with FastAPI's generic body.
                if error.code != 404 or json.load(error) == {'detail': 'Not Found'}:
                    raise
        self.notes.append('TRIP·ARM routes answer on ' + self.config['origin'])

    def change_order(self):
        self.business.prepare(KEY, 1, self.moves, outbound_id=self.config['outbound_id'],
                              outbound_stand=self.config['outbound_stand'], move_arms=(('A', 'B'),) * len(self.moves))
        self.notes.append(f'plan revision 1 with {len(self.moves)} moves')

    def arm_move(self):
        mission = self.business.propose(KEY, self.client)
        def refreshed():
            self.business.refresh(KEY, self.client, now=datetime.now(timezone.utc))
            return self.business.read(KEY)
        row = _wait(refreshed, lambda r: r['goal_confirmed'] or r['state'] == 'HOLD', 'ARM move ' + mission)
        if row['state'] == 'HOLD':
            raise AssertionError('Fleet held ARM mission ' + mission)
        self.notes.append(f'Fleet GOAL_CONFIRMED {mission} attempt {row["attempt_id"]}')
        self.business.confirm(KEY, self.evidence(row), now=datetime.now(timezone.utc))

    def evidence(self, row):
        index = row['step_index']
        return fleet_move_evidence(self.client, self.moves[index], f'{KEY}:{index}', mission_id=row['mission_id'],
                                   revision=1, placement_source=SOURCES[0],
                                   retreat_source=SOURCES[1])

    def final_evidence(self, row):
        # D-930: fresh facts about the outbound attempt measured after the last move.
        mission = row['outbound_mission_id']
        evidence = _wait(lambda: fleet_recheck_evidence(self.client, self.moves[1], f'{KEY}:1', KEY + ':final',
            mission_id=mission, revision=1, placement_source=SOURCES[0],
            retreat_source=SOURCES[1]), lambda value: value is not None, 'recheck of ' + mission)
        self.notes.append('recheck CONFIRMED for outbound ' + mission)
        return evidence

    def trip(self):
        intent = TripIntent(KEY + ':trip', tuple(self.config['trip_candidates']), self.config['trip_destination'])
        self.transport.prepare(intent)
        job = self.transport.submit(intent.request_key, self.client, operator_confirmed=True)
        job = _wait(lambda: self.transport.reconcile(intent.request_key, self.client),
                    lambda j: j['status'] in ('GOAL_CONFIRMED', 'HOLD'), 'trip ' + job['mission_id'])
        if job['status'] != 'GOAL_CONFIRMED':
            raise AssertionError('Fleet held trip ' + job['mission_id'])
        self.notes.append('trip GOAL_CONFIRMED ' + job['mission_id'])

    def restore_and_retreat(self):
        self.arm_move()
        self.business.finish(KEY, self.final_evidence(self.business.read(KEY)), now=datetime.now(timezone.utc))
        self.notes.append('business ' + self.business.read(KEY)['state'])


class FullFlowHttpTests(unittest.TestCase):
    def test_first_flow_stages_on_one_fleet(self):
        config = json.loads(Path(os.environ['HARBOR_INTEGRATION_CONFIG']).read_text(encoding='utf-8'))
        self.assertEqual(config['schema'], 'harbor.full-flow-http-test/1')
        self.assertIs(config['simulation'], True)
        origin = urlsplit(config['origin'])
        self.assertEqual(origin.scheme, 'http')
        self.assertTrue(ipaddress.ip_address(origin.hostname).is_loopback)
        results, failure = [], None
        with tempfile.TemporaryDirectory() as folder:
            flow = FullFlow(config, folder)
            for name, run in flow.stages():
                started, flow.notes = time.monotonic(), []
                try:
                    run()
                except Exception as error:
                    failure = f'{name}: {type(error).__name__}: {error}'
                result = {'stage': name, 'ok': failure is None, 'seconds': round(time.monotonic() - started, 2),
                          'notes': flow.notes, **({'error': failure} if failure else {})}
                results.append(result)
                print(json.dumps(result, ensure_ascii=False), flush=True)
                if failure:
                    break
        report = {'platform_sha': config['platform_sha'], 'app_sha': config['app_sha'], 'first_failure': failure,
                  'stages': results}
        if config.get('report_path'):
            Path(config['report_path']).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        if failure:
            self.fail('first failing stage ' + failure)


if __name__ == '__main__':
    unittest.main()
