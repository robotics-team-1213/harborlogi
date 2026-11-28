"""Django tests for dual-mode OMX cell-plan dispatch (Harbor plan order vs autonomous ROSY directive)."""
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from django.contrib.auth.models import Group, User
from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from harbor.transport import TransportLedger
from harbor_web import cell_plan_dispatch, executor, outbound_loop, services
from harbor_web.models import Audit, CargoFootprint, CellPlanDispatch, OutboundDispatch
from harbor_web.runtime_models import DeploymentState

ROOT = Path(__file__).resolve().parents[2]


def _http_error(status: int, code: str | None = None) -> HTTPError:
    body = json.dumps({'detail': {'code': code}}).encode('utf-8') if code else b'Not Found'
    return HTTPError('https://fleet.invalid/api/fleet/pipeline/cell-plans', status, 'err', {}, BytesIO(body))


def _canonical_places_doc() -> dict:
    body = {
        'schema': 'rosy.port-site-places.v1',
        'sources': {
            'site_map': {'map_id': 'w', 'version': 1, 'sha256': 'a' * 64},
            'cell_site_map': {'schema': 'rosy.cell-site-map.v1', 'revision': 1, 'status': 'verified'},
        },
        'vehicle_places': [
            {'place_id': 'S_OUT', 'port_role': 'outbound_pickup', 'status': 'verified', 'dispatchable': True, 'label': 'S_OUT'},
            {'place_id': 'S_EXIT', 'port_role': 'outbound_exit', 'status': 'verified', 'dispatchable': True, 'label': 'S_EXIT'},
        ],
        'cell_zones': [
            {'zone_id': 'yard', 'role': 'storage', 'status': 'verified', 'label': '야드'},
            {'zone_id': 'stand', 'role': 'outbound', 'status': 'verified', 'label': '반출대'},
        ],
        'intake_handoffs': [],
        'outbound_handoffs': [{
            'handoff_id': 'outbound_stand_load',
            'cell_zone_id': 'stand',
            'wire_slot_ref': 'stand-r1-c1-l1',
            'vehicle_pickup_stop_id': 'S_OUT',
            'vehicle_exit_stop_id': 'S_EXIT',
            'status': 'verified',
            'dispatchable': True,
        }],
        'not_places': ['belt'],
    }
    encoded = json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    return {**body, 'digest': sha256(encoded.encode('utf-8')).hexdigest()}


class _FakeCellPlanClient:
    def __init__(self):
        self.calls = []
        self.by_key = {}
        self.by_id = {}
        self.status = 'PROPOSED'
        self.post_error = None
        self.get_key_error = None
        self.sites_doc = _canonical_places_doc()

    def propose_cell_plan(self, payload, *, request_key):
        self.calls.append((request_key, payload))
        if payload['mode'] == 'harbor_plan':
            moves = [
                {
                    'src_wire_ref': s['src_site'],
                    'dst_wire_ref': s['dst_site'],
                    'arm_id': s.get('arm_id', 'arm_a'),
                    'phase_label': s.get('phase_label', '이동'),
                }
                for s in payload['steps']
            ]
            source = 'harbor'
        else:
            moves = [
                {'src_wire_ref': 'yard-r1-c1-l2', 'dst_wire_ref': 'temp_a-r1-c1-l1', 'arm_id': 'arm_a', 'phase_label': '재취급'},
                {'src_wire_ref': 'yard-r1-c1-l1', 'dst_wire_ref': 'stand-r1-c1-l1', 'arm_id': 'arm_a', 'phase_label': '반출'},
                {'src_wire_ref': 'temp_a-r1-c1-l1', 'dst_wire_ref': 'yard-r1-c1-l1', 'arm_id': 'arm_a', 'phase_label': '복귀'},
            ]
            source = 'rosy_r'
        plan_id = f'cell-plan-{len(self.calls)}'
        tasks = [
            {
                'task_id': f'{plan_id}-s{i + 1}',
                'depends_on': None if i == 0 else f'{plan_id}-s{i}',
                'status': self.status,
                'dst_wire_ref': m['dst_wire_ref'],
                'arm_id': m['arm_id'],
            }
            for i, m in enumerate(moves)
        ]
        response = {
            'ok': True,
            'schema': 'rosy.cell-plan.v1',
            'plan': {
                'plan_id': plan_id,
                'schema': 'rosy.cell-plan.v1',
                'mode': payload['mode'],
                'order_source': source,
                'status': self.status,
                'total_steps': len(tasks),
                'completed_steps': 0,
                'request_key': request_key,
                'task_ids': [t['task_id'] for t in tasks],
                'moves': moves,
            },
            'tasks': tasks,
        }
        self._last = response
        self.by_key[request_key] = response
        self.by_id[plan_id] = response
        if self.post_error is not None:
            raise self.post_error
        return response

    def cell_plan(self, plan_id):
        rec = self.by_id[plan_id]
        plan_extra = {}
        if getattr(self, 'hold_reason', None):
            plan_extra['hold_reason'] = self.hold_reason
        return {
            **rec,
            'plan': {**rec['plan'], 'status': self.status, **plan_extra},
            'tasks': [{**t, 'status': self.status} for t in rec['tasks']],
        }

    def preview_cell_plan(self, payload):
        return {
            'ok': True,
            'schema': 'rosy.cell-plan.v1',
            'mode': payload['mode'],
            'order_source': 'harbor' if payload['mode'] == 'harbor_plan' else 'rosy_r',
            'moves': payload.get('steps') or [{'src_site': 'yard-r1-c1-l1', 'dst_site': 'stand-r1-c1-l1', 'arm_id': 'arm_a', 'phase_label': '반출'}],
            'final_state': {'C1': 'stand-r1-c1-l1'},
        }

    def cancel_cell_plan(self, plan_id, *, reason):
        rec = self.by_id[plan_id]
        self.status = 'CANCELLED'
        tasks = [{**t, 'status': 'CANCELLED', 'cancel_reason': reason} for t in rec['tasks']]
        return {
            'ok': True,
            'schema': 'rosy.cell-plan.v1',
            'plan': {**rec['plan'], 'status': 'CANCELLED', 'hold_reason': reason},
            'tasks': tasks,
            'cancelled_task_ids': [t['task_id'] for t in tasks],
        }

    def cell_plan_request(self, request_key):
        if self.get_key_error is not None:
            raise self.get_key_error
        if request_key not in self.by_key:
            raise _http_error(404, 'UNKNOWN_CELL_PLAN')
        rec = self.by_key[request_key]
        return {
            **rec,
            'plan': {**rec['plan'], 'status': self.status},
            'tasks': [{**t, 'status': self.status} for t in rec['tasks']],
        }

    def site_places(self):
        return self.sites_doc


class _FakeOutboundTransport:
    def __init__(self, ledger_path: Path):
        self.ledger = TransportLedger(ledger_path)
        self.trip_status = 'QUEUED'
        self.assigned_robot = 'pinky-01'

    def prepare(self, intent):
        self.ledger.prepare(intent)

    def read(self, request_key):
        return self.ledger.read(request_key)

    def propose(self, request_key, client):
        del client
        return {'mission_id': f'trip:{request_key}', 'robot_id': self.assigned_robot, 'status': self.trip_status}

    def reconcile_proposal(self, request_key, client):
        return self.propose(request_key, client)


