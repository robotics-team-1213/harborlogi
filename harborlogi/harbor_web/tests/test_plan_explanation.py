from copy import deepcopy

from django.template.loader import render_to_string
from django.test import SimpleTestCase, TestCase

from harbor_web import services
from harbor_web.models import Audit, CargoFootprint, Observation, Plan
from harbor_web.plan_explanation import explain_plan
from harbor_web.tests import test_autoplanning, test_multicell_services, test_workspace


class LegacyExplanationTests(TestCase):
    setUp = test_workspace.WorkspaceTests.setUp
    cargo = test_workspace.WorkspaceTests.cargo
    condition = test_workspace.WorkspaceTests.condition
    observation = test_workspace.WorkspaceTests.observation
    order = test_workspace.WorkspaceTests.order
    buried_plan = test_workspace.WorkspaceTests.buried_plan

    def copy_plan(self, old, result):
        return Plan.objects.create(created_by=self.operator, order=old.order,
            order_version=old.order_version, observation=old.observation, condition=old.condition,
            queue_digest=old.queue_digest, result=result)

    def test_legacy_routes_are_atomic_and_simulate_stack_levels_without_writes(self):
        plan = self.buried_plan()
        counts = [m.objects.count() for m in (Plan, Observation, Audit)]
        explanation = explain_plan(plan)
        routes = {r['cargo_id']: r for r in explanation['routes']}
        self.assertEqual((explanation['move_count'], explanation['rehandle_count'], explanation['restore_count']), (3, 1, 1))
        self.assertEqual([s['phase'] for s in routes['C2']['steps']], ['재취급', '복귀'])
        self.assertEqual([p['level'] for p in routes['C2']['locations']], [2, 1, 1])
        self.assertEqual([p['bay_id'] for p in routes['C2']['locations']], ['bay', 'temp', 'bay'])
        self.assertEqual(routes['C1']['role'], '대상 반출')
        self.assertEqual([m.objects.count() for m in (Plan, Observation, Audit)], counts)

    def test_previous_diff_changed_destination_and_hold_removal(self):
        old = self.buried_plan()
        result = deepcopy(old.result)
        result['previous_plan_id'] = old.pk
        result['moves'][-1]['destination'] = 'out'
        changed = self.copy_plan(old, result)
        diff = explain_plan(changed)['comparison']
        self.assertEqual(diff['counts']['route_changed'], 1)
        self.assertEqual(diff['counts']['unchanged'], 1)
        self.assertEqual(diff['before_pins']['observation_id'], old.observation_id)
        result = dict(status='HOLD', moves=old.result['moves'], previous_plan_id=changed.pk)
        held = self.copy_plan(old, result)
        explanation = explain_plan(held)
        self.assertEqual(explanation['move_count'], 0)
        self.assertEqual(explanation['routes'], [])
        self.assertEqual(explanation['comparison']['counts']['removed'], 2)
        result = deepcopy(old.result)
        result['previous_plan_id'] = held.pk
        resumed = self.copy_plan(old, result)
        self.assertEqual(explain_plan(resumed)['comparison']['counts']['added'], 2)
        html = render_to_string('plan_explanation.html', dict(explanation=explanation, order=old.order))
        self.assertIn('기존 실행의 취소를 뜻하지 않습니다', html)
        self.assertIn('경로 변경', html)

    def test_old_missing_metadata_and_other_order_reference_are_ignored(self):
        old = self.buried_plan()
        legacy = self.copy_plan(old, dict(status='READY', moves=old.result['moves']))
        self.assertIsNone(explain_plan(legacy)['comparison'])
        other = self.order(cargo=old.order.cargo, sequence=2)
        foreign = Plan.objects.create(created_by=self.operator, order=other, order_version=1,
            observation=old.observation, condition=old.condition, queue_digest=old.queue_digest, result=old.result)
        result = deepcopy(old.result)
        result['previous_plan_id'] = foreign.pk
        self.assertIsNone(explain_plan(self.copy_plan(old, result))['comparison'])


class SpatialExplanationTests(TestCase):
    setUp = test_multicell_services.MulticellServicesTests.setUp
    cargo = test_multicell_services.MulticellServicesTests.cargo
    condition = test_multicell_services.MulticellServicesTests.condition
    observe = test_multicell_services.MulticellServicesTests.observe
    order = test_multicell_services.MulticellServicesTests.order
    payload = test_multicell_services.MulticellServicesTests.payload

    def test_multicell_blocker_is_one_route_and_two_moves(self):
        target, blocker = self.cargo(), self.cargo('blocker', 2)
        payload = self.payload(target)
        payload['placements'].append(dict(payload['placements'][0], cargo_id=blocker.code,
            level=2, footprint_revision=CargoFootprint.objects.get(cargo=blocker).pk))
        observed = self.observe(self.condition(), payload)
        order = self.order(target)
        plan = services.make_plan(self.operator, order_id=order.pk, expected_version=1,
            observation_id=observed.pk, now=self.now)
        self.assertEqual(plan.result['status'], 'READY')
        explanation = explain_plan(plan)
        self.assertEqual(len(explanation['routes']), 2)
        self.assertEqual(explanation['rehandle_count'], 1)
        route = next(r for r in explanation['routes'] if r['cargo_id'] == blocker.code)
        self.assertEqual(len(route['steps']), 2)
        self.assertEqual(route['shape']['length_cells'], 2)
        self.assertEqual(route['locations'][0]['level'], 2)
        self.assertEqual(route['steps'][0]['source']['column'], 1)
        self.assertEqual(route['footprint_revision'], CargoFootprint.objects.get(cargo=blocker).pk)


class InboundExplanationTests(TestCase):
    setUp = test_autoplanning.AutomaticPlanningTests.setUp
    cargo = test_autoplanning.AutomaticPlanningTests.cargo
    condition = test_autoplanning.AutomaticPlanningTests.condition
    observe = test_autoplanning.AutomaticPlanningTests.observe
    enabled = test_autoplanning.AutomaticPlanningTests.enabled

    def test_recommendation_has_no_invented_pickup_and_pins_old_shape(self):
        order, observation, old = self.enabled()
        services.register_footprint(self.operator, cargo_id=order.cargo_id, length_cells=2)
        new = Plan.objects.filter(order=order).latest('pk')
        explanation = explain_plan(new)
        route = explanation['routes'][0]
        self.assertEqual(route['kind'], 'intake-recommendation')
        self.assertIsNone(route['observed_source'])
        self.assertEqual(route['steps'], [])
        self.assertEqual(explanation['move_count'], 0)
        comparison = explanation['comparison']
        self.assertEqual(comparison['before_pins']['footprints'][0]['length_cells'], 1)
        self.assertEqual(comparison['after_pins']['footprints'][0]['length_cells'], 2)
        self.assertEqual(comparison['changes'][0]['status'], 'route_changed')
        self.assertEqual(explain_plan(old)['routes'][0]['shape']['length_cells'], 1)

class AreaLabelTests(SimpleTestCase):
    def test_per_arm_temporary_areas_read_by_name(self):
        from harbor_web.plan_explanation import _location
        for area_id, label in (('temp_a', '임시 장치 A 2층'), ('temp_b', '임시 장치 B 2층'), ('stand', '반출대 2층')):
            self.assertEqual(_location(dict(area_id=area_id, row=1, column=1, level=2))['label'], label)
