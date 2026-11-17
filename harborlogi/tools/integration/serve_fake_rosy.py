"""Serve a fake ROSY Fleet on loopback for a hand-run Harbor web session (test only, no physics).

The ARM harness Fleet (simulation profile, two synthetic owners, the platform's simulation
observer) keeps running until Ctrl-C. Every proposed ARM transfer is admitted by the test
approver, the way the harness approves its own keys. The Harbor connection settings go to
--env-out (mode 600) for ~/.config/harbor/run.sh to read.

    <platform python> -m tools.integration.serve_fake_rosy --platform-root <rosy checkout> \
        --env-out ~/.config/harbor/fake-rosy.env [--port 18060]

shortcut: every move uses the one installed source->target transfer and object-1; the scene
object returns to the source before each admission (the harness does the same).
"""
import argparse
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import signal
import sqlite3
import tempfile
import threading

from tools.integration.arm_fleet_harness import ArmFleet, INSTANCE, WORKCELL


class ServedArmFleet(ArmFleet):
    """The harness Fleet whose approver admits every proposed transfer, not only preset request keys."""

    def install(self, db):
        options = super().install(db)
        # The harness observer credential lasts 10 minutes, one test; a served Fleet keeps it for a session.
        from fleet.server.physical_transfer_observer import PhysicalTransferObserver
        observer = options["arm_transfer_observer"]
        options["arm_transfer_observer"] = PhysicalTransferObserver(producer_id=observer.producer_id,
            token=self.observer_token, valid_until=datetime.now(timezone.utc) + timedelta(hours=12))
        return options

    def _proposed_keys(self):
        with sqlite3.connect(self.workdir / "fleet.sqlite3") as db:
            return [row[0] for row in db.execute(
                "SELECT request_key FROM fleet_cell_jobs WHERE status='PROPOSED' AND provenance_kind='physical'")]

    def _approve(self):
        admitted = set()
        try:
            with self.client() as proposer, self.client("approver") as approver:
                while not self.stopped.wait(.2):
                    for request_key in self._proposed_keys():
                        if request_key in admitted:
                            continue
                        response = proposer.get("/api/fleet/arm-transfers/jobs", params={"request_key": request_key})
                        if response.status_code == 404:
                            continue
                        response.raise_for_status()
                        job = response.json()["job"]
                        self.scene["position_m"] = [0, 0, .2]
                        generation = approver.get("/api/fleet/dispatch-control").json()["generation"]
                        approver.post("/api/fleet/arm-transfers/jobs/" + job["mission_id"] + "/admit", json={
                            "expected_proposal_digest": job["proposal_digest"],
                            "expected_generation": generation}).raise_for_status()
                        admitted.add(request_key)
                        print("admitted", job["mission_id"], request_key, flush=True)
                    if self.observer.poll() is not None:
                        raise RuntimeError("simulation observer exited")
        except Exception as error:
            if not self.stopped.is_set():
                self.errors.append(type(error).__name__)
                print("approver stopped:", type(error).__name__, error, flush=True)


def write_env(path, fleet):
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        out.write("# Written by serve_fake_rosy.py; valid only while that process runs.\n"
                  f"HARBOR_FLEET_ORIGIN={fleet.url}\nHARBOR_FLEET_ALLOWED_ORIGINS={fleet.url}\n"
                  f"HARBOR_FLEET_PRINCIPAL=operator\nHARBOR_FLEET_TOKEN={fleet.tokens['operator']}\n")
    os.chmod(path, 0o600)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform-root", type=Path, required=True)
    parser.add_argument("--env-out", type=Path, required=True)
    parser.add_argument("--port", type=int, default=18060)
    args = parser.parse_args(argv)
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    with tempfile.TemporaryDirectory(prefix="fake-rosy-") as temporary:
        fleet = ServedArmFleet(root=args.platform_root.resolve(), workdir=Path(temporary) / "fleet", request_keys=())
        fleet.url = f"http://127.0.0.1:{args.port}"
        fleet.start()
        write_env(args.env_out, fleet)
        print(f"fake ROSY Fleet {fleet.url}: workcell {WORKCELL}, arms {INSTANCE}/arm-2, "
              "object object-1, source->target. Ctrl-C stops.", flush=True)
        try:
            while not stop.wait(1):
                if fleet.errors:
                    print("fake ROSY worker failed:", fleet.errors, flush=True)
                    break
        finally:
            args.env_out.expanduser().unlink(missing_ok=True)
            try:
                fleet.stop()  # the harness's own cleanup; its one-attempt checks do not apply to a served Fleet
            except Exception as error:
                print("stop:", type(error).__name__, error, flush=True)
    return 1 if fleet.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
