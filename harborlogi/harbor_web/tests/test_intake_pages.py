import os
from pathlib import Path
import tempfile
from unittest.mock import patch

from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.utils import timezone

from harbor.port_events import IntakeLedger
from harbor_web import services
from harbor_web.models import Audit
from harbor_web.tests import test_services as fixtures


class IntakePagesTests(TestCase):
    setUp = fixtures.ServicesTests.setUp
    cargo = fixtures.ServicesTests.cargo
    condition = fixtures.ServicesTests.condition
    observation = fixtures.ServicesTests.observation

    def _ledger(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / 'intake.sqlite3'
        ledger = IntakeLedger(path)
        env = patch.dict(os.environ, {'HARBOR_LEDGER_PATH': str(path)})
        env.start()
        self.addCleanup(env.stop)
        return ledger

    def _event(self, kind, *, object_ref, reason=None, suffix='1'):
        from hashlib import sha256
        return dict(
            event_id=sha256(f'{object_ref}|{kind}|{reason}|{suffix}'.encode()).hexdigest(),
            kind=kind,
            observation_ref=f'obs-{suffix}',
            object_ref=object_ref,
            intake_point='intake' if kind == 'container.received_at_intake' else None,
            slot_ref=None,
            vehicle_id=None,
            reason=reason,
            identity='observed' if kind == 'container.received_at_intake' else None,
            observed_at='2026-11-21T03:00:00Z',
        )

    def test_missing_ledger_path_shows_unlinked_without_breaking_alerts(self):
        self.cargo('C1')
        self.observation(code='C1')
        self.client.force_login(self.operator)
        with patch.dict(os.environ, {'HARBOR_LEDGER_PATH': ''}):
            alerts = self.client.get('/alerts/')
            self.assertEqual(alerts.status_code, 200)
            self.assertContains(alerts, '연동 전')
            self.assertEqual(alerts.context['alert_total'], 0)
            review = self.client.get('/intake/C1/')
            self.assertEqual(review.status_code, 200)
            self.assertContains(review, '연동 전')
            post = self.client.post('/intake/C1/', {'confirm': '1'})
            self.assertEqual(post.status_code, 503)

    def test_holds_in_alerts_and_unreadable_attest_vs_mismatch_lost_view_only(self):
        ledger = self._ledger()
        now = timezone.now()
        self.cargo('C-UNREAD')
        self.cargo('C-MISMATCH')
        self.cargo('C-LOST')
        self.observation(code='C-UNREAD')
        for code in ('C-UNREAD', 'C-MISMATCH', 'C-LOST'):
            ledger.expect(code, now=now)
            ledger.arrive(code, now=now)
        from harbor.port_events import parse_event
        ledger.consume([
            parse_event(self._event('container.handling_failed', object_ref='C-UNREAD', reason='UNREADABLE', suffix='u')),
            parse_event(self._event('container.handling_failed', object_ref='C-MISMATCH', reason='MISMATCH', suffix='m')),
            parse_event(self._event('container.handling_failed', object_ref='C-LOST', reason='LOST', suffix='l')),
        ], now=now)

        self.client.force_login(self.operator)
        alerts = self.client.get('/alerts/')
        self.assertEqual(alerts.context['alert_total'], 3)
        for text in ('C-UNREAD · 반입 번호 못 읽음', 'C-MISMATCH · ROSY 인식이 다름', 'C-LOST · ROSY 사건이 안 옴',
                     'data-intake-dialog="/intake/C-UNREAD/"'):
            self.assertContains(alerts, text)

        # UNREADABLE popup/page shows Harbor plan + topview panel + attestation form
        unread_page = self.client.get('/intake/C-UNREAD/?fragment=1')
        self.assertEqual(unread_page.status_code, 200)
        self.assertContains(unread_page, 'Harbor 야드 평면')
        self.assertContains(unread_page, 'ROSY 탑뷰')
        self.assertContains(unread_page, '기명 확인으로 반입 접수')

        # MISMATCH & LOST are view-only: no attestation form and POST is rejected
        for code in ('C-MISMATCH', 'C-LOST'):
            view_page = self.client.get(f'/intake/{code}/')
            self.assertEqual(view_page.status_code, 200)
            self.assertContains(view_page, '보기만 가능합니다')
            self.assertNotContains(view_page, '기명 확인으로 반입 접수')
            self.assertEqual(self.client.post(f'/intake/{code}/', {'confirm': '1'}).status_code, 400)

        # Viewer/field roles cannot attest UNREADABLE
        viewer = User.objects.create_user('viewer-intake')
        viewer.groups.add(Group.objects.get_or_create(name='viewer')[0])
        self.client.force_login(viewer)
        self.assertEqual(self.client.get('/intake/C-UNREAD/').status_code, 200)
        self.assertNotContains(self.client.get('/intake/C-UNREAD/'), '기명 확인으로 반입 접수')
        self.assertEqual(self.client.post('/intake/C-UNREAD/', {'confirm': '1'}).status_code, 403)

        # Operator attest_received works only with confirmation checked
        self.client.force_login(self.operator)
        self.assertEqual(self.client.post('/intake/C-UNREAD/', {}).status_code, 400)
        resp = self.client.post('/intake/C-UNREAD/', {'confirm': '1', 'next': 'alerts'})
        self.assertRedirects(resp, '/alerts/', fetch_redirect_response=False)
        row = ledger.read('C-UNREAD')
        self.assertEqual((row['state'], row['identity'], row['attested_by'], row['attention']),
                         ('RECEIVED_AT_INTAKE', 'attested', self.operator.username, None))
        self.assertTrue(Audit.objects.filter(action='intake.attested_received', object_id='C-UNREAD').exists())
        self.assertEqual(self.client.get('/alerts/').context['alert_total'], 2)