class CellPlanDispatchTests(TestCase):
    def setUp(self):
        # Dispatch checks the deployment runtime gate; tests run in a reconciled, non-restored state.
        DeploymentState.objects.get_or_create(pk=1, defaults={'mode': settings.MODE})
        self.operator = User.objects.create_user('cell-op')
        for role in ('admin', 'operator', 'field', 'viewer'):
            self.operator.groups.add(Group.objects.get_or_create(name=role)[0])
        self.viewer = User.objects.create_user('cell-viewer')
        self.viewer.groups.add(Group.objects.get_or_create(name='viewer')[0])

        policy = json.loads((ROOT / 'config/spatial-policy-3x6.json').read_text(encoding='utf-8'))
        policy['yard_layout'] = json.loads((ROOT / 'config/yard-layout-3x6.json').read_text(encoding='utf-8'))
        self.condition = services.register_condition(self.operator, name='3x6', calibration_ref='synthetic', payload=policy)
        for code in ('C1', 'C2'):
            services.register_cargo(self.operator, code=code, name=code, weight_kg=2, volume_m3='0.012', destination='stand', length_cells=3)
        c1, c2 = [CargoFootprint.objects.filter(cargo__code=c).latest('pk') for c in ('C1', 'C2')]
        self.observation = services.record_observation(
            self.operator,
            source_id='cam-top',
            source_revision='rev-1',
            observed_at=timezone.now(),
            provenance='SYNTHETIC',
            condition=self.condition,
            payload=dict(
                schema='grid-placement-v1',
                observed_areas=['yard', 'temp_a', 'temp_b', 'stand'],
                holdings=[],
                placements=[
                    dict(cargo_id='C1', area_id='yard', row=1, column=1, level=1, orientation=0, footprint_revision=c1.pk),
                    dict(cargo_id='C2', area_id='yard', row=1, column=1, level=2, orientation=0, footprint_revision=c2.pk),
                ],
            ),
        )
        self.order = services.create_order(self.operator, cargo=c1.cargo, kind='OUTBOUND', priority_group=1, sequence=1, tie_policy='FIXED')
        self.plan = services.make_plan(self.operator, order_id=self.order.pk, expected_version=1, observation_id=self.observation.pk)
        self.assertEqual(self.plan.result['status'], 'READY')

    def test_default_client_requires_service_role_rule_4(self):
        env = {
            'HARBOR_FLEET_ORIGIN': 'https://fleet.example',
            'HARBOR_FLEET_TOKEN': 'tok-svc',
            'HARBOR_FLEET_PRINCIPAL': 'cell-app-service',
            'HARBOR_FLEET_ALLOW_HOSTS': 'fleet.example',
        }
        with patch.dict(os.environ, env, clear=False), patch('harbor_web.cell_plan_dispatch.connection_settings', return_value={'origin': 'https://fleet.example', 'allowed': True, 'principal': 'cell-app-service'}):
            client = cell_plan_dispatch._default_client()
        self.assertEqual(client.expected_principal, 'cell-app-service')
        self.assertEqual(client.expected_role, 'service')

    def _inbound_plan(self, code='C3', sequence=2):
        cargo = services.register_cargo(self.operator, code=code, name='incoming', weight_kg=2,
                                       volume_m3='0.012', destination='yard', length_cells=3)
        order = services.create_order(self.operator, cargo=cargo, kind='INBOUND', priority_group=2,
                                      sequence=sequence, tie_policy='FIXED')
        return services.make_plan(self.operator, order_id=order.pk, expected_version=order.version,
                                  observation_id=self.observation.pk)

    def test_inbound_preview_and_dispatch_use_the_decided_alternative(self):
        from harbor_web.inbound_decision import decide_placement

        plan = self._inbound_plan()
        decision = decide_placement(self.operator, plan_id=plan.pk, expected_version=plan.order_version,
                                    alternative_rank=1)
        selected = decision.detail['recommendation']
        dst = '{area_id}-r{row}-c{column}-l{level}'.format(**selected)
        self.assertNotEqual(selected, plan.result['recommendation'])
        client = _FakeCellPlanClient()
        preview = cell_plan_dispatch.preview_harbor_plan(self.operator, plan_id=plan.pk, client=client)
        row = cell_plan_dispatch.dispatch_harbor_plan(self.operator, plan_id=plan.pk, client=client)
        self.assertEqual(preview.moves[0].dst_site, dst)
        self.assertEqual(client.calls[0][1]['steps'][0]['dst_site'], dst)
        self.assertEqual(row.planned_steps[0]['dst_site'], dst)

    def test_dispatch_rechecks_decided_destination_against_prior_submission(self):
        from harbor_web.inbound_decision import decide_placement

        first = self._inbound_plan()
        second = self._inbound_plan('C4', sequence=3)
        first = services.make_plan(self.operator, order_id=first.order_id, expected_version=first.order_version,
                                   observation_id=self.observation.pk)
        # The two saved rankings place the same candidate at different ranks.
        ranked = first.result['alternatives'][0]
        first.result['recommendation'], ranked['placement'] = ranked['placement'], first.result['recommendation']
        from harbor_web.models import Plan
        first = Plan.objects.create(created_by=self.operator, order=first.order, order_version=first.order_version,
                                    observation=self.observation, condition=self.condition,
                                    queue_digest=first.queue_digest, result=first.result)
        chosen = decide_placement(self.operator, plan_id=second.pk, expected_version=second.order_version,
                                  alternative_rank=1).detail['recommendation']
        addr = '{area_id}-r{row}-c{column}-l{level}'
        self.assertEqual(addr.format(**chosen), addr.format(**first.result['recommendation']))
        client = _FakeCellPlanClient()
        sent = cell_plan_dispatch.dispatch_harbor_plan(self.operator, plan_id=first.pk, client=client)
        self.assertEqual(sent.request_payload['steps'][0]['dst_site'], addr.format(**chosen))
        with self.assertRaisesMessage(services.Conflict, 'INBOUND_DESTINATION_RESERVED'):
            cell_plan_dispatch.dispatch_harbor_plan(self.operator, plan_id=second.pk, client=client)
        self.assertFalse(CellPlanDispatch.objects.filter(plan=second).exists())
        self.assertEqual(len(client.calls), 1)

    def test_superseded_inbound_plan_is_rejected_before_submission(self):
        plan = self._inbound_plan()
        services.make_plan(self.operator, order_id=plan.order_id, expected_version=plan.order_version,
                           observation_id=self.observation.pk)
        client = _FakeCellPlanClient()
        with self.assertRaises(services.Conflict):
            cell_plan_dispatch.dispatch_harbor_plan(self.operator, plan_id=plan.pk, client=client)
        self.assertFalse(client.calls)
        self.assertFalse(CellPlanDispatch.objects.filter(plan=plan).exists())

    def test_dual_mode_dispatch_and_sync_lifecycle(self):
        client = _FakeCellPlanClient()
        harbor_row = cell_plan_dispatch.dispatch_harbor_plan(self.operator, plan_id=self.plan.pk, client=client)
        self.assertEqual(harbor_row.dispatch_mode, 'HARBOR_PLAN')
        self.assertEqual(harbor_row.order_source, 'harbor')
        self.assertEqual(harbor_row.state, 'PROPOSED')
        self.assertEqual(harbor_row.request_payload['mode'], 'harbor_plan')
        self.assertEqual(len(harbor_row.rosy_task_ids), len(self.plan.result['moves']))
        self.assertEqual(client.calls[0][1]['mode'], 'harbor_plan')

        # P0-2 mutex: the still-active harbor dispatch blocks any second dispatch for the same order.
        with self.assertRaises(services.Conflict) as blocked:
            cell_plan_dispatch.dispatch_rosy_directive(
                self.operator, order_id=self.order.pk, observation_id=self.observation.pk,
                directive_mode='outbound', plan_id=self.plan.pk, client=client,
            )
        self.assertEqual(str(blocked.exception), 'ACTIVE_CELL_PLAN_DISPATCH')
        self.assertEqual(CellPlanDispatch.objects.filter(order=self.order).count(), 1)

        client.status = 'COMPLETED'
        synced = cell_plan_dispatch.sync_cell_plan_dispatch(harbor_row.pk, client=client)
        self.assertEqual(synced.state, 'COMPLETED')

        rosy_row = cell_plan_dispatch.dispatch_rosy_directive(
            self.operator, order_id=self.order.pk, observation_id=self.observation.pk,
            directive_mode='outbound', plan_id=self.plan.pk, client=client,
        )
        self.assertEqual(rosy_row.dispatch_mode, 'ROSY_DIRECTIVE')
        self.assertEqual(rosy_row.order_source, 'rosy_r')
        self.assertEqual(len(rosy_row.rosy_task_ids), 3)
        self.assertNotIn('steps', client.calls[1][1])
        self.assertEqual(client.calls[1][1]['cargo_id'], 'C1')

        with self.assertRaises(PermissionDenied):
            cell_plan_dispatch.dispatch_harbor_plan(self.viewer, plan_id=self.plan.pk, client=client)

    def test_active_command_blocks_cell_plan_dispatch(self):
        from harbor_web.models import Command, Plan
        client = _FakeCellPlanClient()
        command_plan = Plan.objects.filter(order=self.order).first()
        Command.objects.create(
            order=self.order, plan=command_plan, actor=self.operator, plan_version=1,
            ledger_identity='l', fleet_origin='f', fleet_principal='p', body_digest='a' * 64,
            binding={}, state='UNKNOWN',
        )
        with self.assertRaises(services.Conflict) as blocked:
            cell_plan_dispatch.dispatch_harbor_plan(self.operator, plan_id=self.plan.pk, client=client)
        self.assertEqual(str(blocked.exception), 'EXISTING_ORDER_EXECUTION_REQUIRES_RECONCILIATION')
        self.assertEqual(CellPlanDispatch.objects.count(), 0)

    def test_write_before_post_and_get_request_key_reconciliation_rules_6_to_9(self):
        client = _FakeCellPlanClient()
        client.post_error = URLError('network timeout after commit')
        row = cell_plan_dispatch.dispatch_harbor_plan(self.operator, plan_id=self.plan.pk, client=client)
        self.assertEqual(row.state, 'UNCERTAIN')
        self.assertEqual(row.rosy_plan_id, '')
        self.assertEqual(row.request_payload['mode'], 'harbor_plan')
        self.assertEqual(len(client.calls), 1)

        # P0-2 mutex: an uncertain request still occupies the order.
        with self.assertRaises(services.Conflict):
            cell_plan_dispatch.dispatch_rosy_directive(
                self.operator, order_id=self.order.pk, observation_id=self.observation.pk, client=client,
            )

        # Rule 7: reconciliation queries GET ?request_key= and NEVER re-POSTs.
        client.post_error = None
        recovered = cell_plan_dispatch.reconcile_cell_plan_dispatch(row.pk, client=client)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(recovered.state, 'PROPOSED')
        self.assertEqual(recovered.rosy_plan_id, 'cell-plan-1')

        # Complete/cancel the recovered dispatch so the next independent Rule 8 / Rule 9 requests can start.
        cell_plan_dispatch.cancel_cell_plan_dispatch(
            self.operator, dispatch_id=recovered.pk, reason='done', client=client,
        )

        # Rule 8: uncoded 404 keeps UNCERTAIN (capability absent), and coded UNKNOWN_CELL_PLAN closes to NOT_FOUND.
        client.post_error = _http_error(503, 'PIPELINE_LEDGER_UNAVAILABLE')
        uncertain2 = cell_plan_dispatch.dispatch_rosy_directive(
            self.operator, order_id=self.order.pk, observation_id=self.observation.pk, client=client,
        )
        client.by_key.pop(uncertain2.request_key, None)
        client.get_key_error = _http_error(404, code=None)
        still_uncertain = cell_plan_dispatch.reconcile_cell_plan_dispatch(uncertain2.pk, client=client)
        self.assertEqual(still_uncertain.state, 'UNCERTAIN')
        self.assertEqual(still_uncertain.reason, 'CELL_PLAN_ROUTE_UNAVAILABLE')

        client.get_key_error = None
        closed = cell_plan_dispatch.reconcile_cell_plan_dispatch(uncertain2.pk, client=client)
        self.assertEqual(closed.state, 'NOT_FOUND')
        self.assertEqual(closed.reason, 'UNKNOWN_CELL_PLAN')

        # Rule 9: 401/403 marks REJECTED with FLEET_IDENTITY_REJECTED and raises ValidationError.
        client.post_error = _http_error(401, 'UNAUTHORIZED')
        with self.assertRaises(ValidationError):
            cell_plan_dispatch.dispatch_harbor_plan(self.operator, plan_id=self.plan.pk, client=client)
        latest = CellPlanDispatch.objects.order_by('-pk').first()
        self.assertEqual(latest.state, 'REJECTED')
        self.assertEqual(latest.reason, 'FLEET_IDENTITY_REJECTED')

    def test_executor_tick_reconciles_cell_plan_and_drives_two_leg_outbound_pinky(self):
        import tempfile
        client = _FakeCellPlanClient()
        client.post_error = URLError('lost response')
        row = cell_plan_dispatch.dispatch_rosy_directive(
            self.operator, order_id=self.order.pk, observation_id=self.observation.pk,
            directive_mode='outbound', plan_id=self.plan.pk, client=client,
        )
        self.assertEqual(row.state, 'UNCERTAIN')

        with tempfile.TemporaryDirectory() as tmpdir:
            transport = _FakeOutboundTransport(Path(tmpdir) / 'outbound.sqlite3')

            class _Reg:
                def __init__(self, c, t):
                    self.client = c
                    self.transport = t
                    self.config = {'path': str(Path(tmpdir) / 'outbound.sqlite3')}

            reg = _Reg(client, transport)
            client.post_error = None
            client.status = 'COMPLETED'
            env = {'HARBOR_OUTBOUND_AUTO_DISPATCH': '1', 'HARBOR_OUTBOUND_VEHICLES': 'pinky-01,pinky-02'}
            with patch.dict(os.environ, env, clear=False), patch('harbor_web.runtime.execution_block_reason', return_value=''):
                # Tick 1: reconciles UNCERTAIN -> COMPLETED, auto-prepares OutboundDispatch (PICKUP_PLANNED),
                # then outbound_loop.tick proposes Leg 1 S_OUT -> PICKUP_PROPOSED.
                executor.tick(registry=reg)
                row.refresh_from_db()
                self.assertEqual(row.state, 'COMPLETED')
                od = OutboundDispatch.objects.get(cell_plan_dispatch=row)
                self.assertEqual(od.state, 'PICKUP_PROPOSED')
                self.assertEqual(od.assigned_vehicle_id, 'pinky-01')

                # Tick 2: Leg 1 S_OUT arrives -> PICKUP_ARRIVED (stops cleanly awaiting load confirm).
                transport.trip_status = 'GOAL_CONFIRMED'
                executor.tick(registry=reg)
                od.refresh_from_db()
                self.assertEqual(od.state, 'PICKUP_ARRIVED')

                # Named confirmation -> LOADED -> Leg 2 pinned vehicle S_EXIT -> EXITED.
                outbound_loop.confirm_outbound_loaded(self.operator, dispatch_id=od.pk, expected_version=od.version, registry=reg)
                transport.trip_status = 'QUEUED'
                executor.tick(registry=reg)
                od.refresh_from_db()
                self.assertEqual(od.state, 'EXIT_PROPOSED')

                transport.trip_status = 'GOAL_CONFIRMED'
                executor.tick(registry=reg)
                od.refresh_from_db()
                self.assertEqual(od.state, 'EXITED')

    def test_plan_detail_posts_both_cell_plan_actions(self):
        client = _FakeCellPlanClient()
        self.client.force_login(self.operator)
        with self.settings(ROOT_URLCONF='harbor_web.urls'):
            with patch.object(cell_plan_dispatch, '_default_client', return_value=client), patch(
                    'harbor_web.readiness.first_unmet_cell_plan', return_value=''):
                r1 = self.client.post(f'/plans/{self.plan.pk}/', {'action': 'cell_plan_harbor'})
                self.assertEqual(r1.status_code, 302)
                # P0-2 mutex: the active first dispatch blocks a second dispatch for the same order.
                r2 = self.client.post(f'/plans/{self.plan.pk}/', {'action': 'cell_plan_rosy', 'directive_mode': 'outbound'})
                self.assertEqual(r2.status_code, 409)
                self.assertContains(r2, '이미 ROSY에 셀 계획으로 제안되어 진행 중', status_code=409)
                row = CellPlanDispatch.objects.get(order=self.order)
                client.status = 'COMPLETED'
                r3 = self.client.post(f'/plans/{self.plan.pk}/', {'action': 'cell_plan_sync', 'dispatch_id': row.pk})
                self.assertEqual(r3.status_code, 302)
                r4 = self.client.post(f'/plans/{self.plan.pk}/', {'action': 'cell_plan_rosy', 'directive_mode': 'outbound'})
                self.assertEqual(r4.status_code, 302)
        rows = list(CellPlanDispatch.objects.filter(order=self.order).order_by('pk'))
        self.assertEqual([r.order_source for r in rows], ['harbor', 'rosy_r'])
        self.assertEqual([r.state for r in rows], ['COMPLETED', 'COMPLETED'])

    def test_plan_detail_manual_sync_and_outbound_pinky_actions(self):
        import tempfile
        client = _FakeCellPlanClient()
        self.client.force_login(self.operator)
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger_path = Path(tmpdir) / 'manual-outbound.sqlite3'
            transport = _FakeOutboundTransport(ledger_path)

            class _Reg:
                def __init__(self):
                    self.client = client
                    self.transport = transport
                    self.config = {'path': str(ledger_path)}

            with self.settings(ROOT_URLCONF='harbor_web.urls'), patch.dict(
                os.environ, {'HARBOR_OUTBOUND_VEHICLES': 'pinky-01,pinky-02'}, clear=False
            ), patch.object(cell_plan_dispatch, '_default_client', return_value=client), patch(
                'harbor_web.readiness.first_unmet_cell_plan', return_value=''
            ), patch(
                'harbor_web.commands.acquire_registry', return_value=_Reg()
            ):
                client.post_error = URLError('lost response')
                r1 = self.client.post(f'/plans/{self.plan.pk}/', {'action': 'cell_plan_harbor'})
                self.assertEqual(r1.status_code, 302)
                row = CellPlanDispatch.objects.get(order=self.order)
                self.assertEqual(row.state, 'UNCERTAIN')

                page1 = self.client.get(f'/plans/{self.plan.pk}/')
                self.assertEqual(page1.status_code, 200)
                html1 = page1.content.decode('utf-8')
                self.assertIn('응답 확인 중', html1)
                self.assertIn('진행 상태 확인', html1)
                self.assertNotIn('()', html1)

                client.post_error = None
                client.status = 'COMPLETED'
                r2 = self.client.post(f'/plans/{self.plan.pk}/', {'action': 'cell_plan_sync', 'dispatch_id': row.pk})
                self.assertEqual(r2.status_code, 302)
                row.refresh_from_db()
                self.assertEqual(row.state, 'COMPLETED')

                html2 = self.client.get(f'/plans/{self.plan.pk}/').content.decode('utf-8')
                self.assertIn('반출대 차량 호출', html2)

                r3 = self.client.post(f'/plans/{self.plan.pk}/', {'action': 'cell_plan_outbound_dispatch', 'dispatch_id': row.pk})
                self.assertEqual(r3.status_code, 302)
                od = OutboundDispatch.objects.get(cell_plan_dispatch=row)
                self.assertEqual(od.state, 'PICKUP_PLANNED')

                od.state = 'PICKUP_ARRIVED'
                od.assigned_vehicle_id = 'pinky-01'
                od.save(update_fields=['state', 'assigned_vehicle_id'])

                html3 = self.client.get(f'/plans/{self.plan.pk}/').content.decode('utf-8')
                self.assertIn('차량 적재 확인', html3)

                r4 = self.client.post(f'/plans/{self.plan.pk}/', {
                    'action': 'cell_plan_outbound_loaded',
                    'outbound_dispatch_id': od.pk,
                    'expected_version': od.version,
                })
                self.assertEqual(r4.status_code, 302)
                od.refresh_from_db()
                self.assertEqual(od.state, 'LOADED')

    def test_plan_detail_cancels_the_outbound_dispatch_and_frees_the_stand(self):
        import tempfile
        client = _FakeCellPlanClient()
        self.client.force_login(self.operator)
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger_path = Path(tmpdir) / 'cancel-outbound.sqlite3'
            transport = _FakeOutboundTransport(ledger_path)

            class _Reg:
                def __init__(self):
                    self.client = client
                    self.transport = transport
                    self.config = {'path': str(ledger_path)}

            with self.settings(ROOT_URLCONF='harbor_web.urls'), patch.dict(
                os.environ, {'HARBOR_OUTBOUND_VEHICLES': 'pinky-01,pinky-02'}, clear=False
            ), patch.object(cell_plan_dispatch, '_default_client', return_value=client), patch(
                'harbor_web.readiness.first_unmet_cell_plan', return_value=''
            ), patch(
                'harbor_web.commands.acquire_registry', return_value=_Reg()
            ):
                r1 = self.client.post(f'/plans/{self.plan.pk}/', {'action': 'cell_plan_harbor'})
                self.assertEqual(r1.status_code, 302)
                row = CellPlanDispatch.objects.get(order=self.order)
                client.status = 'COMPLETED'
                r2 = self.client.post(f'/plans/{self.plan.pk}/', {'action': 'cell_plan_sync', 'dispatch_id': row.pk})
                self.assertEqual(r2.status_code, 302)
                r3 = self.client.post(f'/plans/{self.plan.pk}/', {'action': 'cell_plan_outbound_dispatch', 'dispatch_id': row.pk})
                self.assertEqual(r3.status_code, 302)
                od = OutboundDispatch.objects.get(cell_plan_dispatch=row)
                self.assertEqual(od.state, 'PICKUP_PLANNED')

                r4 = self.client.post(f'/plans/{self.plan.pk}/', {
                    'action': 'cell_plan_outbound_cancel', 'dispatch_id': row.pk,
                })
                self.assertEqual(r4.status_code, 302)
                od.refresh_from_db()
                self.assertEqual((od.state, od.reason), ('CANCELLED', '운영자 반출 취소'))
                self.assertTrue(Audit.objects.filter(
                    action='outbound.dispatch_cancelled', object_id=str(od.pk)).exists())

                r5 = self.client.post(f'/plans/{self.plan.pk}/', {
                    'action': 'cell_plan_outbound_cancel', 'dispatch_id': row.pk,
                })
                self.assertEqual(r5.status_code, 409)

    def test_preview_cancel_and_hold_reason_propagation(self):
        client = _FakeCellPlanClient()
        dry1 = cell_plan_dispatch.preview_harbor_plan(self.operator, plan_id=self.plan.pk, client=client)
        self.assertEqual(dry1.order_source, 'harbor')
        dry2 = cell_plan_dispatch.preview_rosy_directive(
            self.operator, order_id=self.order.pk, observation_id=self.observation.pk, client=client,
        )
        self.assertEqual(dry2.order_source, 'rosy_r')

        row = cell_plan_dispatch.dispatch_harbor_plan(self.operator, plan_id=self.plan.pk, client=client)
        client.status = 'PAUSED_HUMAN'
        client.hold_reason = 'HUMAN_IN_CELL_ZONE'
        synced = cell_plan_dispatch.sync_cell_plan_dispatch(row.pk, client=client)
        self.assertEqual(synced.state, 'PAUSED_HUMAN')
        self.assertEqual(synced.reason, 'HUMAN_IN_CELL_ZONE')

        # A paused dispatch still occupies the order; close it (ABORTED) before the next request.
        client.status = 'ABORTED'
        self.assertEqual(cell_plan_dispatch.sync_cell_plan_dispatch(row.pk, client=client).state, 'ABORTED')

        client2 = _FakeCellPlanClient()
        row2 = cell_plan_dispatch.dispatch_harbor_plan(self.operator, plan_id=self.plan.pk, client=client2)
        self.client.force_login(self.operator)
        with self.settings(ROOT_URLCONF='harbor_web.urls'), patch.object(cell_plan_dispatch, '_default_client', return_value=client2):
            page_html = self.client.get(f'/plans/{self.plan.pk}/').content.decode('utf-8')
            self.assertIn('셀 계획 취소', page_html)
            resp = self.client.post(f'/plans/{self.plan.pk}/', {
                'action': 'cell_plan_cancel',
                'dispatch_id': row2.pk,
                'reason': '운영자 현장 취소',
            })
            self.assertEqual(resp.status_code, 302)
        row2.refresh_from_db()
        self.assertEqual(row2.state, 'CANCELLED')
        self.assertEqual(row2.reason, '운영자 현장 취소')

    def test_production_dispatch_rejects_restored_runtime_and_unmet_readiness(self):
        with patch('harbor_web.runtime.execution_block_reason', return_value='RESTORE_RECONCILIATION_REQUIRED'):
            with self.assertRaises(services.Conflict) as restore_blocked:
                cell_plan_dispatch.dispatch_harbor_plan(self.operator, plan_id=self.plan.pk)
        self.assertEqual(str(restore_blocked.exception), 'RESTORE_RECONCILIATION_REQUIRED')
        self.assertEqual(CellPlanDispatch.objects.count(), 0)

        with patch('harbor_web.readiness.first_unmet_cell_plan', return_value='READINESS_ARM_INCIDENT_UNMET'):
            with self.assertRaises(services.Conflict) as readiness_blocked:
                cell_plan_dispatch.dispatch_rosy_directive(
                    self.operator, order_id=self.order.pk, observation_id=self.observation.pk,
                )
        self.assertEqual(str(readiness_blocked.exception), 'READINESS_ARM_INCIDENT_UNMET')
        self.assertEqual(CellPlanDispatch.objects.count(), 0)


