"""The standing executor consumes public facts without gaining observer authority."""
from dataclasses import asdict
import os
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from harbor.process import ArmTransferBinding
from harbor_web import evidence, executor


BINDING = ArmTransferBinding("cargo-1", "yard", "out", "cell-1", "arm-a", "object-1", "src", "dst", "A")
ENVIRONMENT = dict(HARBOR_EXECUTOR_EVIDENCE="physical", HARBOR_PLACEMENT_SOURCES="camera-1",
                   HARBOR_RETREAT_SOURCES="fleet-1")


class PhysicalProviderTests(SimpleTestCase):
    def test_loop_selects_the_explicit_physical_provider(self):
        found = object()
        with patch.dict(os.environ, ENVIRONMENT), patch.object(evidence, "physical_provider", return_value=lambda *args: found):
            self.assertIs(executor._loop_evidence()(object(), {}, {}, "step"), found)

    def test_provider_reads_only_current_step_and_never_manufactures_final_evidence(self):
        client, result = object(), object()
        row = dict(request_key="command-1", step_index=0, mission_id="mission-1", attempt_id="attempt-1",
                   state="WAITING", goal_confirmed=1)
        payload = dict(execution_kind="ARM_TRANSFER", revision=7, moves=[asdict(BINDING)])
        with patch.dict(os.environ, ENVIRONMENT), patch("harbor_web.commands.acquire_registry",
                return_value=SimpleNamespace(client=client)), patch("harbor.physical_evidence.fleet_move_evidence",
                return_value=result) as read:
            provider = evidence.physical_provider()
            self.assertIsNone(provider(object(), row, payload, "final"))
            read.assert_not_called()
            self.assertIs(provider(object(), row, payload, "step"), result)
            read.assert_called_once_with(client, BINDING, "command-1:0", mission_id="mission-1", revision=7,
                placement_source="camera-1", retreat_source="fleet-1")

    def test_missing_or_ambiguous_installed_sources_never_create_a_provider(self):
        for changes in ({"HARBOR_RETREAT_SOURCES": ""},
                        {"HARBOR_PLACEMENT_SOURCES": "camera-1,camera-2"}):
            with self.subTest(changes=changes), patch.dict(os.environ, {**ENVIRONMENT, **changes}), self.assertRaises(ValueError):
                evidence.physical_provider()
