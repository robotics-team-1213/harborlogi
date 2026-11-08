"""Explicit independent Harbor consumer of the simulation ARM HTTP contract."""
import json
import ipaddress
import os
from pathlib import Path
import tempfile
import sys
import time
import unittest
from threading import Thread
from wsgiref.simple_server import make_server
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from urllib.parse import urlsplit

from harbor.process import ArmTransferBinding
from harbor.rosy_http import RosyClient
from harbor.workflow import BusinessLedger
from harbor.web import Application, QuietHandler, ReadOnlyLedger, Workbench
from datetime import datetime, timezone


class ArmHttpTests(unittest.TestCase):
    def test_neutral_arm_job_and_physical_receipt_do_not_finish_the_business_request(self):
        config = json.loads(Path(os.environ["HARBOR_INTEGRATION_CONFIG"]).read_text(encoding="utf-8"))
        self.assertEqual(config["schema"], "harbor.arm-http-test/1")
        self.assertIs(config["simulation"], True)
        origin = urlsplit(config["origin"])
        self.assertEqual(origin.scheme, "http")
        self.assertTrue(ipaddress.ip_address(origin.hostname).is_loopback)
        self.assertNotIn("observer_token", config)
        client = RosyClient(config["origin"], config["operator"])
        binding = ArmTransferBinding(**config["binding"])
        with tempfile.TemporaryDirectory() as folder:
            ledger = BusinessLedger(Path(folder) / "business.sqlite3",
                placement_sources=("simulation-camera",), retreat_sources=("simulation-platform",))
            ledger.prepare("neutral-arm-http", 1, (binding,), outbound_id=binding.cargo_id,
                           outbound_stand=binding.destination, move_arms=(("A", "B"),))
            mission = ledger.propose("neutral-arm-http", client)
            deadline = time.monotonic() + config.get("goal_timeout_s", 30)  # a MuJoCo transfer runs in sim time
            while True:
                ledger.refresh("neutral-arm-http", client, now=datetime.now(timezone.utc))
                row = ledger.read("neutral-arm-http")
                self.assertNotEqual(row["state"], "HOLD", row)
                if row["goal_confirmed"]:
                    break
                if time.monotonic() >= deadline:
                    self.fail("independent observer and Fleet monitor did not close the physical transfer")
                time.sleep(.1)
            self.assertEqual(row["state"], "WAITING")
            self.assertIsNotNone(row["attempt_id"])
            with self.assertRaises(ValueError):
                ledger.confirm("neutral-arm-http", None, now=datetime.now(timezone.utc))
            self.assertEqual(ledger.read("neutral-arm-http")["state"], "WAITING")

            # A second stdlib consumer reads the same public contract without Harbor's adapter.
            request = Request(config["origin"] + "/api/fleet/arm-transfers/jobs/" + mission + "/physical-receipt",
                              headers={"Authorization": "Bearer " + config["operator"]})
            with urlopen(request, timeout=5) as response:
                receipt = json.load(response)["receipt"]
            self.assertEqual(receipt["physical_status"], "AT_TARGET_AND_CLEAR")
            self.assertEqual(receipt["attempt_id"], row["attempt_id"])
            # The calls behind business completion, through whichever Fleet is the origin (R relays them, D-965).
            evidence = client.physical_evidence(mission)
            self.assertEqual(evidence["receipt"], receipt)
            self.assertTrue(evidence["evidence_document"])
            recheck = client.request_recheck(mission, "neutral-arm-http-recheck")["recheck"]
            self.assertEqual((recheck["mission_id"], recheck["attempt_id"]), (mission, row["attempt_id"]))
            self.assertIn(recheck["status"], ("WAITING_OBSERVATIONS", "CONFIRMED"))
            self.assertEqual(client.recheck_read(mission, "neutral-arm-http-recheck")["recheck"]["recheck_key"],
                             "neutral-arm-http-recheck")
            # Browser-facing HTTP projection reads the same real Fleet contract,
            # without recovering/refreshing the Harbor ledger or receiving approver credentials.
            web = Application(Workbench(ReadOnlyLedger(ledger.path), client))
            with make_server('127.0.0.1', 0, web, handler_class=QuietHandler) as server:
                thread = Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    before = ledger.read('neutral-arm-http')
                    with urlopen(f'http://127.0.0.1:{server.server_port}/api/work?key=neutral-arm-http', timeout=10) as response:
                        display = json.load(response)
                    self.assertEqual(display['mode'], 'live')
                    self.assertEqual(display['fleet']['status'], 'GOAL_CONFIRMED')
                    self.assertEqual(display['receipt']['status'], 'VERIFIED')
                    self.assertEqual(display['receipt']['attempt_id'], row['attempt_id'])
                    self.assertEqual(display['business']['state'], 'WAITING')
                    self.assertEqual(ledger.read('neutral-arm-http'), before)
                finally:
                    server.shutdown()
                    thread.join()
            other = RosyClient(config["origin"], config["viewer"])
            with self.assertRaises(HTTPError) as denied:
                other.physical_receipt(mission)
            self.assertEqual(denied.exception.code, 403)
            with self.assertRaises(ValueError):
                ledger.propose("neutral-arm-http", client)
        self.assertFalse(any(name == prefix or name.startswith(prefix + ".") for name in sys.modules
                             for prefix in ("fleet", "core_common", "omx_adapter", "rosy")))


if __name__ == "__main__":
    unittest.main()