class AutomaticCellPlanProposalTests(CellPlanDispatchTests):
    """ADR 0034: in automatic operation the executor proposes ready plans by site points, one cell job at a time."""

    def test_proposes_ready_plan_once_as_the_automatic_user(self):
        client = _FakeCellPlanClient()
        row = cell_plan_dispatch.auto_dispatch_next(client=client, production=False)
        self.assertIsNotNone(row)
        self.assertEqual((row.plan_id, row.dispatch_mode, row.state), (self.plan.pk, 'HARBOR_PLAN', 'PROPOSED'))
        self.assertEqual(row.created_by.username, cell_plan_dispatch.AUTO_DISPATCH_USERNAME)
        self.assertFalse(row.created_by.is_active)
        self.assertTrue(all('src_site' in step and 'dst_site' in step for step in row.request_payload['steps']))
        self.assertTrue(Audit.objects.filter(action='cell_plan.auto_proposed', object_id=str(row.pk)).exists())
        # One cell job at a time, and a plan already sent is never resent automatically.
        self.assertIsNone(cell_plan_dispatch.auto_dispatch_next(client=client, production=False))
        CellPlanDispatch.objects.filter(pk=row.pk).update(state='COMPLETED')
        self.assertIsNone(cell_plan_dispatch.auto_dispatch_next(client=client, production=False))
        self.assertEqual(len(client.calls), 1)

    def test_stale_plan_is_not_proposed(self):
        type(self.order).objects.filter(pk=self.order.pk).update(version=self.order.version + 1)
        self.assertIsNone(cell_plan_dispatch.auto_dispatch_next(client=_FakeCellPlanClient(), production=False))

    def test_executor_tick_proposes_when_nothing_is_active(self):
        client = _FakeCellPlanClient()

        class _Reg:
            production = False

            def __init__(self, c):
                self.client = c

        with patch('harbor_web.runtime.execution_block_reason', return_value=''):
            cell_plan_dispatch.tick(_Reg(client))
        self.assertEqual(CellPlanDispatch.objects.filter(plan=self.plan, state='PROPOSED').count(), 1)


