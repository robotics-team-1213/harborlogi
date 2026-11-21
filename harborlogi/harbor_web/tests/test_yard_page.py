"""Yard page upcoming plans, move arrows, and layout tests (WP08)."""
import json
from pathlib import Path
import re

from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.utils import timezone

from harbor_web import rearrangement, services
from harbor_web.models import Cargo, CargoFootprint, Command, Order, Plan
from harbor_web.tests.test_control_board import site_policy
from harbor_web.yard_pages import yard_page_context

ROOT = Path(__file__).resolve().parents[2]


class YardPageTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user('yard-admin')
        self.operator = User.objects.create_user('yard-operator')
        self.viewer = User.objects.create_user('yard-viewer')
        for user, roles in (
            (self.admin, ('admin', 'viewer')),
            (self.operator, ('operator', 'viewer')),
            (self.viewer, ('viewer',)),
        ):
            user.groups.add(*[Group.objects.get_or_create(name=name)[0] for name in roles])
        self.condition = services.register_condition(
            self.admin, name='3x6 site', calibration_ref='synthetic', payload=site_policy(),
        )
        self.now = timezone.now()
        self.placements = []
        for code, row, column, level in (
            ('K1', 1, 1, 1), ('K2', 1, 1, 2),
            ('K3', 1, 4, 1), ('K4', 1, 4, 2),
            ('K5', 2, 1, 1), ('K6', 2, 4, 1),
        ):
            services.register_cargo(
                self.operator, code=code, name=code, weight_kg=2, volume_m3='0.012',
                destination='컨베이어', length_cells=3,
            )
            footprint = CargoFootprint.objects.filter(cargo__code=code).latest('pk')
            self.placements.append(dict(
                cargo_id=code, area_id='yard', row=row, column=column, level=level,
                orientation=0, footprint_revision=footprint.pk,
            ))
        self.observation = services.record_observation(
            self.admin, source_id='site-cam', source_revision='1', observed_at=self.now,
            provenance='SYNTHETIC', condition=self.condition,
            payload=dict(
                schema='grid-placement-v1',
                observed_areas=['yard', 'temp_a', 'temp_b', 'stand'],
                placements=self.placements,
                holdings=[],
            ),
        )
        self.client.force_login(self.operator)

    def test_empty_observation_returns_empty_yard_page_context(self):
        self.assertEqual(yard_page_context(None), dict(upcoming=[], active_arrows=[], active_upcoming=None))

    def test_outbound_rearrange_inbound_plans_and_filters(self):
        outbound = services.create_order(
            self.operator, cargo=Cargo.objects.get(code='K1'), kind='OUTBOUND',
            priority_group=1, sequence=1, tie_policy='FIXED',
        )
        outbound_plan = services.make_plan(
            self.operator, order_id=outbound.pk, expected_version=outbound.version,
            observation_id=self.observation.pk, now=self.now,
        )
        self.assertEqual(outbound_plan.result['status'], 'READY')

        # Completed outbound order must be excluded even if it has a READY plan.
        done_order = services.create_order(
            self.operator, cargo=Cargo.objects.get(code='K5'), kind='OUTBOUND',
            priority_group=2, sequence=1, tie_policy='FIXED',
        )
        done_plan = services.make_plan(
            self.operator, order_id=done_order.pk, expected_version=done_order.version,
            observation_id=self.observation.pk, now=self.now,
        )
        Command.objects.create(
            order=done_order, plan=done_plan, actor=self.operator, plan_version=1,
            ledger_identity='l', fleet_origin='http://fleet.example', fleet_principal='p',
            body_digest='a' * 64, binding={}, state='COMPLETED',
        )

        # Superseded READY plan followed by HOLD on the same observation must be excluded.
        superseded = services.create_order(
            self.operator, cargo=Cargo.objects.get(code='K6'), kind='OUTBOUND',
            priority_group=3, sequence=1, tie_policy='FIXED',
        )
        Plan.objects.create(
            created_by=self.operator, order=superseded, order_version=superseded.version,
            observation=self.observation, condition=self.condition, queue_digest='d1',
            result=dict(status='READY', reason='OK', moves=[]),
        )
        Plan.objects.create(
            created_by=self.operator, order=superseded, order_version=superseded.version,
            observation=self.observation, condition=self.condition, queue_digest='d2',
            result=dict(status='HOLD', reason='PRIORITY_ORDER_BLOCKED', moves=[]),
        )

        # Inbound order with a READY recommendation at 2행 1열 3층 (row=1, column=4, level=3).
        services.register_cargo(
            self.operator, code='N1', name='N1', weight_kg=2, volume_m3='0.012',
            destination='보관', length_cells=3,
        )
        inbound = services.create_order(
            self.operator, cargo=Cargo.objects.get(code='N1'), kind='INBOUND',
            priority_group=1, sequence=2, tie_policy='FIXED',
        )
        inbound_plan = Plan.objects.create(
            created_by=self.operator, order=inbound, order_version=inbound.version,
            observation=self.observation, condition=self.condition, queue_digest='in',
            result=dict(
                plan_kind='inbound-placement',
                status='READY',
                reason='PROPOSAL_REQUIRES_ADMISSION_AND_EVIDENCE',
                recommendation=dict(cargo_id='N1', area_id='yard', row=1, column=4, level=3, orientation=0),
            ),
        )

        # Rearrange order (order.cargo is None) with READY moves.
        rearrange_order = Order.objects.create(created_by=self.operator, kind='REARRANGE')
        rearrange_plan = Plan.objects.create(
            created_by=self.operator, order=rearrange_order, order_version=rearrange_order.version,
            observation=self.observation, condition=self.condition, queue_digest='rr',
            result=dict(
                plan_kind='rearrangement',
                status='READY',
                reason='PROPOSAL_REQUIRES_ADMISSION_AND_EVIDENCE',
                moves=[dict(
                    cargo_id='K4', source='Y14-L2', destination='Y21-L2',
                    primary_arm='A', alternate_arms=['B'],
                    source_placement=dict(cargo_id='K4', area_id='yard', row=1, column=4, level=2, orientation=0),
                    destination_placement=dict(cargo_id='K4', area_id='yard', row=2, column=1, level=2, orientation=0),
                    phase_label='재배치',
                    footprint_revision=CargoFootprint.objects.get(cargo__code='K4').pk,
                )],
            ),
        )

        # Plan on another observation must be excluded when viewing self.observation.
        other_obs = services.record_observation(
            self.admin, source_id='site-cam', source_revision='2', observed_at=self.now,
            provenance='SYNTHETIC', condition=self.condition, payload=self.observation.payload,
        )
        other_order = services.create_order(
            self.operator, cargo=Cargo.objects.get(code='K2'), kind='OUTBOUND',
            priority_group=2, sequence=2, tie_policy='FIXED',
        )
        services.make_plan(
            self.operator, order_id=other_order.pk, expected_version=other_order.version,
            observation_id=other_obs.pk, now=self.now,
        )

        context = yard_page_context(self.observation)
        items = {item['order'].pk: item for item in context['upcoming']}
        self.assertEqual(set(items), {outbound.pk, inbound.pk, rearrange_order.pk})

        out_item = items[outbound.pk]
        self.assertEqual(len(out_item['stages']), len(outbound_plan.result['moves']) + 1)
        self.assertIsNotNone(out_item['scene'])
        self.assertEqual(out_item['scene_id'], f'upcoming-scene-{outbound_plan.pk}')
        self.assertEqual(len(out_item['arrows']), len(outbound_plan.result['moves']))
        for arrow in out_item['arrows']:
            self.assertEqual(set(arrow), {'number', 'cargo', 'phase', 'source', 'destination', 'arm'})
        self.assertTrue(any(a['phase'] == '반출' and a['destination'] == '반출대' for a in out_item['arrows']))

        in_item = items[inbound.pk]
        self.assertEqual(in_item['target_label'], '→ 2행 1열 3층')
        self.assertIsNone(in_item['scene'])
        self.assertEqual(in_item['arrows'], [])

        rr_item = items[rearrange_order.pk]
        self.assertIsNotNone(rr_item['scene'])
        self.assertEqual(len(rr_item['stages']), 2)
        self.assertEqual(rr_item['arrows'][0]['cargo'], 'K4')
        self.assertEqual(rr_item['arrows'][0]['phase'], '재배치')

        response = self.client.get(f'/inventory/?observation={self.observation.pk}')
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn('<h1>야드</h1><p>지금 어디에 무엇이 있나 · 화살은 지금 하는 일 하나</p>', html)
        self.assertIn('class="yard-page-layout"', html)
        self.assertIn('class="yard-upcoming-list"', html)
        self.assertIn('class="inventory-yard yard-inline-3d"', html)
        self.assertIn('data-yard-arrows', html)
        self.assertEqual(html.count('class="yard-topview"'), 1)
        self.assertIn('<details class="fold yard-source-fold">', html)
        self.assertIn('<summary>기준 · 출처 선택(지원 담당자용)</summary>', html)
        self.assertIn(f'id="upcoming-scene-{outbound_plan.pk}"', html)
        self.assertIn(f'id="upcoming-scene-{rearrange_plan.pk}"', html)
        self.assertNotIn(f'id="upcoming-scene-{inbound_plan.pk}"', html)
        self.assertIn('→ 2행 1열 3층', html)
        self.assertIn('data-yard-stages', html)
        self.assertIn(f'data-upcoming-plan="plan-{outbound_plan.pk}"', html)
        self.assertIn(f'data-upcoming-scene="upcoming-scene-{outbound_plan.pk}"', html)
        self.assertIsNone(re.search(r'\sstyle=', html))

    def test_rearrangement_preview_includes_3d_viewer_and_stage_buttons(self):
        # Create a 3x3 spatial observation where bridging cargo BR can rearrange onto the floor (G=1).
        cells = [dict(bay_id=f'Y{r}{c}', row=r, column=c) for r in range(1, 4) for c in range(1, 4)]
        condition = services.register_condition(self.admin, name='Grid R2', calibration_ref='declared', payload={
            'outbound_stand': 'out', 'temporary_bays': ['temp'], 'conveyor': 'belt',
            'allowed_moves': [['Y11', 'out']], 'max_observation_age_seconds': 3600,
            'arm_reach': [['A', c['bay_id']] for c in cells] + [['A', 'temp'], ['A', 'out']],
            'yard_layout': dict(rows=3, columns=3, levels=3, cells=cells),
            'spatial_areas': [dict(area_id=a, rows=3, columns=3, levels=3) for a in ['temp', 'out']],
            'spatial_moves': [['yard', 'yard'], ['yard', 'out'], ['yard', 'temp'], ['temp', 'yard']],
        })
        placements = []
        for code, length, column, level in (('L1', 1, 1, 1), ('L2', 1, 2, 1), ('BR', 2, 1, 2)):
            cargo = services.register_cargo(self.operator, code=code, name=code, weight_kg=1, volume_m3=1,
                                            destination='yard', length_cells=length)
            placements.append(dict(cargo_id=code, area_id='yard', row=1, column=column, level=level, orientation=0,
                                   footprint_revision=CargoFootprint.objects.get(cargo=cargo).pk))
        services.record_observation(
            self.admin, source_id='r2-cam', source_revision='1', observed_at=self.now,
            provenance='SYNTHETIC', condition=condition,
            payload=dict(schema='grid-placement-v1', observed_areas=['yard', 'temp', 'out'],
                         holdings=[], placements=placements),
        )
        response = self.client.get('/rearrangement/')
        self.assertEqual(response.status_code, 200)
        preview = response.context['preview']
        self.assertIsNotNone(preview)
        self.assertEqual(preview['proposal'].status, 'READY')
        self.assertTrue(preview['yard_scene']['frames'])
        self.assertEqual(len(preview['yard_stages']), len(preview['proposal'].moves) + 1)
        html = response.content.decode()
        self.assertIn('data-yard-preview', html)
        self.assertIn('data-yard-3d', html)
        self.assertIn('data-yard-stage="0"', html)
        self.assertIn('data-yard-stage="1"', html)
        self.assertIn('id="yard-scene-data"', html)
        self.assertIsNone(re.search(r'\sstyle=', html))

    def test_active_command_populates_yard_arrows_on_page(self):
        outbound = services.create_order(
            self.operator, cargo=Cargo.objects.get(code='K1'), kind='OUTBOUND',
            priority_group=1, sequence=1, tie_policy='FIXED',
        )
        plan = services.make_plan(
            self.operator, order_id=outbound.pk, expected_version=outbound.version,
            observation_id=self.observation.pk, now=self.now,
        )
        Command.objects.create(
            order=outbound, plan=plan, actor=self.operator, plan_version=1,
            ledger_identity='l', fleet_origin='http://fleet.example', fleet_principal='p',
            body_digest='b' * 64, binding={}, state='DISPATCHING',
        )
        response = self.client.get(f'/inventory/?observation={self.observation.pk}')
        html = response.content.decode()
        self.assertIn('class="yard-arrows-list"', html)
        self.assertIn('K1', html)
        self.assertIn('반출대', html)
        self.assertTrue(response.context['active_arrows'])

    def test_yard_page_css_has_no_raw_hex_or_px_literals(self):
        css = (ROOT / 'harbor_web/static/harbor_web/yard-page.css').read_text(encoding='utf-8')
        self.assertIsNone(re.search(r'#[0-9a-fA-F]{3,8}', css))
        body_without_media = re.sub(r'@media[^{]+\{', '', css)
        self.assertIsNone(re.search(r'\b\d+(?:\.\d+)?px\b', body_without_media))
