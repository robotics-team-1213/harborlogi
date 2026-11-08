from types import SimpleNamespace

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase

from harbor_web import services


def configured_map():
    return dict(outbound_stand='out', temporary_bays=['temp'], conveyor='belt',
        allowed_moves=[], arm_reach=[], max_observation_age_seconds=60,
        yard_layout=dict(rows=1, columns=4, levels=2,
            cells=[dict(bay_id=f'Y{c}', row=1, column=c) for c in range(1, 5)]),
        spatial_areas=[dict(area_id=a, rows=1, columns=2, levels=1) for a in ('temp', 'out', 'store')],
        spatial_moves=[], storage_areas=['yard', 'store'],
        area_presentation={a: dict(label=a, x=0, y=i*3) for i, a in enumerate(('yard', 'temp', 'out', 'store'))})


class MapDomainTests(SimpleTestCase):
    def test_roles_resolved_areas_and_presentation(self):
        payload = configured_map()
        services.constraints(payload)
        self.assertEqual(services.spatial_policy(payload).storage_areas, ('yard', 'store'))
        self.assertEqual(services.map_areas(payload)[0].columns, 4)
        self.assertEqual(services.area_presentation(payload), payload['area_presentation'])

    def test_invalid_roles_overlap_metadata_and_location_collisions(self):
        for change in ('overlap', 'missing', 'nonfinite', 'unknown', 'collision', 'role', 'label', 'bool'):
            payload = configured_map()
            if change == 'overlap': payload['area_presentation']['temp']['y'] = 0
            elif change == 'missing': del payload['area_presentation']['temp']
            elif change == 'nonfinite': payload['area_presentation']['temp']['x'] = float('nan')
            elif change == 'unknown': payload['unexpected'] = 1
            elif change == 'collision': payload['temporary_bays'] = ['Y1']
            elif change == 'role': payload['storage_areas'].append('temp')
            elif change == 'label': payload['area_presentation']['temp']['label'] = 'x'*81
            else: payload['area_presentation']['temp']['x'] = True
            with self.subTest(change=change), self.assertRaises(ValidationError):
                services.constraints(payload)

    def test_editor_metadata_requires_spatial_observation(self):
        observation = SimpleNamespace(payload={'bays': [], 'holdings': []},
            condition=SimpleNamespace(payload=configured_map()))
        with self.assertRaisesMessage(ValidationError, 'grid-placement-v1'):
            services.snapshot(observation)

    def test_old_metadata_automatic_layout_and_defaults(self):
        payload = configured_map()
        del payload['storage_areas'], payload['area_presentation']
        payload['spatial_areas'] = payload['spatial_areas'][:2]
        self.assertEqual(services.spatial_policy(payload).storage_areas, ('yard',))
        presentation = services.area_presentation(payload)
        self.assertEqual((presentation['temp']['x'], presentation['out']['y']), (5.2, 2.2))

    def test_area_and_cell_budget(self):
        payload = configured_map()
        payload['spatial_areas'] = [dict(area_id=f'a{i}', rows=12, columns=12, levels=6) for i in range(5)]
        with self.assertRaises(ValidationError):
            services.map_areas(payload)

    def test_storage_area_cannot_alias_a_primary_cell_reach_identifier(self):
        payload = configured_map()
        payload['spatial_areas'][-1]['area_id'] = 'Y1'
        payload['storage_areas'][-1] = 'Y1'
        payload['area_presentation']['Y1'] = payload['area_presentation'].pop('store')
        with self.assertRaises(ValidationError):
            services.constraints(payload)


class MultiOutboundMapTests(SimpleTestCase):
    """Design 33: one outbound line per arm plate."""

    def example(self):
        import json
        from pathlib import Path
        root = Path(__file__).resolve().parents[2]
        payload = json.loads((root / 'config/spatial-policy.arm-plates.example.json').read_text(encoding='utf-8'))
        payload['yard_layout'] = json.loads((root / 'config/yard-layout.json').read_text(encoding='utf-8'))
        return payload

    def test_arm_plate_example_validates_with_two_outbound_lines(self):
        payload = self.example()
        services.constraints(payload)
        self.assertEqual(services.spatial_policy(payload).outbound_areas, ('a_out', 'b_out'))

    def test_extra_stand_must_be_unique_listed_area_and_needs_spatial_areas(self):
        for change in ('duplicate', 'unlisted', 'no_spatial', 'not_list'):
            payload = self.example()
            if change == 'duplicate': payload['extra_outbound_stands'] = ['a_out']
            elif change == 'unlisted': payload['extra_outbound_stands'] = ['b_out', 'ghost']
            elif change == 'not_list': payload['extra_outbound_stands'] = 'b_out'
            else:
                legacy = configured_map()
                legacy['extra_outbound_stands'] = ['store']
                for key in ('spatial_areas', 'storage_areas', 'area_presentation'):
                    del legacy[key]
                payload = legacy
            with self.subTest(change=change), self.assertRaises(ValidationError):
                services.constraints(payload)

    def test_map_editor_round_trips_several_outbound_areas_and_single_stays_legacy(self):
        from harbor_web.map_config import build_map, editor_draft
        payload = self.example()
        draft = editor_draft(payload)
        self.assertEqual(sorted(a['id'] for a in draft['areas'] if a['role'] == 'outbound'), ['a_out', 'b_out'])
        rebuilt = build_map(payload, draft)['payload']
        self.assertEqual((rebuilt['outbound_stand'], rebuilt['extra_outbound_stands']), ('a_out', ['b_out']))
        for area in draft['areas']:
            if area['id'] == 'b_out':
                area['role'] = 'storage'
        single = build_map(payload, draft)['payload']
        self.assertNotIn('extra_outbound_stands', single)
        for area in draft['areas']:
            area['role'] = 'storage' if area['id'] != 'yard' else area['role']
        with self.assertRaises(ValidationError):
            build_map(payload, draft)

    def test_stack_rules_apply_to_resolved_areas_and_are_validated(self):
        from harbor_web.map_config import build_map, editor_draft
        payload = self.example()
        rules = {a.area_id: a.stack_rule for a in services.map_areas(payload)}
        self.assertEqual((rules['a_tmp'], rules['yard']), ('same', 'any'))
        del payload['stack_rules']
        self.assertEqual({a.stack_rule for a in services.map_areas(payload)}, {'any'})
        for bad in ({'a_tmp': 'sometimes'}, {'ghost': 'same'}, ['a_tmp']):
            payload['stack_rules'] = bad
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                services.constraints(payload)
        payload = self.example()
        draft = editor_draft(payload)
        draft['areas'] = [a for a in draft['areas'] if a['id'] != 'b_sto']
        rebuilt = build_map(payload, draft)['payload']
        self.assertNotIn('b_sto', rebuilt['stack_rules'])
        self.assertEqual(rebuilt['stack_rules']['a_sto'], 'same')
