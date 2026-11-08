from datetime import timedelta

from django.test import TestCase

from harbor_web.cargo_history import cargo_history
from harbor_web.models import Audit, Cargo, Command, Plan
from harbor_web.tests import test_services as fixtures
from harbor_web.tests import test_workspace as workspace_fixtures


class CargoHistoryTests(TestCase):
    setUp = fixtures.ServicesTests.setUp
    cargo = fixtures.ServicesTests.cargo
    condition = fixtures.ServicesTests.condition
    observation = fixtures.ServicesTests.observation
    order = fixtures.ServicesTests.order
    buried_plan = workspace_fixtures.WorkspaceTests.buried_plan

    def test_target_and_rehandled_blocker_each_show_their_own_records_without_writing(self):
        plan = self.buried_plan()
        target, blocker = plan.order.cargo, Cargo.objects.get(code=next(
            m['cargo_id'] for m in plan.result['moves'] if m['cargo_id'] != plan.order.cargo.code))
        before = (Audit.objects.count(), Plan.objects.count(), Command.objects.count())
        own, other = cargo_history(target), cargo_history(blocker)
        self.assertEqual(before, (Audit.objects.count(), Plan.objects.count(), Command.objects.count()))
        self.assertLessEqual({'등록', '요청', '관측'}, {e['kind'] for e in own['events']})
        self.assertIn(f'Plan #{plan.pk}', [e['source'] for e in own['events'] if e['kind'] == '계획'])
        rehandle = [e for e in other['events'] if e['kind'] == '계획']
        self.assertEqual(len(rehandle), 1)
        self.assertIn('재취급 대상', rehandle[0]['text'])
        self.assertEqual(own['dwell']['count'], 1)
        self.assertEqual(own['dwell']['span'], timedelta(0))
        self.assertTrue(all(own['events'][i]['at'] <= own['events'][i + 1]['at'] for i in range(len(own['events']) - 1)))

    def test_dwell_is_the_observed_span_and_an_unseen_cargo_has_none(self):
        plan = self.buried_plan()
        earlier = self.observation(payload=dict(bays=[
            dict(bay_id='bay', cargo=['C1', 'C2'], capacity=3), dict(bay_id='temp', cargo=[], capacity=2),
            dict(bay_id='out', cargo=[], capacity=1)], holdings=[]),
            source_id='camera-2', observed_at=self.now - timedelta(hours=5))
        dwell = cargo_history(plan.order.cargo)['dwell']
        self.assertEqual((dwell['count'], dwell['span']), (2, timedelta(hours=5)))
        self.assertEqual((dwell['first'], dwell['last']), (earlier.observed_at, plan.observation.observed_at))
        self.assertIsNone(cargo_history(self.cargo('UNSEEN'))['dwell'])

    def test_page_is_read_only_for_viewers_and_linked_from_the_cargo_list(self):
        plan = self.buried_plan()
        self.client.force_login(self.operator)
        response = self.client.get(f'/cargo/{plan.order.cargo.pk}/history/')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '관측 기준 체류')
        self.assertContains(self.client.get('/cargo/'), f'/cargo/{plan.order.cargo.pk}/history/')
        self.assertEqual(self.client.post(f'/cargo/{plan.order.cargo.pk}/history/').status_code, 405)
        self.assertEqual(self.client.get('/cargo/999999/history/').status_code, 404)
