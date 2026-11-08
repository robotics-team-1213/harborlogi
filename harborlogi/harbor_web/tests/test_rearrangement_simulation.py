"""Temporary simulation binding: an R2 plan runs end to end on the simulation path only."""
import json
import os
from pathlib import Path

from django.test import TestCase
from django.utils import timezone

from harbor.process import anchor_address, load_bindings
from harbor_web import commands, executor, rearrangement, services
from harbor_web.models import CargoFootprint
from harbor_web.tests import test_executor
from harbor_web.tests import test_spatial_binding

TEMPLATE = dict(simulation_cell_template=True, workcell_id='cell', instances={'A': 'instance'})


class RearrangementSimulationTests(TestCase):
    setUp = test_spatial_binding.SpatialBindingTests.setUp

    def plan(self, provenance='SYNTHETIC', prefix=''):
        cells = [dict(bay_id=f'Y{r}{c}', row=r, column=c) for r in range(1, 4) for c in range(1, 4)]
        condition = services.register_condition(self.admin, name='Grid', calibration_ref='declared', payload={
            'outbound_stand': 'out', 'temporary_bays': ['temp'], 'conveyor': 'belt',
            'allowed_moves': [['Y11', 'out']], 'max_observation_age_seconds': 60,
            'arm_reach': [['A', c['bay_id']] for c in cells] + [['A', 'temp'], ['A', 'out']],
            'yard_layout': dict(rows=3, columns=3, levels=3, cells=cells),
            'spatial_areas': [dict(area_id=a, rows=3, columns=3, levels=3) for a in ['temp', 'out']],
            'spatial_moves': [['yard', 'yard'], ['yard', 'out'], ['yard', 'temp'], ['temp', 'yard']]})
        placements = []
        # A two-cell cargo bridging two supports: moving it to the floor removes one crossing (G=1).
        for code, length, column, level in [('L1', 1, 1, 1), ('L2', 1, 2, 1), ('BR', 2, 1, 2)]:
            code = prefix + code
            cargo = services.register_cargo(self.operator, code=code, name=code, weight_kg=1, volume_m3=1,
                                            destination='yard', length_cells=length)
            placements.append(dict(cargo_id=code, area_id='yard', row=1, column=column, level=level, orientation=0,
                                   footprint_revision=CargoFootprint.objects.get(cargo=cargo).pk))
        observation = services.record_observation(self.admin, source_id='spatial' + prefix, source_revision='one',
            observed_at=timezone.now(), provenance=provenance, condition=condition,
            payload=dict(schema='grid-placement-v1', observed_areas=['yard', 'temp', 'out'], holdings=[],
                         placements=placements))
        return rearrangement.request_rearrangement(self.operator, observation_id=observation.pk)

    def install(self, *entries):
        Path(os.environ['HARBOR_BINDINGS_PATH']).write_text(json.dumps(list(entries)))

    def test_template_binds_each_move_between_anchor_addresses(self):
        plan = self.plan()
        self.assertEqual(plan.result['status'], 'READY', plan.result['reason'])
        (template,) = load_bindings([TEMPLATE])
        moves = template.moves(plan.result['moves'])
        self.assertEqual(len(moves), len(plan.result['moves']))
        first = plan.result['moves'][0]
        self.assertEqual(moves[0].source_ref, anchor_address(*(first['source_placement'][k]
                                                               for k in ('area_id', 'row', 'column', 'level'))))
        self.assertEqual((moves[0].instance_id, moves[0].yard_arm), ('instance', 'A'))
        for bad in (dict(TEMPLATE, simulation_cell_template=False), dict(TEMPLATE, instances={'C': 'x'})):
            with self.assertRaises(ValueError):
                load_bindings([bad])

    def test_without_template_or_off_the_simulation_path_the_gate_stays(self):
        plan = self.plan()
        self.assertEqual(commands.capability(plan)['reason'], 'SPATIAL_EXECUTION_BINDINGS_REQUIRED')
        self.install(TEMPLATE, dict(TEMPLATE, workcell_id='other'))
        self.assertEqual(commands.capability(plan)['reason'], 'SPATIAL_EXECUTION_BINDINGS_REQUIRED')
        self.install(TEMPLATE)
        declared = self.plan(provenance='DECLARED', prefix='D')
        self.assertEqual(commands.capability(declared)['reason'],
                         'AUTHENTICATED_OBSERVATION_AND_EVIDENCE_PRODUCER_REQUIRED')

    def test_simulated_rearrangement_completes(self):
        plan = self.plan()
        self.install(TEMPLATE)
        self.assertEqual(commands.capability(plan)['reason'], '')
        command = commands.prepare_command(self.operator, plan_id=plan.pk, expected_version=plan.order_version)
        self.assertEqual(command.binding['move_arms'], [['A']] * len(plan.result['moves']))
        self.assertEqual(command.binding['outbound_stand'], plan.result['moves'][-1]['destination'])
        command = commands.submit_command(self.operator, command_id=command.pk, expected_version=command.version)
        self.fleet.status = 'GOAL_CONFIRMED'
        for _ in range(24):
            executor.tick(registry=self.registry, evidence=test_executor.evidence_provider(timezone.now()),
                          now=timezone.now())
            command.refresh_from_db()
            if command.state == 'COMPLETED':
                break
        self.assertEqual(command.state, 'COMPLETED')
        self.assertTrue(all(call[1] == '/api/fleet/arm-transfers/jobs' for call in self.fleet.calls if call[0] == 'POST'))
