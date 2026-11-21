import re

from django.contrib.auth.models import Group, User
from django.test import TestCase

from harbor_web.models import Audit


class RecordTextTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('records-reader')
        self.user.groups.add(Group.objects.get_or_create(name='viewer')[0])
        self.client.force_login(self.user)

    def test_events_are_sentences_and_technical_data_is_collapsed_even_without_detail(self):
        cases = {
            'order.create': '업무를 등록했습니다.',
            'order.auto_replan': '자동 계획 설정을 변경했습니다.',
            'observation.record': '야드 현황을 등록했습니다.',
            'command.prepare': '실행 요청을 준비했습니다.',
            'command.dispatching': 'ROSY Fleet에 실행 요청을 보내기 시작했습니다.',
            'command.reconciled': 'ROSY 실행 결과를 가져왔습니다.',
            'command.completed': '업무 완료를 기록했습니다.',
            'future.action': '새 사건을 기록했습니다.',
        }
        for action in cases:
            Audit.objects.create(action=action, object_type='Command', object_id='42')
        before = list(Audit.objects.values())
        response = self.client.get('/records/')
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        folds = re.findall(r'<details\b[^>]*>.*?</details>', html, flags=re.S)
        visible = re.sub(r'<details\b[^>]*>.*?</details>', '', html, flags=re.S)
        self.assertEqual(len(folds), len(cases))
        for action, sentence in cases.items():
            with self.subTest(action=action):
                self.assertTrue(sentence in visible, f'Missing sentence for {action}')
                self.assertNotIn(action, visible)
                self.assertTrue(any(action in fold and 'open' not in fold.split('>')[0] for fold in folds))
        self.assertNotIn('Command #42', visible)
        self.assertNotIn('관측 원본', visible)
        self.assertNotIn('실행 인계', visible)
        self.assertEqual(list(Audit.objects.values()), before)

    def test_raw_details_keep_extra_fields_and_escape_html(self):
        audit = Audit.objects.create(action='order.update', object_type='Order', object_id='42',
            detail={'before': {'note': '이전 메모'}, 'after': {'note': '새 메모'},
                    'extra': '<script>alert(1)</script>'})
        response = self.client.get('/records/?object_type=Order&q=42')
        html = response.content.decode()
        folds = re.findall(r'<details\b[^>]*>.*?</details>', html, flags=re.S)
        self.assertEqual(len(folds), 1)
        self.assertIn('이전 메모', folds[0])
        self.assertIn('새 메모', folds[0])
        self.assertIn('extra', folds[0])
        self.assertIn('&lt;script&gt;', folds[0])
        self.assertNotIn('<script>alert(1)</script>', html)
        self.assertEqual([item.pk for item in response.context['page']], [audit.pk])

    def test_empty_records_and_read_only_access(self):
        self.assertContains(self.client.get('/records/'), '해당하는 사건 기록이 없습니다')
        self.assertEqual(self.client.post('/records/').status_code, 405)
        self.user.groups.clear()
        self.user.groups.add(Group.objects.get_or_create(name='field')[0])
        self.assertEqual(self.client.get('/records/').status_code, 403)

    def test_all_current_event_kinds_have_human_wording(self):
        from harbor_web.record_text import record_text

        actions = (
            'cargo.register', 'footprint.register', 'condition.register', 'condition.map.register',
            'observation.record', 'measurement.record', 'order.create', 'order.update', 'order.auto_replan',
            'plan.create', 'plan.decide', 'episode.assign', 'episode.reassign', 'episode.confirm',
            'departure.prepare', 'command.prepare', 'command.prepared', 'command.dispatching',
            'command.reconciled', 'command.unknown', 'command.blocked', 'command.cancel_pending',
            'command.cancelled', 'command.completed', 'human_attestation.record', 'scenario.import',
            'fleet.discover', 'fleet.configure', 'fleet.check', 'evidence_producer.configure',
            'rearrange.authorization.enable', 'rearrange.authorization.disable', 'rearrange.hold',
        )
        for action in actions:
            with self.subTest(action=action):
                text = record_text(action)
                self.assertNotEqual(text, '새 사건을 기록했습니다.')
                for technical in ('관측', '원본', '인계', '대조', '회차', 'READY', 'HOLD'):
                    self.assertNotIn(technical, text)

    def test_pagination_preserves_scope_and_sentences_on_later_pages(self):
        Audit.objects.bulk_create([
            Audit(action='cargo.register', object_type='Cargo', object_id='42') for _ in range(26)
        ])
        response = self.client.get('/records/?object_type=Cargo&q=42&page=2')
        self.assertContains(response, '화물을 등록했습니다.', count=1)
        self.assertEqual(len(response.context['page']), 1)
        self.assertEqual(response.context['page'].paginator.count, 26)
        self.assertContains(response, '?object_type=Cargo&amp;q=42&amp;page=1')
