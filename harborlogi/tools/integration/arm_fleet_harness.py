"""Real Fleet HTTP/UDS contracts with explicitly synthetic devices and a simulated scene.

This development harness tests framing, authentication and journal transitions.
The synthetic owners move a scripted scene that is written to a simulation state
file; the platform's simulation observer (tools/sim/transfer_observer.py, D-927
decision 3) turns it into facts as a separate process. No physics, sensor
calibration, native driver or field-acceptance evidence.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import xml.etree.ElementTree as ET

import httpx

from tools.integration.fleet_harness import enable, _free_port

WORKCELL, INSTANCE, ROBOT = "w-1", "arm-1", "unused"
JOINTS = ["joint1", "joint2", "joint3", "joint4", "joint5", "gripper_joint_1"]
ASSETS = "learning/envs/isaac/assets/open_manipulator_description"
# shortcut: arm bases face away from the shared zone at the origin; the S4 installation profile will own them.
BASES = {INSTANCE: {"position_m": [.45, 0, 0], "orientation_xyzw": [0, 0, 0, 1]},
         "arm-2": {"position_m": [-.45, 0, 0], "orientation_xyzw": [0, 0, 1, 0]}}


class SyntheticOwner:
    MAX_FRAME_BYTES = 64 * 1024

    def __init__(self, capability, Receipt, Grant, scene, task_store):
        self.capability, self.Receipt, self.Grant = capability, Receipt, Grant
        self.scene, self.tasks = scene, task_store
        self.actions, self.operations = {}, []
        self.events = 10

    def dispatch(self, request, *, peer_uid):
        if peer_uid != os.getuid():
            raise PermissionError("unapproved simulation peer")
        operation = request["operation"]
        self.operations.append(operation)
        cap = self.capability
        if operation == "GetTransferCapability":
            return dict(version=2, status=200, capability=cap.model_dump(mode="json"))
        if operation == "GetOwnerIdentity":
            return dict(version=2, status=200, identity=dict(workcell_id=cap.workcell_id,
                instance_id=cap.instance_id, journal_id=cap.journal_id, simulation=True))
        if operation == "SubmitAction":
            grant = self.Grant.model_validate(request["grant"])
            fence = self.tasks.dispatch_control()
            if ((grant.workcell_id, grant.instance_id, grant.owner_journal_id, grant.owner_session_id)
                    != (cap.workcell_id, cap.instance_id, cap.journal_id, cap.owner_session_id)
                    or not fence["dispatch_enabled"] or grant.authority_epoch != fence["authority_epoch"]
                    or grant.dispatch_generation != fence["generation"]):
                raise PermissionError("synthetic owner scope or fence mismatch")
            if grant.action_id in self.actions:
                raise AssertionError("unexpected repeated SubmitAction")
            self.actions[grant.action_id] = grant
            state = "ACCEPTED"
        else:
            grant = self.actions[request["action_id"]]
            state = "CANCEL_REQUESTED" if operation == "CancelAction" else "SUCCEEDED"
            if state == "SUCCEEDED":
                self.scene["position_m"] = [.05, 0, .2]
        self.events += 1
        at = datetime.now(timezone.utc)
        receipt = self.Receipt(mission_id=grant.mission_id, step_id=grant.step_id, action_id=grant.action_id,
            attempt_id=grant.attempt_id, workcell_id=grant.workcell_id, instance_id=grant.instance_id,
            request_digest=grant.request_digest, authority_epoch=grant.authority_epoch,
            dispatch_generation=grant.dispatch_generation, journal_id=cap.journal_id,
            state=state, journal_event_id=self.events, observed_at=at, reason=None,
            phase_summaries=tuple(dict(phase_id=phase, ordinal=i, state="SUCCEEDED", journal_event_id=i + 1,
                observed_at=at) for i, phase in enumerate(("approach", "grasp", "transfer", "release")))
                if state == "SUCCEEDED" else ())
        return dict(version=2, status=200, receipt=receipt.model_dump(mode="json"))


class SyntheticStops:
    def stop(self, **kwargs):
        return {"state": "LOCAL_LATCHED"}

    def rearm(self, **kwargs):
        return {"state": "OPEN"}


class ArmFleet:
    def __init__(self, *, root, workdir, request_key_path=None, request_keys=("neutral-arm-http:0",), one_attempt=True,
                 sim=False, delegated=False, before_app=None):
        self.root, self.workdir, self.sim = Path(root), Path(workdir), sim
        # D-965 relay: proposals come from R's delegations, found by the cell operator; and extra create_app
        # arguments known only after install().
        self.delegated, self.before_app = delegated, before_app
        self.identity, self.sim_process = (WORKCELL, INSTANCE), None
        self.request_key_path = Path(request_key_path) if request_key_path else None
        self.request_keys, self.one_attempt = tuple(request_keys), one_attempt
        self.tokens = {name: secrets.token_urlsafe(40) for name in ("operator", "viewer", "approver")}
        self.observer_token = secrets.token_urlsafe(40)
        self.url = f"http://127.0.0.1:{_free_port()}"
        self.scene = {"position_m": [0, 0, .2]}
        self.stopped, self.errors = threading.Event(), []
        self.observed = set()
        self.owners, self.servers, self.threads = [], [], []
        self.transfers = {}
        self.server = self.observer = None

    def start(self):
        db = self.workdir / "fleet.sqlite3"
        options = self.install(db)
        if self.before_app is not None:
            options.update(self.before_app(self))
        import uvicorn
        from fleet.server.app import create_app
        from fleet.server.console import FleetConsole
        from fleet.server.mission_service import MissionService
        from fleet.server.mission_store import MissionStore
        from fleet.server.proposal_store import ProposalStore
        from fleet.server.task_service import FleetTaskService
        users = {hashlib.sha256(token.encode()).hexdigest(): {"principal_id": name,
            "role": "operator" if name == "approver" else "service"} for name, token in self.tokens.items()}
        self.app = create_app(FleetConsole([], []), task_service=FleetTaskService(self.tasks, robot_ids=()),
            mission_service=MissionService(MissionStore(db)), proposal_store=ProposalStore(db), site_users=users,
            deployment_profile="simulation", **options)
        self.server = uvicorn.Server(uvicorn.Config(self.app, host="127.0.0.1", port=int(self.url.rsplit(":", 1)[1]),
                                                    log_level="error", lifespan="on"))
        self._thread(self.server.run)
        deadline = time.monotonic() + 10
        while not self.server.started:
            if time.monotonic() > deadline:
                raise TimeoutError("Fleet HTTP did not start")
            time.sleep(.01)
        with self.client("approver") as client:
            generation = client.get("/api/fleet/dispatch-control").json()["generation"]
            client.post("/api/fleet/dispatch/rearm", json={"expected_generation": generation}).raise_for_status()
        if self.sim:  # the owner's fence readback reads this, with this token, before every phase
            with self.client("viewer") as client:
                started = time.monotonic()
                response = client.get("/api/fleet/dispatch-control")
                print("Fence readback:", response.status_code, round(time.monotonic() - started, 3), "s",
                      response.text[:300], flush=True)
            self.fence_seen, self.fence_slowest, self.fence_failures = set(), 0.0, []
            self._thread(self._watch_fence)
        self._workers()

    def _watch_fence(self):
        """What the owner's 250 ms readback would have seen, polled through the attempt (diagnostic only)."""
        import http.client
        port = int(self.url.rsplit(":", 1)[1])
        while not self.stopped.wait(.05):
            started = time.monotonic()
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=.25)
            try:
                connection.request("GET", "/api/fleet/dispatch-control",
                                   headers={"Authorization": "Bearer " + self.tokens["viewer"]})
                response = connection.getresponse()
                control = json.loads(response.read())
                self.fence_seen.add((response.status, control.get("authority_epoch"), control.get("generation"),
                                     control.get("dispatch_enabled")))
            except Exception as error:
                self.fence_failures.append(type(error).__name__)
            finally:
                connection.close()
                self.fence_slowest = max(self.fence_slowest, time.monotonic() - started)

    def attach(self, url, tokens):
        """Approve and observe for a Fleet started elsewhere with install()'s options (one-Fleet full flow)."""
        self.url, self.tokens = url, tokens
        self._workers()

    def _workers(self):
        script = self.root / "tools/sim/transfer_observer.py"
        if not script.is_file():
            raise FileNotFoundError("platform lacks tools/sim/transfer_observer.py (D-927 decision 3)")
        if self.sim:  # the platform state writer reads the simulator itself
            state, bases = self.sim_out / "sim-state.json", self.sim_out / "installation.bases.json"
        else:
            state, bases = self.workdir / "sim-state.json", self.workdir / "sim-bases.json"
            bases.write_text(json.dumps(BASES), encoding="utf-8")
            self._write_state(state, 0)
            self._thread(lambda: self._publish_state(state))
        self.observer_log = (self.workdir / "observer.log").open("w", encoding="utf-8")
        self.observer = subprocess.Popen([sys.executable, str(script),
            "--origin", self.url, "--assets", str(self.root / ASSETS), "--bases", str(bases), "--state", str(state),
            "--poll-s", "0.05", "run"], env={**os.environ, "ROSY_TRANSFER_OBSERVER_TOKEN": self.observer_token},
            stdout=self.observer_log, stderr=subprocess.STDOUT)
        self._thread(self._approve)

    def _write_state(self, path, sequence):
        now = time.time_ns()
        arm = {"sequence": sequence, "observed_at_ns": now, "joint_names": JOINTS, "positions_rad": [0.0] * len(JOINTS),
               "velocities_rad_s": [0.0] * len(JOINTS), "gripper_state": "OPEN", "held_state": "NONE",
               "held_object_ref": None}
        state = {"schema": "rosy.sim-transfer-state/1", "owners": {i: arm for i in BASES}, "object": {
            "position_m": list(self.scene["position_m"]), "orientation_xyzw": [0, 0, 0, 1], "observed_at_ns": now}}
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(state), encoding="utf-8")
        os.replace(temporary, path)

    def _publish_state(self, path):
        sequence = 0
        while not self.stopped.wait(.05):
            sequence += 1
            self._write_state(path, sequence)

    def _install_sim(self, db):
        """Platform MuJoCo + the real cell owner with the C3 installation (tools/sim/run_arm_transfer_sim.sh)."""
        import importlib.util
        import signal
        from fleet.server.task_store import FleetTaskStore
        from fleet.server.physical_transfer_observer import PhysicalTransferObserver
        self.sim_out = self.workdir / "sim"
        self.sim_out.mkdir(parents=True)
        self.sockets = Path(tempfile.mkdtemp(prefix="rosy-uds-", dir="/tmp"))  # AF_UNIX paths stop at 108 bytes
        log = self.workdir / "sim.log"
        self.sim_log = log.open("w", encoding="utf-8")
        self.sim_process = subprocess.Popen(["bash", str(self.root / "tools/sim/run_arm_transfer_sim.sh")],
            env={**os.environ, "OUT": str(self.sim_out), "SOCKETS": str(self.sockets), "ROSY_FLEET_FENCE_URL": self.url + "/api/fleet/dispatch-control",
                 "ROSY_FLEET_FENCE_TOKEN": self.tokens["viewer"]},
            stdout=self.sim_log, stderr=subprocess.STDOUT, start_new_session=True)
        self.sim_stop = lambda: os.killpg(self.sim_process.pid, signal.SIGTERM)
        deadline = time.monotonic() + 900
        while "ARM_TRANSFER_SIM_READY" not in log.read_text(encoding="utf-8", errors="replace"):
            if self.sim_process.poll() is not None or time.monotonic() > deadline:
                tail = log.read_text(encoding="utf-8", errors="replace").splitlines()[-30:]
                raise RuntimeError("platform simulation did not start:\n" + "\n".join(tail))
            time.sleep(1)
        spec = importlib.util.spec_from_file_location("transfer_installation", self.root / "tools/sim/transfer_installation.py")
        tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tool)
        self.send_trace = []
        from fleet.server.local_action_transport import UnixLocalActionTransport
        from fleet.server.physical_transfer_execution import PhysicalTransferExecution
        for owner, name in ((UnixLocalActionTransport, "submit"), (UnixLocalActionTransport, "get"),
                            (PhysicalTransferExecution, "validate_send"),
                            (PhysicalTransferExecution, "record_receipt")):
            setattr(owner, name, self._traced(getattr(owner, name), name))  # diagnostic: Fleet keeps only the reason
        # shortcut: Fleet issues ARM grants for 15 s (D-778) and the owner re-checks the grant before every phase, but one
        # MuJoCo transfer takes ~30 s; the sim uses the allowed maximum until the platform decides the ARM grant window.
        PhysicalTransferExecution.__init__.__kwdefaults__["grant_ttl_s"] = 60.0
        written = json.loads((self.sim_out / "installation.json").read_text(encoding="utf-8"))
        installed = tool.fleet_installation(written, self.sockets)
        self.identity = installed.policy.primary
        self.tasks = FleetTaskStore(db)
        # The real owner plans inside SubmitAction; 250 ms (the default) lost replies on a busy test PC (G2 uses 2 s).
        return dict(omx_socket_root=self.sockets, arm_transfer_installation=installed,
            omx_action_transport=UnixLocalActionTransport(self.sockets, timeout_s=2),
            arm_transfer_observer=PhysicalTransferObserver(producer_id=installed.policy.producer_id,
                token=self.observer_token, valid_until=datetime.now(timezone.utc) + timedelta(minutes=30)))

    def _traced(self, function, name):
        def traced(*args, **kwargs):
            try:
                result = function(*args, **kwargs)
            except Exception as error:
                self.send_trace.append((name, "raised", repr(error)[:800]))
                raise
            shown = result.model_dump(mode="json") if hasattr(result, "model_dump") else result
            self.send_trace.append((name, "returned", str(shown)[:800]))
            return result
        return traced

    def install(self, db):
        """Start the synthetic owners and return the create_app options that install ARM into a Fleet on db."""
        enable(self.root)
        if self.sim:
            return self._install_sim(db)
        if "rosy_cell" in sys.modules:
            raise RuntimeError("neutral harness must start without the palletizing package")
        sys.modules["rosy_cell"] = None
        sys.path.insert(0, str(self.root / "middleware/devices/omx/adapter"))
        from core_common.protocol.schemas import ArmTransferCapability, DeviceActionReceipt, FleetArmTransferGrant
        from fleet.server.task_store import FleetTaskStore
        from fleet.server.physical_transfer_evidence import PhysicalTransferPolicy, TransferParticipant
        from fleet.server.physical_transfer_observer import PhysicalTransferObserver
        from fleet.server.physical_transfer_store import TransferInstallation
        from omx_adapter.action_api import ActionApi, UnixActionServer
        from rosy.world.state.projections.omx_geometry import load_omx_geometry
        from rosy.world.state.projections.physical_transfer import Bounds3, PhysicalPose

        self.workdir.mkdir(parents=True)
        assets = self.root / "learning/envs/isaac/assets/open_manipulator_description"
        urdf = (assets / "urdf/omx_f/omx_f.urdf").read_bytes()
        meshes = {n.attrib["filename"]: (assets / n.attrib["filename"].removeprefix(
            "package://open_manipulator_description/")).read_bytes() for n in ET.fromstring(urdf).findall(".//mesh")}
        geometry = load_omx_geometry(urdf, meshes)
        self.links = sorted(geometry.required_links)
        participants = tuple(TransferParticipant(workcell_id=WORKCELL, instance_id=ident, arm_resource_id=WORKCELL + "." + ident,
            geometry=geometry, calibration_revision="cal-1", transform_revision="tf-1") for ident in (INSTANCE, "arm-2"))
        policy = PhysicalTransferPolicy(participants=participants, primary=(WORKCELL, INSTANCE),
            zones=(Bounds3((-.1, -.1, 0), (.1, .1, .3)),), zone_resource_ids=("w-1.shared",),
            object_resource_id="object-1", zone_margin_m=.01, producer_id="observer-1", evaluator_revision="eval-1",
            frame_id="world", catalog_digest="c" * 64, bindings_digest="d" * 64, object_ref="object-1",
            object_origin_id="model-origin", source_pose=PhysicalPose((0, 0, .2), (0, 0, 0, 1)),
            target_pose=PhysicalPose((.05, 0, .2), (0, 0, 0, 1)), position_tolerance_m=.002,
            orientation_tolerance_rad=.02, max_linear_speed_m_s=.01, max_angular_speed_rad_s=.02,
            max_age_ns=500_000_000, min_separation_ns=100_000_000)
        capabilities = tuple(ArmTransferCapability(action_kind="ARM_TRANSFER", control_mode="fleet_managed",
            workcell_id=p.workcell_id, instance_id=p.instance_id, journal_id="journal-" + p.instance_id,
            owner_session_id="session-1", catalog_digest=policy.catalog_digest, bindings_digest=policy.bindings_digest,
            resource_policy_digest=policy.digest, capability_revision="cap-1", config_revision="config-1") for p in participants)
        installed = TransferInstallation(policy=policy, source="source", target="target", capabilities=capabilities)
        sockets = self.workdir / "sockets"
        self.tasks = FleetTaskStore(db)
        for cap in capabilities:
            owner = SyntheticOwner(cap, DeviceActionReceipt, FleetArmTransferGrant, self.scene, self.tasks)
            path = sockets / cap.instance_id / "control.sock"
            path.parent.mkdir(parents=True)
            framed = ActionApi(None)
            framed.dispatch = owner.dispatch
            server = UnixActionServer(framed, path)
            self.owners.append(owner)
            self.servers.append(server)
            self._thread(server.serve_forever)
            deadline = time.monotonic() + 5
            while not path.exists():
                if time.monotonic() > deadline:
                    raise TimeoutError("synthetic owner socket did not start")
                time.sleep(.01)
        return dict(omx_socket_root=sockets, omx_stop_transport=SyntheticStops(), arm_transfer_installation=installed,
            arm_transfer_observer=PhysicalTransferObserver(producer_id=policy.producer_id, token=self.observer_token,
                valid_until=datetime.now(timezone.utc) + timedelta(minutes=10)))

    def _thread(self, function):
        thread = threading.Thread(target=function, daemon=True)
        self.threads.append(thread)
        thread.start()

    def client(self, who="operator"):
        return httpx.Client(base_url=self.url, headers={"Authorization": "Bearer " + self.tokens[who]}, timeout=5)

    def _proposed(self, client, request_key):
        """The job to admit: the test proposer's own key, or (D-965) the one R's delegation bound at this cell.

        R's peer credential reaches only the control-unit API, so the cell operator finds it from the delegation list.
        """
        if not self.delegated:
            response = client.get("/api/fleet/arm-transfers/jobs", params={"request_key": request_key})
            if response.status_code == 404:
                return None
            response.raise_for_status()
            return response.json()["job"]
        listed = client.get("/api/fleet/control-unit/delegations")
        listed.raise_for_status()
        for reply in listed.json()["delegations"]:
            row = reply["delegation"]
            if row["request"].get("transfer", {}).get("request_key") == request_key and row["local_task_id"]:
                response = client.get("/api/fleet/arm-transfers/jobs/" + row["local_task_id"])
                response.raise_for_status()
                return response.json()["job"]
        return None

    def _approve(self):
        """Test-operator approval (D-927 decision 4); observation phases are only read, never answered here."""
        pending = [] if self.request_key_path else list(self.request_keys)
        loaded = False
        try:
            with self.client() as proposer, self.client("approver") as approver, httpx.Client(
                    base_url=self.url, headers={"X-Physical-Observation-Token": self.observer_token}, timeout=5) as observer:
                while not self.stopped.wait(.05):
                    if self.request_key_path and not loaded:
                        if not self.request_key_path.exists():
                            continue
                        request_key = self.request_key_path.read_text(encoding="utf-8").strip()
                        if not request_key.startswith("harbor:") or not request_key.endswith(":0"):
                            raise ValueError("invalid web request key")
                        pending.append(request_key)
                        loaded = True
                    for request_key in tuple(pending):
                        job = self._proposed(approver if self.delegated else proposer, request_key)
                        if job is not None:
                            # shortcut: each move's object starts at the one installed source; per-move bindings come with S4.
                            self.scene["position_m"] = [0, 0, .2]
                            generation = approver.get("/api/fleet/dispatch-control").json()["generation"]
                            approver.post("/api/fleet/arm-transfers/jobs/" + job["mission_id"] + "/admit", json={
                                "expected_proposal_digest": job["proposal_digest"], "expected_generation": generation}).raise_for_status()
                            pending.remove(request_key)
                    response = observer.get("/api/fleet/arm-transfers/observation-context")
                    response.raise_for_status()
                    context = response.json()["context"]
                    if context is not None:
                        self.observed.add(context["phase"])
                    if self.observer.poll() is not None:
                        raise RuntimeError("simulation observer exited")
        except Exception as error:
            if not self.stopped.is_set():
                self.errors.append(type(error).__name__)

    def stop(self):
        self.stopped.set()
        if self.sim and self.server is not None and self.observed - {"RECHECK"} != {"PRE_DISPATCH", "POST_ACTION"}:
            with self.client() as client:  # Fleet's own account of the attempt, before it shuts down
                for key in self.request_keys:
                    print("== Fleet job", key, client.get("/api/fleet/arm-transfers/jobs",
                          params={"request_key": key}).text[:3000], flush=True)
            import sqlite3
            with sqlite3.connect(self.workdir / "fleet.sqlite3") as db:
                for row in db.execute("SELECT * FROM fleet_cell_events ORDER BY rowid DESC LIMIT 6"):
                    print("== Fleet event", str(row)[:1500], flush=True)
        if self.server is not None:
            self.server.should_exit = True
        for server in self.servers:
            server.stop()
        if self.observer is not None:
            self.observer.terminate()
            self.observer.wait(timeout=5)
            self.observer_log.close()
        for thread in self.threads:
            thread.join(timeout=5)
        if self.sim_process is not None:
            self.sim_stop()
            self.sim_process.wait(timeout=30)
            self.sim_log.close()
            shutil.rmtree(self.sockets, ignore_errors=True)
        sys.modules.pop("rosy_cell", None)
        if self.errors or any(thread.is_alive() for thread in self.threads):
            raise RuntimeError("simulation harness worker failed or did not stop")
        if self.sim:
            print("Simulation owner (MuJoCo) physical proofs:", sorted(self.observed), flush=True)
            print("Fence watch: seen", sorted(self.fence_seen, key=str), "slowest", round(self.fence_slowest, 3),
                  "s, failures", self.fence_failures[:10], flush=True)
            for entry in self.send_trace[:2] + self.send_trace[-6:]:
                print("== Fleet send", *entry, flush=True)
            if self.one_attempt and self.observed - {"RECHECK"} != {"PRE_DISPATCH", "POST_ACTION"}:
                import sqlite3
                with sqlite3.connect(self.sim_out / "journal" / "owner.sqlite3") as db:
                    for row in db.execute("SELECT phase_id, state, result_json FROM omx_action_phases ORDER BY ordinal"):
                        print("== owner phase", row[0], row[1], (row[2] or "")[:700], flush=True)
                    for row in db.execute("SELECT event_type, state, detail_json FROM omx_action_events "
                                          "ORDER BY event_id DESC LIMIT 6"):
                        print("== owner event", row[0], row[1], (row[2] or "")[:400], flush=True)
                for name in ("owner.log", "writer.log"):  # the temporary run folder goes with the harness
                    lines = (self.sim_out / name).read_text(encoding="utf-8", errors="replace").splitlines()
                    print(f"== {name}", *lines[-40:], sep="\n", flush=True)
                raise AssertionError("simulation did not prove both phases of the admitted attempt")
            return
        submits = sum(o.operations.count("SubmitAction") for o in self.owners)
        lookups = sum(o.operations.count("GetAction") for o in self.owners)
        print("Synthetic UDS submits:", submits, "lookups:", lookups, "physical proofs:", sorted(self.observed), flush=True)
        if self.one_attempt and (submits != 1 or lookups < 1 or self.observed - {"RECHECK"} != {"PRE_DISPATCH", "POST_ACTION"}):
            import sqlite3  # diagnostic: the job's own account, read after the server stopped
            with sqlite3.connect(self.workdir / "fleet.sqlite3") as db:
                for row in db.execute("SELECT mission_id, status, reason FROM fleet_cell_jobs"):
                    print("== cell job", *row, flush=True)
                for row in db.execute("SELECT * FROM fleet_cell_events ORDER BY rowid DESC LIMIT 8"):
                    print("== cell job event", *[str(value)[:300] for value in row], flush=True)
            raise AssertionError("simulation did not complete exactly one admitted physical attempt")