class YardFollowsCellPlanTests(TestCase):
    """Completed cell plans derive a fresh DECLARED yard observation so auto_replan and auto_dispatch advance."""

    setUp = CellPlanDispatchTests.setUp
    _inbound_plan = CellPlanDispatchTests._inbound_plan

    def _reg(self, client):
        class _Reg:
            production = False

            def __init__(self, c):
                self.client = c

        return _Reg(client)

    def test_completed_outbound_cell_plan_updates_yard_triggers_auto_replan_and_auto_proposes_next(self):
        from harbor_web.models import Observation, Plan

        # Enable auto_replan on order 1 (C1) and order 2 (C2 stacked on top of C1).
        c2_cargo = CargoFootprint.objects.filter(cargo__code='C2').latest('pk').cargo
        order2 = services.create_order(
            self.operator, cargo=c2_cargo, kind='OUTBOUND', priority_group=1, sequence=2, tie_policy='FIXED',
        )
        plan1 = services.make_plan(
            self.operator, order_id=self.order.pk, expected_version=self.order.version,
            observation_id=self.observation.pk, automatic=True,
        )
        plan2_initial = services.make_plan(
            self.operator, order_id=order2.pk, expected_version=order2.version,
            observation_id=self.observation.pk, automatic=True,
        )
        self.assertEqual(plan1.result['status'], 'READY')
        self.assertEqual(plan2_initial.result['status'], 'HOLD')
        self.assertEqual(plan2_initial.result['reason'], 'PRIORITY_ORDER_BLOCKED')

        client = _FakeCellPlanClient()
        reg = self._reg(client)
        with patch('harbor_web.runtime.execution_block_reason', return_value=''):
            # Tick 1: auto-proposes C1 outbound plan.
            cell_plan_dispatch.tick(reg)
            d1 = CellPlanDispatch.objects.get(order=self.order)
            self.assertEqual(d1.state, 'PROPOSED')

            # Fake ROSY finishes C1 plan (C2 -> temp_a, C1 -> stand, C2 -> yard-r1-c1-l1).
            client.status = 'COMPLETED'
            # Tick 2: sync transitions d1 -> COMPLETED, records derived DECLARED observation,
            # and triggers auto_replan so order2 gets a READY plan on the new yard.
            cell_plan_dispatch.tick(reg)

        d1.refresh_from_db()
        self.assertEqual(d1.state, 'COMPLETED')
        derived = Observation.objects.get(
            source_id=self.observation.source_id,
            source_revision=f'cell-plan-{d1.pk}',
        )
        self.assertEqual(derived.provenance, 'DECLARED')
        self.assertEqual(derived.condition_id, self.condition.pk)
        self.assertEqual(
            derived.payload['derived_from'],
            {'cell_plan_dispatch': d1.pk, 'rosy_plan_id': d1.rosy_plan_id},
        )
        # C1 went to stand and is removed; C2 returned to yard-r1-c1-l1.
        placements = {p['cargo_id']: p for p in derived.payload['placements']}
        self.assertNotIn('C1', placements)
        self.assertEqual(
            (placements['C2']['area_id'], placements['C2']['row'], placements['C2']['column'], placements['C2']['level']),
            ('yard', 1, 1, 1),
        )

        # auto_replan created a fresh READY plan for order2 anchored to the derived observation.
        plan2_next = Plan.objects.filter(order=order2).latest('pk')
        self.assertNotEqual(plan2_next.pk, plan2_initial.pk)
        self.assertEqual(plan2_next.observation_id, derived.pk)
        self.assertEqual(plan2_next.result['status'], 'READY')
        self.assertEqual(len(plan2_next.result['moves']), 1)
        self.assertEqual(plan2_next.result['moves'][0]['cargo_id'], 'C2')

        # Tick 3: auto_dispatch_next proposes order2's new READY plan to ROSY.
        client.status = 'PROPOSED'
        with patch('harbor_web.runtime.execution_block_reason', return_value=''):
            cell_plan_dispatch.tick(reg)
        d2 = CellPlanDispatch.objects.get(order=order2)
        self.assertEqual(d2.plan_id, plan2_next.pk)
        self.assertEqual(d2.observation_id, derived.pk)
        self.assertEqual(d2.state, 'PROPOSED')

        # Idempotent: calling sync or follow again for d1 never records a second observation.
        obs_count = Observation.objects.filter(source_id=self.observation.source_id).count()
        cell_plan_dispatch.sync_cell_plan_dispatch(d1.pk, client=client)
        cell_plan_dispatch._follow_yard_on_completed(d1)
        self.assertEqual(Observation.objects.filter(source_id=self.observation.source_id).count(), obs_count)
        self.assertFalse(Audit.objects.filter(action='yard.follow_skipped', object_id=str(d1.pk)).exists())

    def test_completed_inbound_cell_plan_adds_cargo_to_derived_observation(self):
        from harbor_web.models import Observation

        plan = self._inbound_plan('C3', sequence=2)
        rec = plan.result['recommendation']
        client = _FakeCellPlanClient()
        row = cell_plan_dispatch.dispatch_harbor_plan(self.operator, plan_id=plan.pk, client=client)
        client.status = 'COMPLETED'
        cell_plan_dispatch.sync_cell_plan_dispatch(row.pk, client=client)

        derived = Observation.objects.get(
            source_id=self.observation.source_id,
            source_revision=f'cell-plan-{row.pk}',
        )
        placements = {p['cargo_id']: p for p in derived.payload['placements']}
        self.assertIn('C3', placements)
        self.assertEqual(
            (placements['C3']['area_id'], placements['C3']['row'], placements['C3']['column'], placements['C3']['level']),
            (rec['area_id'], rec['row'], rec['column'], rec['level']),
        )

    def test_completed_rosy_directive_tracks_cargo_by_slot_without_cargo_id(self):
        from harbor_web.models import Observation

        client = _FakeCellPlanClient()
        row = cell_plan_dispatch.dispatch_rosy_directive(
            self.operator, order_id=self.order.pk, observation_id=self.observation.pk,
            directive_mode='outbound', plan_id=self.plan.pk, client=client,
        )
        self.assertTrue(all('cargo_id' not in step for step in row.planned_steps))
        client.status = 'COMPLETED'
        cell_plan_dispatch.sync_cell_plan_dispatch(row.pk, client=client)

        derived = Observation.objects.get(
            source_id=self.observation.source_id,
            source_revision=f'cell-plan-{row.pk}',
        )
        placements = {p['cargo_id']: p for p in derived.payload['placements']}
        self.assertNotIn('C1', placements)
        self.assertEqual(
            (placements['C2']['area_id'], placements['C2']['row'], placements['C2']['column'], placements['C2']['level']),
            ('yard', 1, 1, 1),
        )

    def test_yard_follow_skips_with_single_audit_when_newer_observation_exists_or_steps_fail(self):
        from harbor_web.models import Observation

        client = _FakeCellPlanClient()
        row = cell_plan_dispatch.dispatch_harbor_plan(self.operator, plan_id=self.plan.pk, client=client)
        # Record a newer observation on the same source before the dispatch completes.
        services.record_observation(
            self.operator,
            source_id=self.observation.source_id,
            source_revision='rev-manual-newer',
            observed_at=timezone.now(),
            provenance='DECLARED',
            condition=self.condition,
            payload=self.observation.payload,
        )
        client.status = 'COMPLETED'
        cell_plan_dispatch.sync_cell_plan_dispatch(row.pk, client=client)
        self.assertFalse(
            Observation.objects.filter(source_id=self.observation.source_id, source_revision=f'cell-plan-{row.pk}').exists()
        )
        skips = Audit.objects.filter(action='yard.follow_skipped', object_id=str(row.pk))
        self.assertEqual(skips.count(), 1)
        self.assertEqual(skips.first().detail['reason'], 'NEWER_OBSERVATION_EXISTS')
        # Calling follow again still leaves only one skip audit.
        cell_plan_dispatch._follow_yard_on_completed(row)
        self.assertEqual(skips.count(), 1)

        # Unapplicable step against the latest observation skips with one audit.
        latest_obs = Observation.objects.filter(source_id=self.observation.source_id).latest('pk')
        bad_row = CellPlanDispatch.objects.create(
            order=self.order, plan=self.plan, observation=latest_obs,
            dispatch_mode='HARBOR_PLAN', directive_mode='harbor_plan',
            request_key='rk-bad-steps', request_payload={}, rosy_plan_id='cp-bad',
            order_source='harbor', state='COMPLETED', created_by=self.operator,
            planned_steps=[{'cargo_id': 'C1', 'src_site': 'yard-r3-c4-l1', 'dst_site': 'stand-r1-c1-l1'}],
            rosy_task_ids=['cp-bad-s1'],
        )
        cell_plan_dispatch._follow_yard_on_completed(bad_row)
        self.assertFalse(Observation.objects.filter(source_revision=f'cell-plan-{bad_row.pk}').exists())
        bad_skips = Audit.objects.filter(action='yard.follow_skipped', object_id=str(bad_row.pk))
        self.assertEqual(bad_skips.count(), 1)
        self.assertEqual(bad_skips.first().detail['reason'], 'SRC_SLOT_MISMATCH')

    def test_yard_follow_can_be_disabled_via_env_flag(self):
        from harbor_web.models import Observation

        client = _FakeCellPlanClient()
        row = cell_plan_dispatch.dispatch_harbor_plan(self.operator, plan_id=self.plan.pk, client=client)
        client.status = 'COMPLETED'
        with patch.dict(os.environ, {'HARBOR_YARD_FOLLOWS_CELL_PLANS': '0'}, clear=False):
            cell_plan_dispatch.sync_cell_plan_dispatch(row.pk, client=client)
        self.assertFalse(Observation.objects.filter(source_revision=f'cell-plan-{row.pk}').exists())
        self.assertFalse(Audit.objects.filter(action='yard.follow_skipped', object_id=str(row.pk)).exists())

    def test_derived_from_validation_keeps_spatial_schema_closed(self):
        for bad in (
            {'cell_plan_dispatch': 0, 'rosy_plan_id': 'p-1'},
            {'cell_plan_dispatch': '1', 'rosy_plan_id': 'p-1'},
            {'cell_plan_dispatch': 1, 'rosy_plan_id': 'p-1', 'extra': True},
            'not-a-dict',
        ):
            with self.subTest(derived_from=bad), self.assertRaises(ValidationError):
                services.record_observation(
                    self.operator,
                    source_id=self.observation.source_id,
                    source_revision=f'bad-derived-{abs(hash(str(bad)))}',
                    observed_at=timezone.now(),
                    provenance='DECLARED',
                    condition=self.condition,
                    payload={**self.observation.payload, 'derived_from': bad},
                )

    def test_same_tick_twice_is_idempotent_and_conflict_on_same_revision_succeeds(self):
        from harbor_web.models import Observation

        client = _FakeCellPlanClient()
        reg = self._reg(client)
        row = cell_plan_dispatch.dispatch_harbor_plan(self.operator, plan_id=self.plan.pk, client=client)
        client.status = 'COMPLETED'

        with patch('harbor_web.runtime.execution_block_reason', return_value=''):
            cell_plan_dispatch.tick(reg)
            row.refresh_from_db()
            derived = Observation.objects.get(
                source_id=self.observation.source_id,
                source_revision=f'cell-plan-{row.pk}',
            )
            # Deterministic observed_at matches dispatch completion time (row.updated_at).
            self.assertEqual(derived.observed_at, row.updated_at)

            # Running the same tick again is a no-op (idempotent).
            obs_count = Observation.objects.filter(source_id=self.observation.source_id).count()
            cell_plan_dispatch.tick(reg)
            self.assertEqual(Observation.objects.filter(source_id=self.observation.source_id).count(), obs_count)
            self.assertFalse(Audit.objects.filter(action='yard.follow_skipped', object_id=str(row.pk)).exists())

        # Simulate two concurrent ticks racing past the initial existence check so record_observation raises Conflict.
        original_filter = Observation.objects.filter
        calls = {'n': 0}

        def _raced_filter(*args, **kwargs):
            if kwargs.get('source_revision') == f'cell-plan-{row.pk}':
                calls['n'] += 1
                if calls['n'] == 1:
                    return original_filter(pk=-1)
            return original_filter(*args, **kwargs)

        with patch.object(Observation.objects, 'filter', side_effect=_raced_filter), patch.object(
            services, 'record_observation', side_effect=services.Conflict('Source revision already has different content.')
        ):
            raced_result = cell_plan_dispatch._follow_yard_on_completed(row)
        self.assertIsNotNone(raced_result)
        self.assertEqual(raced_result.pk, derived.pk)
        self.assertFalse(Audit.objects.filter(action='yard.follow_skipped', object_id=str(row.pk)).exists())

    def test_newer_port_event_rebases_when_applicable_and_skips_with_alert_when_not(self):
        from datetime import timedelta
        import re
        from harbor_web.models import Observation

        c3 = services.register_cargo(
            self.operator, code='C3', name='C3', weight_kg=2, volume_m3='0.012', destination='stand', length_cells=3,
        )
        c3_fp = CargoFootprint.objects.filter(cargo=c3).latest('pk')

        # Case 1: newer PORT_EVENT adds C3 at yard-r2-c1-l1 while C1/C2 stay in place -> rebase succeeds.
        client = _FakeCellPlanClient()
        row = cell_plan_dispatch.dispatch_harbor_plan(self.operator, plan_id=self.plan.pk, client=client)
        port_payload_ok = {
            **self.observation.payload,
            'placements': [
                *self.observation.payload['placements'],
                dict(cargo_id='C3', area_id='yard', row=2, column=1, level=1, orientation=0, footprint_revision=c3_fp.pk),
            ],
        }
        pe_ok = Observation.objects.create(
            created_by=None,
            source_id='rosy-port-events',
            source_revision='a' * 64,
            observed_at=timezone.now(),
            provenance='PORT_EVENT',
            condition=self.condition,
            payload=port_payload_ok,
        )
        client.status = 'COMPLETED'
        cell_plan_dispatch.sync_cell_plan_dispatch(row.pk, client=client)

        derived = Observation.objects.get(
            source_id=self.observation.source_id,
            source_revision=f'cell-plan-{row.pk}',
        )
        placements = {p['cargo_id']: p for p in derived.payload['placements']}
        self.assertNotIn('C1', placements)
        self.assertEqual((placements['C2']['area_id'], placements['C2']['row'], placements['C2']['column'], placements['C2']['level']), ('yard', 1, 1, 1))
        self.assertEqual((placements['C3']['area_id'], placements['C3']['row'], placements['C3']['column'], placements['C3']['level']), ('yard', 2, 1, 1))
        self.assertGreaterEqual(derived.observed_at, pe_ok.observed_at)
        self.assertFalse(Audit.objects.filter(action='yard.follow_skipped', object_id=str(row.pk)).exists())

        # Case 2: newer PORT_EVENT removes/replaces source cargo -> skip + alert on /alerts/.
        order_c2 = services.create_order(
            self.operator, cargo=CargoFootprint.objects.filter(cargo__code='C2').latest('pk').cargo,
            kind='OUTBOUND', priority_group=1, sequence=2, tie_policy='FIXED',
        )
        plan_c2 = services.make_plan(
            self.operator, order_id=order_c2.pk, expected_version=order_c2.version, observation_id=derived.pk,
        )
        self.assertEqual(plan_c2.result['status'], 'READY')
        client2 = _FakeCellPlanClient()
        row2 = cell_plan_dispatch.dispatch_harbor_plan(self.operator, plan_id=plan_c2.pk, client=client2)
        # Newer PORT_EVENT no longer has C2 at yard-r1-c1-l1 (replaced by C3 there).
        port_payload_conflict = {
            **derived.payload,
            'placements': [
                dict(cargo_id='C3', area_id='yard', row=1, column=1, level=1, orientation=0, footprint_revision=c3_fp.pk),
            ],
        }
        port_payload_conflict.pop('derived_from', None)
        Observation.objects.create(
            created_by=None,
            source_id='rosy-port-events',
            source_revision='b' * 64,
            observed_at=timezone.now(),
            provenance='PORT_EVENT',
            condition=self.condition,
            payload=port_payload_conflict,
        )
        client2.status = 'COMPLETED'
        cell_plan_dispatch.sync_cell_plan_dispatch(row2.pk, client=client2)

        self.assertFalse(Observation.objects.filter(source_revision=f'cell-plan-{row2.pk}').exists())
        skip_audit = Audit.objects.get(action='yard.follow_skipped', object_id=str(row2.pk))
        self.assertEqual(skip_audit.detail['reason'], 'SRC_SLOT_MISMATCH')

        self.client.force_login(self.operator)
        with self.settings(ROOT_URLCONF='harbor_web.urls'):
            alerts_resp = self.client.get('/alerts/')
        self.assertEqual(alerts_resp.status_code, 200)
        skip_alerts = [a for a in alerts_resp.context['alerts'] if a['title'].startswith('야드 갱신 못 함 · ')]
        self.assertEqual(len(skip_alerts), 1)
        self.assertEqual(skip_alerts[0]['count'], 1)
        self.assertIn('출발 칸의 화물이 계획과 다름', skip_alerts[0]['title'])
        content = alerts_resp.content.decode('utf-8').split('id="alerts-page"', 1)[1]
        visible = re.sub(r'<[^>]+>', '', re.sub(r'<details\b.*?</details>', '', content, flags=re.S))
        self.assertNotRegex(visible, r'관측|원본|인계|대조|회차|#\d+|\bREADY\b|\bHOLD\b|BLOCKED')

        # Recording a newer observation after the skip clears the alert.
        services.record_observation(
            self.operator,
            source_id=self.observation.source_id,
            source_revision='rev-after-skip-resolved',
            observed_at=timezone.now(),
            provenance='DECLARED',
            condition=self.condition,
            payload=port_payload_conflict,
        )
        with self.settings(ROOT_URLCONF='harbor_web.urls'):
            cleared_resp = self.client.get('/alerts/')
        self.assertEqual([a for a in cleared_resp.context['alerts'] if a['title'].startswith('야드 갱신 못 함 · ')], [])

    def test_rearrange_then_outbound_compound_sequence_with_c4_and_canonical_site_id(self):
        from harbor_web.models import Observation

        c3 = services.register_cargo(
            self.operator, code='C3', name='C3', weight_kg=2, volume_m3='0.012', destination='stand', length_cells=3,
        )
        c4 = services.register_cargo(
            self.operator, code='C4', name='C4', weight_kg=2, volume_m3='0.012', destination='stand', length_cells=3,
        )
        fp = {code: CargoFootprint.objects.filter(cargo__code=code).latest('pk').pk for code in ('C1', 'C2', 'C3', 'C4')}
        obs = services.record_observation(
            self.operator,
            source_id='cam-compound',
            source_revision='rev-compound-1',
            observed_at=timezone.now(),
            provenance='DECLARED',
            condition=self.condition,
            payload=dict(
                schema='grid-placement-v1',
                observed_areas=['yard', 'temp_a', 'temp_b', 'stand'],
                holdings=[],
                placements=[
                    dict(cargo_id='C1', area_id='yard', row=1, column=4, level=1, orientation=0, footprint_revision=fp['C1']),
                    dict(cargo_id='C2', area_id='yard', row=1, column=4, level=2, orientation=0, footprint_revision=fp['C2']),
                    dict(cargo_id='C3', area_id='yard', row=2, column=4, level=1, orientation=0, footprint_revision=fp['C3']),
                    dict(cargo_id='C4', area_id='yard', row=2, column=4, level=2, orientation=0, footprint_revision=fp['C4']),
                ],
            ),
        )
        # Step 1 (rearrange): C4 yard:r2-s2-t2 (canonical -> yard-r2-c4-l2) -> temp_b:r1-s1-t1
        # Step 2 (rehandle): C2 yard-r1-c4-l2 -> yard:r2-s2-t2 (yard-r2-c4-l2)
        # Step 3 (outbound): C1 yard-r1-c4-l1 -> stand:r1-s1-t1 (stand-r1-c1-l1)
        # Step 4 (return):   C4 temp_b-r1-c1-l1 -> yard-r1-c4-l1
        row = CellPlanDispatch.objects.create(
            order=self.order,
            plan=self.plan,
            observation=obs,
            dispatch_mode='HARBOR_PLAN',
            directive_mode='harbor_plan',
            request_key='rk-compound-c4',
            request_payload={},
            rosy_plan_id='cp-compound-c4',
            order_source='harbor',
            state='COMPLETED',
            created_by=self.operator,
            planned_steps=[
                {'cargo_id': 'C4', 'src_site': 'yard:r2-s2-t2', 'dst_site': 'temp_b:r1-s1-t1', 'phase_label': '재배치'},
                {'cargo_id': 'C2', 'src_wire_ref': 'yard-r1-c4-l2', 'dst_site': 'yard:r2-s2-t2', 'phase_label': '재취급'},
                {'cargo_id': 'C1', 'src_wire_ref': 'yard-r1-c4-l1', 'dst_site': 'stand:r1-s1-t1', 'phase_label': '반출'},
                {'cargo_id': 'C4', 'src_wire_ref': 'temp_b-r1-c1-l1', 'dst_wire_ref': 'yard-r1-c4-l1', 'phase_label': '복귀'},
            ],
            rosy_task_ids=['t1', 't2', 't3', 't4'],
        )
        derived = cell_plan_dispatch._follow_yard_on_completed(row)
        self.assertIsNotNone(derived)
        placements = {p['cargo_id']: p for p in derived.payload['placements']}
        self.assertNotIn('C1', placements)
        self.assertEqual((placements['C2']['area_id'], placements['C2']['row'], placements['C2']['column'], placements['C2']['level']), ('yard', 2, 4, 2))
        self.assertEqual((placements['C3']['area_id'], placements['C3']['row'], placements['C3']['column'], placements['C3']['level']), ('yard', 2, 4, 1))
        self.assertEqual((placements['C4']['area_id'], placements['C4']['row'], placements['C4']['column'], placements['C4']['level']), ('yard', 1, 4, 1))
        self.assertEqual(placements['C2']['orientation'], 0)
        self.assertEqual(placements['C4']['orientation'], 0)
        self.assertFalse(Audit.objects.filter(action='yard.follow_skipped', object_id=str(row.pk)).exists())

    def test_follow_unexpected_exception_records_skip_and_crash_before_follow_retries_on_next_tick(self):
        from harbor_web.models import Observation

        # Part A: unexpected exception INSIDE _follow_yard_on_completed is caught and recorded as yard.follow_skipped.
        client_a = _FakeCellPlanClient()
        row_a = cell_plan_dispatch.dispatch_harbor_plan(self.operator, plan_id=self.plan.pk, client=client_a)
        client_a.status = 'COMPLETED'
        with patch.object(cell_plan_dispatch, '_apply_planned_steps_to_observation', side_effect=RuntimeError('unexpected-boom')):
            synced_a = cell_plan_dispatch.sync_cell_plan_dispatch(row_a.pk, client=client_a)
        self.assertEqual(synced_a.state, 'COMPLETED')
        self.assertFalse(Observation.objects.filter(source_revision=f'cell-plan-{row_a.pk}').exists())
        skip_a = Audit.objects.get(action='yard.follow_skipped', object_id=str(row_a.pk))
        self.assertEqual(skip_a.detail['reason'], 'UNEXPECTED_ERROR')
        self.assertIn('RuntimeError', skip_a.detail['message'])

        # Part B: crash/exception escaping _follow_yard_on_completed on Tick 1 leaves COMPLETED without obs/skip;
        # Tick 2 catches it up and creates the derived observation.
        c2_cargo = CargoFootprint.objects.filter(cargo__code='C2').latest('pk').cargo
        order_b = services.create_order(
            self.operator, cargo=c2_cargo, kind='OUTBOUND', priority_group=1, sequence=2, tie_policy='FIXED',
        )
        obs_b = services.record_observation(
            self.operator,
            source_id='cam-retry',
            source_revision='rev-retry-1',
            observed_at=timezone.now(),
            provenance='DECLARED',
            condition=self.condition,
            payload=dict(
                schema='grid-placement-v1',
                observed_areas=['yard', 'temp_a', 'temp_b', 'stand'],
                holdings=[],
                placements=[
                    dict(
                        cargo_id='C2', area_id='yard', row=1, column=1, level=1, orientation=0,
                        footprint_revision=CargoFootprint.objects.filter(cargo=c2_cargo).latest('pk').pk,
                    ),
                ],
            ),
        )
        plan_b = services.make_plan(
            self.operator, order_id=order_b.pk, expected_version=order_b.version, observation_id=obs_b.pk,
        )
        client_b = _FakeCellPlanClient()
        reg_b = self._reg(client_b)
        row_b = cell_plan_dispatch.dispatch_harbor_plan(self.operator, plan_id=plan_b.pk, client=client_b)
        client_b.status = 'COMPLETED'

        with patch('harbor_web.runtime.execution_block_reason', return_value=''), patch.object(
            cell_plan_dispatch, '_follow_yard_on_completed', side_effect=RuntimeError('simulated-process-crash')
        ):
            res1 = cell_plan_dispatch.tick(reg_b)
        self.assertEqual(res1[row_b.pk], 'ERROR: RuntimeError')
        row_b.refresh_from_db()
        self.assertEqual(row_b.state, 'COMPLETED')
        self.assertFalse(Observation.objects.filter(source_revision=f'cell-plan-{row_b.pk}').exists())
        self.assertFalse(Audit.objects.filter(action='yard.follow_skipped', object_id=str(row_b.pk)).exists())

        # Next tick retries unfollowed COMPLETED dispatch and updates the yard.
        with patch('harbor_web.runtime.execution_block_reason', return_value=''):
            cell_plan_dispatch.tick(reg_b)
        derived_b = Observation.objects.get(source_id='cam-retry', source_revision=f'cell-plan-{row_b.pk}')
        self.assertEqual(derived_b.payload['placements'], [])
        self.assertEqual(derived_b.observed_at, row_b.updated_at)
        self.assertFalse(Audit.objects.filter(action='yard.follow_skipped', object_id=str(row_b.pk)).exists())


class RefusalTextTests(SimpleTestCase):
    def test_codes_become_operator_text_and_unknown_codes_stay_visible(self):
        from django.core.exceptions import ValidationError
        from harbor_web.presentation import refusal_text
        from harbor_web.service_common import Conflict
        self.assertIn('이미 ROSY에 셀 계획으로', refusal_text(Conflict('ACTIVE_CELL_PLAN_DISPATCH'), 'fb'))
        self.assertIn('이미 ROSY에 셀 계획으로', refusal_text(ValidationError('ACTIVE_CELL_PLAN_DISPATCH'), 'fb'))
        self.assertIn('원래 작업을 대조', refusal_text(Conflict('EXISTING_ORDER_EXECUTION_REQUIRES_RECONCILIATION'), 'fb'))
        self.assertEqual(refusal_text(Conflict('NEW_CODE_X'), 'fb'), 'fb (사유 코드 NEW_CODE_X)')
        self.assertEqual(refusal_text(ValidationError('관측을 다시 고르세요.'), 'fb'), '관측을 다시 고르세요.')
        self.assertEqual(refusal_text(Conflict('Cell plan dispatch cannot be cancelled in its current state.'), 'fb'), 'fb')
