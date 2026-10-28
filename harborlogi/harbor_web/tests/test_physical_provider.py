"""The standing executor consumes public facts without gaining observer authority."""
from dataclasses import asdict
from datetime import datetime, timezone
import json
import os
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from harbor.process import ArmTransferBinding
from harbor_web import evidence, executor


NOW = datetime(2030, 1, 1, tzinfo=timezone.utc)
BINDING = ArmTransferBinding("cargo-1", "yard", "out", "cell-1", "arm-a", "object-1", "src", "dst", "A")
CONFIG = dict(policy_digest="c" * 64, arms=[dict(arm="A", workcell_id="cell-1", instance_id="arm-a",
                                               zone_id="shared-zone", joint_names=["joint1", "joint2"])])
ENVIRONMENT = dict(HARBOR_EXECUTOR_EVIDENCE="physical", HARBOR_PLACEMENT_SOURCES="camera-1",
                   HARBOR_RETREAT_SOURCES="fleet-1", HARBOR_PHYSICAL_EVIDENCE=json.dumps(CONFIG))


class PhysicalProviderTests(SimpleTestCase):
    def test_loop_selects_the_explicit_physical_provider(self):
        marker = object()
        with patch.dict(os.environ, ENVIRONMENT), patch.object(evidence, "physical_provider", return_value=marker):
            self.assertIs(executor._loop_evidence(), marker)

    def test_provider_reads_only_current_step_and_never_manufactures_final_evidence(self):
        client, result = object(), object()
        row = dict(request_key="command-1", step_index=0, mission_id="mission-1", attempt_id="attempt-1",
                   state="WAITING", goal_confirmed=1)
        payload = dict(execution_kind="ARM_TRANSFER", revision=7, moves=[asdict(BINDING)], move_arms=[["A"]])
        with patch.dict(os.environ, ENVIRONMENT), patch("harbor_web.commands.acquire_registry",
                return_value=SimpleNamespace(client=client)), patch("harbor.physical_evidence.PhysicalEvidenceAdapter.read",
                return_value=result) as read, patch("harbor_web.evidence.timezone.now", return_value=NOW):
            provider = evidence.physical_provider()
            self.assertIsNone(provider(object(), row, payload, "final"))
            read.assert_not_called()
            self.assertIs(provider(object(), row, payload, "step"), result)
            read.assert_called_once_with(client, BINDING, request_key="command-1:0", mission_id="mission-1",
                attempt_id="attempt-1", plan_revision=7, required_arms=("A",), now=NOW)

    def test_missing_or_ambiguous_installed_sources_never_create_a_provider(self):
        for changes in ({"HARBOR_PHYSICAL_EVIDENCE": ""}, {"HARBOR_RETREAT_SOURCES": ""},
                        {"HARBOR_PLACEMENT_SOURCES": "camera-1,camera-2"}):
            with self.subTest(changes=changes), patch.dict(os.environ, {**ENVIRONMENT, **changes}), self.assertRaises(ValueError):
                evidence.physical_provider()
