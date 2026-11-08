from django.test import SimpleTestCase, TestCase

from harbor_web import services
from harbor_web.tests import test_multicell_view, test_yard_grid
from harbor_web.workspace import workspace_context
from harbor_web.yard_occupancy import occupancy_map
from harbor_web.yard_view import spatial_yard_view


class OccupancyMapTests(SimpleTestCase):
    def test_multicell_cargo_is_one_entity_and_steps_cap_at_three(self):
        stage = spatial_yard_view(test_multicell_view.SpatialViewTests.snapshot(self), target='C1')
        moves = [dict(cargo_id='C2', phase_label='재취급'), dict(cargo_id='C1', phase_label='반출')]
        yard = occupancy_map(stage, moves)['areas'][0]
        cells = {(c['row'], c['column']): c for row in yard['rows'] for c in row['cells']}
        self.assertEqual((cells[1, 1]['height'], cells[1, 1]['rehandle']), (2, ['C2']))
        self.assertEqual((cells[1, 2]['height'], cells[1, 2]['rehandle']), (1, []))  # C1 covers 1,2; C2 is not there.
        self.assertEqual([cells[3, c]['height'] for c in (1, 2, 3)], [1, 1, 1])  # C5 spans three columns.
        self.assertEqual(cells[2, 2]['step'], 0)
        self.assertEqual(yard['cargo_count'], 3)
        self.assertEqual(yard['rehandle_cargo'], ['C2'])

    def test_step_never_exceeds_three(self):
        stage = dict(layout=dict(rows=1, columns=1), layers=[dict(rows=[dict(cells=[
            dict(code=f'C{level}', row=1, column=1, level=level)])]) for level in range(1, 6)])
        cell = occupancy_map(stage, [])['areas'][0]['rows'][0]['cells'][0]
        self.assertEqual((cell['height'], cell['step']), (5, 3))

    def test_no_layout_has_no_map(self):
        self.assertIsNone(occupancy_map(dict(layout=None, layers=[]), []))


class OccupancyWorkspaceTests(TestCase):
    setUp = test_yard_grid.ConfiguredYardTests.setUp
    cargo = test_yard_grid.ConfiguredYardTests.cargo
    order = test_yard_grid.ConfiguredYardTests.order
    condition = test_yard_grid.ConfiguredYardTests.condition
    condition_payload = test_yard_grid.ConfiguredYardTests.condition_payload
    grid = test_yard_grid.ConfiguredYardTests.grid

    def test_rehandle_cells_match_the_stored_plan_moves_and_page_states_its_basis(self):
        order, observation = self.grid()
        plan = services.make_plan(self.operator, order_id=order.pk, expected_version=1, observation_id=observation.pk)
        moves = plan.result['moves']
        outbound = next(i for i, m in enumerate(moves) if m['cargo_id'] == order.cargo.code)
        stored = {m['cargo_id'] for m in moves[:outbound]}
        occupancy = workspace_context(order, plan)['yard_occupancy']
        self.assertEqual(stored, {'C2'})
        self.assertEqual(set(occupancy['rehandle_cargo']), stored)
        yard = occupancy['areas'][0]['rows'][0]['cells'][0]
        self.assertEqual((yard['height'], yard['step'], yard['rehandle']), (2, 2, ['C2']))
        marked = [c for a in occupancy['areas'] for r in a['rows'] for c in r['cells'] if c['rehandle']]
        self.assertEqual(len(marked), 1)
        self.client.force_login(self.operator)
        page = self.client.get(f'/requests/{order.pk}/')
        for text in ('행별 적재 · 재취급 예정', '계획 기준 · 실적 아님', 'data-cargo="C2"', 'slot role-rehandle', 'slot role-target'):
            self.assertContains(page, text)

    def test_scene_carries_the_same_stored_rehandle_cargo_for_the_linked_views(self):
        order, observation = self.grid()
        plan = services.make_plan(self.operator, order_id=order.pk, expected_version=1, observation_id=observation.pk)
        context = workspace_context(order, plan)
        self.assertEqual(context['yard_scene']['rehandle'], context['yard_occupancy']['rehandle_cargo'])
        self.assertEqual(context['yard_scene']['rehandle'], ['C2'])

    def test_without_a_plan_nothing_is_marked_for_rehandling(self):
        order, observation = self.grid()
        occupancy = workspace_context(order, None, review_observation=observation)['yard_occupancy']
        self.assertEqual(occupancy['rehandle_cargo'], [])
        self.assertFalse(occupancy['has_moves'])
        self.assertEqual(occupancy['areas'][0]['rows'][0]['cells'][0]['height'], 2)

    def test_non_ready_plan_says_why_nothing_is_marked(self):
        from types import SimpleNamespace
        from django.template.loader import render_to_string
        order, observation = self.grid()
        occupancy = workspace_context(order, None, review_observation=observation)['yard_occupancy']
        html = render_to_string('yard_occupancy.html', dict(yard_occupancy=occupancy,
                                                            plan=SimpleNamespace(result=dict(status='HOLD'))))
        self.assertIn('계획이 READY가 아니어서 재취급 예정 칸을 표시하지 않습니다.', html)
        self.assertNotIn('저장된 계획에 이동이 없어', html)