class FullFlowFleet:
    """D-927 decision 2: one simulation Fleet serves TRIP (synthetic Pinky) and ARM (synthetic owners)."""

    def __init__(self, *, root, workdir, request_keys):
        self.root, self.workdir = Path(root), Path(workdir) / "fleet"
        self.arm = ArmFleet(root=root, workdir=Path(workdir) / "arm", request_keys=request_keys, one_attempt=False)
        self.fleet = None

    def start(self):
        from tools.integration.fleet_harness import RealFleet
        self.workdir.mkdir(parents=True)
        self.fleet = RealFleet(root=self.root, workdir=self.workdir, transport_only=True,
                               extra_app=self.arm.install(self.workdir / "fleet.sqlite3"))
        self.fleet.start()
        self.url, self.tokens = self.fleet.url, self.fleet.tokens
        self.arm.attach(self.url, self.tokens)

    def stop(self):
        try:
            self.arm.stop()
        finally:
            if self.fleet is not None:
                self.fleet.stop()


class RelayedArmFleet:
    """D-965: Harbor talks only to R; R relays ARM work to the cell Fleet, whose operator alone admits it."""

    r_resources = ()  # the manifest's R resources; both units must load identical manifests

    def __init__(self, *, root, workdir, sim=False, request_keys=("neutral-arm-http:0",), one_attempt=True):
        self.root, self.workdir = Path(root), Path(workdir)
        self.peer = {unit: secrets.token_urlsafe(40) for unit in ("R", "CELL-A")}  # the token each unit sends
        self.cell = ArmFleet(root=root, workdir=self.workdir / "cell", sim=sim, delegated=True,
                             request_keys=request_keys, one_attempt=one_attempt,
                             before_app=lambda cell: {"control_unit_config": self._config(
                                 "CELL-A", "R", self.url, cell.identity[0])})
        self.cell.tokens["control-unit:R"] = self.peer["R"]
        self.tokens = {name: secrets.token_urlsafe(40) for name in ("operator", "viewer")}
        self.url = f"http://127.0.0.1:{_free_port()}"
        self.server = None

    @property
    def identity(self):
        return self.cell.identity

    def _config(self, unit, peer, url, workcell):
        folder = self.workdir / unit
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "peer.token").write_text(self.peer[unit], encoding="utf-8")
        path = folder / "control-unit.json"  # the loader reads YAML, and JSON is YAML
        path.write_text(json.dumps({"unit_id": unit, "peers": {peer: {"url": url, "token_file": "peer.token"}},
            "manifest": {"revision": "harbor-int-1", "units": [
                {"id": "R", "resources": list(self.r_resources)},
                {"id": "CELL-A", "parent": "R", "resources": ["workcell:" + workcell]}]}}),
            encoding="utf-8")
        return path

    def start(self):
        (self.workdir / "R").mkdir(parents=True, exist_ok=True)  # ArmFleet.install creates its own folder
        self.cell.start()
        import uvicorn
        from fleet.server.app import create_app
        from fleet.server.console import FleetConsole
        from fleet.server.task_service import FleetTaskService
        from fleet.server.task_store import FleetTaskStore
        users = {hashlib.sha256(token.encode()).hexdigest(): {"principal_id": principal, "role": role}
                 for token, principal, role in ((self.tokens["operator"], "harbor-operator", "service"),
                                                (self.tokens["viewer"], "viewer", "viewer"),
                                                (self.peer["CELL-A"], "control-unit:CELL-A", "service"))}
        app = create_app(FleetConsole([], []), site_users=users,
            task_service=FleetTaskService(FleetTaskStore(self.workdir / "R" / "fleet.sqlite3"), robot_ids=()),
            control_unit_config=self._config("R", "CELL-A", self.cell.url, self.identity[0]))
        if app.state.arm_transfer_job_store is not None or app.state.control_unit is None:
            raise RuntimeError("R must relay ARM work, not own it")
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=int(self.url.rsplit(":", 1)[1]),
                                                    log_level="error", lifespan="on"))
        self.cell._thread(self.server.run)
        deadline = time.monotonic() + 10
        while not self.server.started:
            if time.monotonic() > deadline:
                raise TimeoutError("R Fleet HTTP did not start")
            time.sleep(.01)

    def stop(self):
        try:
            if self.server is not None:
                self.server.should_exit = True
        finally:
            self.cell.stop()


