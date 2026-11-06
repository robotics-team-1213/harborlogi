"""Outbound queue editing by lanes, request titles and first-place registration (KAN-115)."""
from unittest.mock import patch

from django.contrib.auth.models import Group, User
from django.test import TestCase

from harbor_web import services
from harbor_web.models import Audit, Cargo, Order


class QueueEditingTests(TestCase):
    def setUp(self):
        self.operator = User.objects.create_user('operator')
        self.operator.groups.add(Group.objects.get_or_create(name='operator')[0])
        self.viewer = User.objects.create_user('viewer')
        self.viewer.groups.add(Group.objects.get_or_create(name='viewer')[0])

    def cargo(self, code):
        return services.register_cargo(self.operator, code=code, name=f'{code} 화물', weight_kg=1, volume_m3='0.010', destination='반출대')

    def order(self, code, group, sequence, tie_policy='FIXED', **extra):
        return services.create_order(self.operator, cargo=self.cargo(code), kind='OUTBOUND',
                                     priority_group=group, sequence=sequence, tie_policy=tie_policy, **extra)

    def lanes(self):
        return [(o.priority_group, o.sequence, o.tie_policy, o.cargo.code)
                for o in Order.objects.filter(kind='OUTBOUND').select_related('cargo').order_by('priority_group', 'sequence', 'pk')]

    def test_reorder_renumbers_lanes_and_bumps_only_changed_orders(self):
        a, b, c = self.order('A', 1, 1), self.order('B', 1, 2), self.order('C', 2, 1)
        services.reorder_outbound(self.operator, lanes=[
            dict(fixed=True, orders=[(c.pk, c.version)]),
            dict(fixed=False, orders=[(a.pk, a.version), (b.pk, b.version)]),
        ])
        self.assertEqual(self.lanes(), [(1, 1, 'FIXED', 'C'), (2, 1, 'ANY', 'A'), (2, 2, 'ANY', 'B')])
        for order, version in ((a, 2), (b, 2), (c, 2)):
            order.refresh_from_db()
            self.assertEqual(order.version, version)
        update = Audit.objects.filter(action='order.update', object_id=str(c.pk)).get().detail
        self.assertEqual((update['before']['priority_group'], update['after']['priority_group']), (2, 1))

    def test_unchanged_order_keeps_its_version(self):
        a, b = self.order('A', 1, 1), self.order('B', 2, 1)
        services.reorder_outbound(self.operator, lanes=[dict(fixed=True, orders=[(a.pk, 1)]), dict(fixed=True, orders=[(b.pk, 1)])])
        a.refresh_from_db(); b.refresh_from_db()
        self.assertEqual((a.version, b.version), (1, 1))
        self.assertFalse(Audit.objects.filter(action='order.update').exists())

    def closing(self, *orders):
        return patch('harbor_web.order_queue.completed_order_ids', return_value=[order.pk for order in orders])

    def test_swap_inside_one_lane_leaves_other_lanes_alone_across_a_gap(self):
        a, closed, c, d = self.order('A', 1, 1), self.order('X', 2, 1), self.order('C', 3, 1), self.order('D', 3, 2)
        with self.closing(closed):
            services.reorder_outbound(self.operator, lanes=[dict(fixed=True, orders=[(a.pk, 1)]),
                                                            dict(fixed=True, orders=[(d.pk, 1), (c.pk, 1)])])
        self.assertEqual(self.lanes(), [(1, 1, 'FIXED', 'A'), (2, 1, 'FIXED', 'X'), (3, 1, 'FIXED', 'D'), (3, 2, 'FIXED', 'C')])
        a.refresh_from_db(); c.refresh_from_db()
        self.assertEqual((a.version, c.version), (1, 2))
        self.assertFalse(Audit.objects.filter(action='order.update', object_id=str(a.pk)).exists())

    def test_saving_an_unchanged_queue_with_a_gap_changes_nothing(self):
        a, closed, c = self.order('A', 1, 1), self.order('X', 2, 1), self.order('C', 3, 1)
        with self.closing(closed):
            changed = services.reorder_outbound(self.operator, lanes=[dict(fixed=True, orders=[(a.pk, 1)]), dict(fixed=True, orders=[(c.pk, 1)])])
        self.assertEqual(changed, [])
        self.assertFalse(Audit.objects.filter(action='order.update').exists())

    def test_moved_lanes_still_renumber_above_finished_orders(self):
        a, closed, c = self.order('A', 1, 1), self.order('X', 2, 1), self.order('C', 3, 1)
        with self.closing(closed):
            services.reorder_outbound(self.operator, lanes=[dict(fixed=True, orders=[(c.pk, 1)]), dict(fixed=True, orders=[(a.pk, 1)])])
        self.assertEqual(self.lanes(), [(2, 1, 'FIXED', 'X'), (3, 1, 'FIXED', 'C'), (4, 1, 'FIXED', 'A')])

    def test_swap_beside_a_finished_order_stays_above_its_sequence(self):
        closed, b, c = self.order('X', 1, 1), self.order('B', 1, 2), self.order('C', 1, 3)
        with self.closing(closed):
            services.reorder_outbound(self.operator, lanes=[dict(fixed=True, orders=[(c.pk, 1), (b.pk, 1)])])
        self.assertEqual(self.lanes(), [(1, 1, 'FIXED', 'X'), (1, 2, 'FIXED', 'C'), (1, 3, 'FIXED', 'B')])

    def test_policy_change_beside_a_finished_order_moves_to_a_new_group(self):
        closed, b = self.order('X', 1, 1), self.order('B', 1, 2)
        with self.closing(closed):
            services.reorder_outbound(self.operator, lanes=[dict(fixed=False, orders=[(b.pk, 1)])])
        self.assertEqual(self.lanes(), [(1, 1, 'FIXED', 'X'), (2, 1, 'ANY', 'B')])

    def test_any_lane_turning_fixed_gets_distinct_sequences(self):
        a, b = self.order('A', 1, 1, 'ANY'), self.order('B', 1, 1, 'ANY')
        services.reorder_outbound(self.operator, lanes=[dict(fixed=True, orders=[(a.pk, 1), (b.pk, 1)])])
        self.assertEqual(self.lanes(), [(1, 1, 'FIXED', 'A'), (1, 2, 'FIXED', 'B')])

    def test_stale_version_is_a_conflict_and_changes_nothing(self):
        a, b = self.order('A', 1, 1), self.order('B', 2, 1)
        with self.assertRaises(services.Conflict):
            services.reorder_outbound(self.operator, lanes=[dict(fixed=True, orders=[(b.pk, 1), (a.pk, 9)])])
        self.assertEqual(self.lanes(), [(1, 1, 'FIXED', 'A'), (2, 1, 'FIXED', 'B')])

    def test_lanes_must_list_every_open_outbound_order_once(self):
        a, b = self.order('A', 1, 1), self.order('B', 2, 1)
        for lanes in ([dict(fixed=True, orders=[(a.pk, 1)])],
                      [dict(fixed=True, orders=[(a.pk, 1), (a.pk, 1), (b.pk, 1)])],
                      [dict(fixed=True, orders=[(a.pk, 1), (b.pk, 1)]), dict(fixed=True, orders=[])]):
            with self.assertRaises(services.Conflict):
                services.reorder_outbound(self.operator, lanes=lanes)

    def test_reorder_requires_operator(self):
        a = self.order('A', 1, 1)
        from django.core.exceptions import PermissionDenied
        with self.assertRaises(PermissionDenied):
            services.reorder_outbound(self.viewer, lanes=[dict(fixed=True, orders=[(a.pk, 1)])])

    def test_title_is_saved_and_kept_in_request_snapshot(self):
        order = self.order('A', 1, 1, title='본선 1차 선적')
        self.assertEqual(order.title, '본선 1차 선적')
        created = Audit.objects.get(action='order.create').detail['after']
        self.assertEqual(created['title'], '본선 1차 선적')
        changed = services.update_order(self.operator, order_id=order.pk, expected_version=1, priority_group=1, sequence=1,
                                        tie_policy='FIXED', note='', title='변경된 제목')
        self.assertEqual(changed.title, '변경된 제목')
        same = services.update_order(self.operator, order_id=order.pk, expected_version=2, priority_group=1, sequence=1, tie_policy='FIXED')
        self.assertEqual(same.title, '변경된 제목')

    def test_new_outbound_request_defaults_to_the_back_of_the_queue(self):
        self.order('A', 1, 1)
        self.order('B', 3, 1)
        self.client.force_login(self.operator)
        response = self.client.get('/requests/new/?kind=OUTBOUND')
        self.assertContains(response, 'name="priority_group" value="4"')
        self.assertContains(response, 'data-queue-lane')
        cargo = self.cargo('C')
        response = self.client.post('/requests/new/', dict(cargo=cargo.pk, kind='OUTBOUND', title='야간 반출', priority_group=4, sequence=1, tie_policy='FIXED'))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.lanes()[-1], (4, 1, 'FIXED', 'C'))

    def test_first_place_registration_moves_the_queue_back_by_one_lane(self):
        self.order('A', 1, 1)
        self.order('B', 1, 2)
        cargo = self.cargo('C')
        self.client.force_login(self.operator)
        response = self.client.post('/requests/new/', dict(cargo=cargo.pk, kind='OUTBOUND', priority_group=2, sequence=1,
                                                           tie_policy='FIXED', make_first='on'))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.lanes(), [(1, 1, 'FIXED', 'C'), (2, 1, 'FIXED', 'A'), (2, 2, 'FIXED', 'B')])

    def test_unchecked_fixed_order_box_saves_any(self):
        cargo = self.cargo('A')
        self.client.force_login(self.operator)
        self.client.post('/requests/new/', dict(cargo=cargo.pk, kind='OUTBOUND', priority_group=1, sequence=1))
        self.assertEqual(Order.objects.get().tie_policy, 'ANY')

    def test_first_place_box_is_marked_outbound_only(self):
        self.client.force_login(self.operator)
        self.assertContains(self.client.get('/requests/new/'), 'data-outbound-only')
        self.assertContains(self.client.get('/requests/new/?kind=INBOUND'), 'name="make_first"')

    def test_outbound_page_posts_lane_order(self):
        a, b = self.order('A', 1, 1, title='첫 요청'), self.order('B', 2, 1)
        self.client.force_login(self.operator)
        page = self.client.get('/outbound/')
        self.assertContains(page, 'data-queue-editor')
        self.assertContains(page, '첫 요청')
        response = self.client.post('/outbound/reorder/', dict(lane=[f'ANY|{b.pk}:1,{a.pk}:1']))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.lanes(), [(1, 1, 'ANY', 'B'), (1, 2, 'ANY', 'A')])

    def test_outbound_reorder_rejects_malformed_and_stale_input(self):
        a = self.order('A', 1, 1)
        self.client.force_login(self.operator)
        self.assertEqual(self.client.post('/outbound/reorder/', dict(lane=['FIXED|x:1'])).status_code, 400)
        self.assertEqual(self.client.post('/outbound/reorder/', dict(lane=[f'FIXED|{a.pk}:7'])).status_code, 409)
        self.client.force_login(self.viewer)
        self.assertEqual(self.client.post('/outbound/reorder/', dict(lane=[f'FIXED|{a.pk}:1'])).status_code, 403)
