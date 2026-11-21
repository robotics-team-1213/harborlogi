"""Ensure visible operating text on all seven main screens avoids forbidden internal terms (WP19)."""
import json
import re
from html.parser import HTMLParser
from pathlib import Path

from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.utils import timezone

from harbor_web import services
from harbor_web.models import Audit, CargoFootprint, Command, FieldEpisode

ROOT = Path(__file__).resolve().parents[2]
FORBIDDEN = re.compile(r'관측|원본|인계|대조|회차|#\d+|\bREADY\b|\bHOLD\b')


class _VisibleAndSummaryParser(HTMLParser):
    """Extract visible text outside <details>, plus each <details>'s first <summary> text."""

    def __init__(self):
        super().__init__()
        self.details_depth = 0
        self.skip_depth = 0
        self.summary_depth = 0
        self.visible_parts = []
        self.summary_parts = []

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self.skip_depth += 1
        elif tag == 'details':
            self.details_depth += 1
        elif tag == 'summary' and self.details_depth == 1:
            self.summary_depth += 1

    def handle_endtag(self, tag):
        if tag in ('script', 'style') and self.skip_depth:
            self.skip_depth -= 1
        elif tag == 'summary' and self.summary_depth:
            self.summary_depth -= 1
        elif tag == 'details' and self.details_depth:
            self.details_depth -= 1

    def handle_data(self, data):
        if self.skip_depth:
            return
        if self.details_depth == 0:
            self.visible_parts.append(data)
        elif self.summary_depth:
            self.summary_parts.append(data)


def _visible_and_summary_text(html: str) -> tuple[str, str]:
    parser = _VisibleAndSummaryParser()
    parser.feed(html)
    parser.close()
    return (
        re.sub(r'\s+', ' ', ' '.join(parser.visible_parts)).strip(),
        re.sub(r'\s+', ' ', ' '.join(parser.summary_parts)).strip(),
    )


class GlossarySweepTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('glossary-admin-op')
        for role in ('admin', 'operator', 'viewer'):
            self.user.groups.add(Group.objects.get_or_create(name=role)[0])
        self.field_user = User.objects.create_user('glossary-field')
        self.field_user.groups.add(Group.objects.get_or_create(name='field')[0])

        policy = json.loads((ROOT / 'config/spatial-policy-3x6.json').read_text(encoding='utf-8'))
        policy['yard_layout'] = json.loads((ROOT / 'config/yard-layout-3x6.json').read_text(encoding='utf-8'))
        self.condition = services.register_condition(
            self.user, name='3x6 현장 기준', calibration_ref='synthetic', payload=policy
        )
        for code in ('C1', 'C2', 'C3'):
            services.register_cargo(
                self.user, code=code, name=code, weight_kg=2, volume_m3='0.012', destination='출하장', length_cells=3
            )
        c1, c2, c3 = [CargoFootprint.objects.filter(cargo__code=c).latest('pk') for c in ('C1', 'C2', 'C3')]
        self.observation = services.record_observation(
            self.user,
            source_id='cam-top',
            source_revision='1',
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
        outbound = services.create_order(
            self.user, cargo=c1.cargo, kind='OUTBOUND', priority_group=1, sequence=1, tie_policy='FIXED'
        )
        inbound = services.create_order(
            self.user, cargo=c3.cargo, kind='INBOUND', priority_group=2, sequence=1, tie_policy='FIXED'
        )
        ready_plan = services.make_plan(
            self.user, order_id=outbound.pk, expected_version=1, observation_id=self.observation.pk
        )
        Command.objects.create(
            order=outbound,
            plan=ready_plan,
            actor=self.user,
            plan_version=1,
            ledger_identity='l',
            fleet_origin='f',
            fleet_principal='p',
            body_digest='a' * 64,
            binding={},
            state='UNKNOWN',
        )
        services.make_plan(self.user, order_id=inbound.pk, expected_version=1, observation_id=self.observation.pk)
        FieldEpisode.objects.create(
            order=inbound, assigned_to=self.field_user, vehicle_id='P1', created_by=self.user, state='OPEN'
        )
        Audit.objects.create(actor=self.user, action='command.dispatching', object_type='Command', object_id='1')
        self.client.force_login(self.user)

    def test_seven_main_operating_screens_avoid_forbidden_terms_outside_details(self):
        routes = ('/', '/work/', '/inventory/', '/alerts/', '/records/', '/settings/', '/workflow/')
        for route in routes:
            with self.subTest(route=route):
                response = self.client.get(route)
                self.assertEqual(response.status_code, 200)
                html = response.content.decode()
                visible, summaries = _visible_and_summary_text(html)
                self.assertNotRegex(visible, FORBIDDEN, f'{route} visible text matched forbidden term: {visible}')
                self.assertNotRegex(summaries, FORBIDDEN, f'{route} summary text matched forbidden term: {summaries}')

    def test_empty_inventory_avoids_forbidden_terms_outside_details(self):
        unobserved = services.register_condition(
            self.user, name='빈 현장 기준', calibration_ref='synthetic-empty', payload=self.condition.payload
        )
        response = self.client.get(f'/inventory/?condition={unobserved.pk}')
        self.assertEqual(response.status_code, 200)
        visible, summaries = _visible_and_summary_text(response.content.decode())
        self.assertNotRegex(visible, FORBIDDEN)
        self.assertNotRegex(summaries, FORBIDDEN)