class RelayedFullFlowFleet(RelayedArmFleet):
    """D-965 full first flow: Harbor has only R's origin; R owns the Pinky TRIP path and relays arm work to the cell."""

    def __init__(self, *, root, workdir, request_keys):
        from tools.integration.fleet_harness import ROBOT
        self.r_resources = ("robot:" + ROBOT,)  # Fleet refuses an R whose robots differ from its console roster
        super().__init__(root=root, workdir=workdir, request_keys=request_keys, one_attempt=False)
        self.fleet = None

    def start(self):
        from tools.integration.fleet_harness import RealFleet
        (self.workdir / "R").mkdir(parents=True, exist_ok=True)
        self.cell.start()
        self.fleet = RealFleet(root=self.root, workdir=self.workdir / "R", transport_only=True,
                               port=int(self.url.rsplit(":", 1)[1]),
                               extra_app={"control_unit_config": self._config("R", "CELL-A", self.cell.url, self.identity[0])})
        self.fleet.tokens["cell"] = self.peer["CELL-A"]
        self.fleet.principals["cell"] = ("control-unit:CELL-A", "service")
        self.tokens = self.fleet.tokens
        self.fleet.start()
        if self.fleet.app.state.arm_transfer_job_store is not None or self.fleet.app.state.control_unit is None:
            raise RuntimeError("R must relay ARM work, not own it")

    def stop(self):
        try:
            if self.fleet is not None:
                self.fleet.stop()
        finally:
            self.cell.stop()
