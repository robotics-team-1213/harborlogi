"""Real Fleet HTTP/UDS contracts with explicitly synthetic devices and raw facts.

This development harness tests framing, authentication and journal transitions.
Its boxes, zero velocities and object poses are simulated fixtures; they provide
no physics, sensor calibration, native driver or field-acceptance evidence.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import os
from pathlib import Path
import secrets
import threading
import time
import xml.etree.ElementTree as ET

import httpx

from tools.integration.fleet_harness import enable, _free_port

WORKCELL, INSTANCE, ROBOT = "w-1", "arm-1", "unused"


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
    def __init__(self, *, root, workdir, request_key_path=None):
        self.root, self.workdir = Path(root), Path(workdir)
        self.request_key_path = Path(request_key_path) if request_key_path else None
        self.include_joint_evidence = False
        self.tokens = {name: secrets.token_urlsafe(40) for name in ("operator", "viewer", "approver")}
        self.observer_token = secrets.token_urlsafe(40)
        self.url = f"http://127.0.0.1:{_free_port()}"
        self.scene = {"position_m": [0, 0, .2]}
        self.stopped, self.errors = threading.Event(), []
        self.observed = set()
        self.owners, self.servers, self.threads = [], [], []
        self.transfers = {}
        self.server = None

    def start(self):
        enable(self.root)
        import sys
        if "rosy_cell" in sys.modules:
            raise RuntimeError("neutral harness must start without the palletizing package")
        sys.modules["rosy_cell"] = None
        sys.path.insert(0, str(self.root / "middleware/devices/omx/adapter"))
        import uvicorn
        from core_common.protocol.schemas import ArmTransferCapability, DeviceActionReceipt, FleetArmTransferGrant
        from fleet.server.app import create_app
        from fleet.server.console import FleetConsole
        from fleet.server.mission_service import MissionService
        from fleet.server.mission_store import MissionStore
        from fleet.server.proposal_store import ProposalStore
        from fleet.server.task_service import FleetTaskService
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
        db, sockets = self.workdir / "fleet.sqlite3", self.workdir / "sockets"
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
        users = {hashlib.sha256(token.encode()).hexdigest(): {"principal_id": name,
            "role": "operator" if name == "approver" else "service"} for name, token in self.tokens.items()}
        self.app = create_app(FleetConsole([], []), task_service=FleetTaskService(self.tasks, robot_ids=()),
            mission_service=MissionService(MissionStore(db)), proposal_store=ProposalStore(db), site_users=users,
            deployment_profile="simulation", omx_socket_root=sockets, omx_stop_transport=SyntheticStops(),
            arm_transfer_installation=installed, arm_transfer_observer=PhysicalTransferObserver(
                producer_id=policy.producer_id, token=self.observer_token,
                valid_until=datetime.now(timezone.utc) + timedelta(minutes=10)))
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
        self._thread(self._approve_and_observe)

    def _thread(self, function):
        thread = threading.Thread(target=function, daemon=True)
        self.threads.append(thread)
        thread.start()

    def client(self, who="operator"):
        return httpx.Client(base_url=self.url, headers={"Authorization": "Bearer " + self.tokens[who]}, timeout=5)

    def _sample(self, context, sequence):
        now = time.time_ns()
        owners = [{**owner, "links_observed_at_ns": now, "gripper_observed_at_ns": now,
            "gripper_state": "OPEN", "held_state": "NONE", "held_object_ref": None,
            "links": {link: {"bounds": {"min_m": [1, 1, 1], "max_m": [1.1, 1.1, 1.1]},
                "linear_velocity_m_s": [0, 0, 0], "angular_velocity_rad_s": [0, 0, 0]} for link in self.links}}
            for owner in context["owners"]]
        if self.include_joint_evidence:
            # Synthetic HTTP fixture only; these are not Gazebo or device measurements.
            for owner in owners:
                owner["joints"] = dict(sample_id=f"joint-{owner['instance_id']}-{sequence}",
                    sequence=sequence, observed_at_ns=now, joint_names=["joint1", "joint2"],
                    positions_rad=[0.1, -0.2])
        fields = ("producer_id", "evaluator_revision", "action_id", "attempt_id", "frame_id", "policy_digest",
                  "catalog_digest", "bindings_digest", "object_ref", "object_origin_id")
        return {**{k: context[k] for k in fields}, "evidence_id": "sample-" + str(sequence), "sequence": sequence,
            "object_observed_at_ns": now, "object_pose": {"position_m": list(self.scene["position_m"]),
                "orientation_xyzw": [0, 0, 0, 1]}, "owners": owners}

    def _approve_and_observe(self):
        approved, observed, sequence = False, self.observed, 0
        request_key = None if self.request_key_path else "neutral-arm-http:0"
        try:
            with self.client() as proposer, self.client("approver") as approver, httpx.Client(
                    base_url=self.url, headers={"X-Physical-Observation-Token": self.observer_token}, timeout=5) as observer:
                while not self.stopped.wait(.05):
                    if request_key is None:
                        if not self.request_key_path.exists():
                            continue
                        request_key = self.request_key_path.read_text(encoding="utf-8").strip()
                        if not request_key.startswith("harbor:") or not request_key.endswith(":0"):
                            raise ValueError("invalid web request key")
                    if not approved:
                        response = proposer.get("/api/fleet/arm-transfers/jobs", params={"request_key": request_key})
                        if response.status_code == 200:
                            job = response.json()["job"]
                            generation = approver.get("/api/fleet/dispatch-control").json()["generation"]
                            approver.post("/api/fleet/arm-transfers/jobs/" + job["mission_id"] + "/admit", json={
                                "expected_proposal_digest": job["proposal_digest"], "expected_generation": generation}).raise_for_status()
                            approved = True
                        elif response.status_code != 404:
                            response.raise_for_status()
                    response = observer.get("/api/fleet/arm-transfers/observation-context")
                    response.raise_for_status()
                    context = response.json()["context"]
                    if context is None or context["phase"] in observed:
                        continue
                    sequence += 1
                    first = self._sample(context, sequence)
                    if self.stopped.wait(.15):
                        return
                    sequence += 1
                    second = self._sample(context, sequence)
                    response = observer.post("/api/fleet/arm-transfers/jobs/" + context["mission_id"] + "/observations",
                        json={"phase": context["phase"], "expected_request_digest": context["request_digest"],
                              "observations": [first, second]})
                    response.raise_for_status()
                    observed.add(context["phase"])
        except Exception as error:
            if not self.stopped.is_set():
                self.errors.append(type(error).__name__)

    def stop(self):
        self.stopped.set()
        if self.server is not None:
            self.server.should_exit = True
        for server in self.servers:
            server.stop()
        for thread in self.threads:
            thread.join(timeout=5)
        import sys
        sys.modules.pop("rosy_cell", None)
        if self.errors or any(thread.is_alive() for thread in self.threads):
            raise RuntimeError("simulation harness worker failed or did not stop")
        submits = sum(o.operations.count("SubmitAction") for o in self.owners)
        lookups = sum(o.operations.count("GetAction") for o in self.owners)
        print("Synthetic UDS submits:", submits, "lookups:", lookups, "physical proofs:", sorted(self.observed), flush=True)
        if submits != 1 or lookups < 1 or self.observed != {"PRE_DISPATCH", "POST_ACTION"}:
            raise AssertionError("simulation did not complete exactly one admitted physical attempt")
