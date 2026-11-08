"""진짜 Rosy Fleet 코드를 이 개발 프로세스 안에서 simulation 프로필로 띄우는 Harbor 계약 시험 하네스.

출처: rosy-vault `9a744a3ed2e74e1775cf4622d15c138942336db8`의 `tools/integration/real_fleet.py`를 옮겨 Harbor 이름으로 맞췄다
(ADR-0003). Fleet 코드는 고치지 않고, ROS · Gazebo 없이 다음 가짜만 주입한다:
- 로봇: ROSY 시험용 FakeRobot(operations/fleet/test/fakes.py) + 받은 goal 쪽으로 움직이는 자세(`Drive`). 맵은 free 구간 고리.
  운행 작업에 필요한 CORE 능력(운행 임대 등)을 알리고, Fleet은 운행 임대 · 교통 권한을 켠다.
- 팔: OMX 소유자 대신 가짜 전송(`Omx`): 받으면 ACCEPTED, work_s 뒤 SUCCEEDED.
- 목표 근거: 시뮬 생산자(`Producer`)가 ACTION_SUCCEEDED 단계마다 저장된 목표 자세 그대로 sim_model_pose 근거를 HTTP로 보낸다.
- 승인: 앱이 아니라 두 번째 운영자(`approver`)가 승인한다. Harbor 런타임에는 승인 기능이 없다.
Windows: Fleet은 import 때 fcntl · AF_UNIX를 쓰지 않고, 유닉스 소켓을 쓰는 OMX 전송은 주입한 가짜로 대신한다.

이 파일은 플랫폼 시험 의존성(httpx · uvicorn · yaml)이 있는 개발 환경에서만 `real_fleet_http`가 불러 쓴다.
Harbor 런타임과 기본 시험은 이 파일을 import하지 않는다.
"""
from __future__ import annotations

import hashlib
import importlib.util
import itertools
import json
import math
import os
import secrets
import socket
import sqlite3
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import httpx

PATHS = ("operations/fleet", "operations/decision/src", "operations/world/src", "operations/site_execution/src",
         "operations/execution/src", "contracts/foundation", "contracts/skill/src", "contracts/motion/src",
         "contracts/learning/src", "middleware/core/services", "middleware/skills/api/src",
         "operations/processes/palletizing/src")
ROBOT, WORKCELL, INSTANCE = "pinky-sim-1", "omx-sim-cell", "omx-sim-1"
SERVICE = "cell-app-service"
# 위치 이름 = Fleet 활성 맵 장소 id. free 구간 한 바퀴(한 방향):
# dock → inbound → arm_station → outbound → dock. Fleet 경로 계획은 제자리 U턴을 하지 않아서 고리로 둔다.
PLACES = {"dock": (0.0, 0.0), "inbound": (0.5, 0.0), "arm_station": (0.5, 0.5), "outbound": (0.0, 0.5)}
RING = ("dock", "inbound", "arm_station", "outbound", "dock")
# 상자 하나 전달 recipe: 팔레트 하나에 상자 하나 (rosy_cell.recipe/1, palletize, 집는 곳 infeed). 칸 · 쟁반 자세는 만들지 않음
RECIPE = {"schema": "rosy_cell.recipe/1", "mode": "palletize",
          "box": {"length": 0.04, "width": 0.03, "height": 0.03, "mass_kg": 0.02, "grasp_depth": 0.015},
          "pallets": [{"id": "A", "frame": "pallet_a", "length": 0.05, "width": 0.04, "max_stack_height": 0.04,
                       "max_load_kg": 0.5}],
          "pick_station": "infeed", "gap": 0.015, "layers": [{"pattern": "grid"}]}
TRANSFERS = {"pinky_tray>cell_1": "harbor-in-cell-1", "cell_1>pinky_tray": "harbor-out-cell-1"}   # 이 하네스가 저장하는 recipe 문서 id
CELL_ID = "omx-sim"
PRODUCER_REVISION = "harbor-int-v1"


def rosy_root() -> Optional[Path]:
    v = os.environ.get("ROSY_PLATFORM_DIR")
    return Path(v).resolve() if v else None


def enable(root: Path) -> None:
    """ROSY 코드를 import할 수 있게 (이 하네스 프로세스만)."""
    for rel in reversed(PATHS):
        p = str(root / rel)
        if p not in sys.path:
            sys.path.insert(0, p)


def _fakes(root: Path):
    """ROSY 시험용 FakeRobot. 다른 `fakes` 이름과 섞이지 않게 파일 경로로 다른 이름에 싣는다."""
    spec = importlib.util.spec_from_file_location("rosy_fleet_test_fakes", root / "operations/fleet/test/fakes.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def digest(doc) -> str:
    return hashlib.sha256(json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def documents(root: Path) -> dict:
    """(kind, id) → 문서. 들이기 · 내기 recipe는 이름만 다르다(생산자 범위가 recipe digest마다 하나라서)."""
    import yaml
    cell = yaml.safe_load((root / "operations/processes/cell/examples/omx_sim/cell.yaml").read_text(encoding="utf-8"))
    cell["frames"] = {k: v for k, v in cell["frames"].items() if k in ("base", "pallet_a")}
    cell["stations"] = {"infeed": cell["stations"]["infeed"]}
    docs = {("cell", CELL_ID): cell}
    for key, rid in TRANSFERS.items():
        docs[("recipe", rid)] = {**RECIPE, "name": f"harbor-{rid}"}
    return docs


# ---------- 가짜 장치 (Fleet 바깥) ----------
class Drive:
    """로봇 자세: 마지막으로 받은 goal 쪽으로 speed m/s로 간다. Fleet의 map_pose_port · lane_junction(임대 포함)."""

    def __init__(self, MapPose, start=(0.0, 0.0), speed=0.5):
        self.MapPose = MapPose
        self.x, self.y, self.yaw, self.speed = start[0], start[1], 0.0, speed
        self.target, self.t = None, time.monotonic()
        self.lock = threading.Lock()
        self.leases: list[str] = []

    def goal(self, x, y, yaw):
        with self.lock:
            self._advance()
            self.target = (x, y, yaw)

    def _advance(self):
        now = time.monotonic()
        dt, self.t = now - self.t, now
        if self.target is None:
            return
        dx, dy = self.target[0] - self.x, self.target[1] - self.y
        d = math.hypot(dx, dy)
        step = min(d, self.speed * dt)
        if d > 1e-9:
            self.x, self.y = self.x + dx / d * step, self.y + dy / d * step
            self.yaw = math.atan2(dy, dx)

    def pose(self):
        with self.lock:
            self._advance()
            return self.x, self.y, self.yaw

    async def arbitrated_pose(self, robot_id):
        x, y, yaw = self.pose()
        return self.MapPose(x, y, yaw, "LOCALIZED", "sighting", 0.0, 0.05, 0.05, odom_stamp=time.time())

    async def refresh(self, robot_id, force_rest=False, **_):
        return None

    async def junction_state(self, robot_id, **_):   # free 구간만 쓰므로 부르지 않음
        return {}

    # CORE 운행 임대 (PUT · DELETE /api/v1/trip-lease): Fleet이 작업 하나 동안 잡고 갱신한다 (D-763 6)
    async def trip_lease(self, robot_id, body):
        self.leases.append(body["lease_id"])
        return {"renewed": True}

    async def trip_lease_release(self, robot_id, lease_id):
        return {}

    async def send_authority(self, robot_id, body):   # 차선 구간만. free에서는 보내지 않지만 켜 둔 교통 권한의 몫
        return {"accepted": True, "authority": {"state": "FREE"}}

    async def send_advice(self, robot_id, body):
        return {"accepted": True}


class Omx:
    """OMX 소유자 대신: 받으면 ACCEPTED, work_s 뒤 SUCCEEDED. 정지 · 재무장은 바로 LOCAL_LATCHED · OPEN."""

    PHASES = ("approach", "grasp", "transfer", "release")

    def __init__(self, Receipt, work_s=1.0):
        self.Receipt, self.work_s = Receipt, work_s
        self.started: dict[str, float] = {}
        self.events = itertools.count(2)
        self.stops = 0

    def _receipt(self, grant, state, phases):
        now = datetime.now(timezone.utc).isoformat()
        return self.Receipt.model_validate({
            "mission_id": grant.mission_id, "step_id": grant.step_id, "action_id": grant.action_id,
            "attempt_id": grant.attempt_id, "workcell_id": grant.workcell_id, "instance_id": grant.instance_id,
            "request_digest": grant.request_digest, "authority_epoch": grant.authority_epoch,
            "dispatch_generation": grant.dispatch_generation, "state": state, "journal_event_id": next(self.events),
            "observed_at": now, "reason": None,
            "phase_summaries": [{"phase_id": p, "ordinal": i, "state": "SUCCEEDED" if state == "SUCCEEDED" else "RUNNING",
                                 "journal_event_id": 1, "observed_at": now} for i, p in enumerate(phases)]})

    def submit(self, grant):
        self.started.setdefault(grant.action_id, time.monotonic())
        return self._receipt(grant, "ACCEPTED", self.PHASES[:1])

    def get(self, grant):
        if time.monotonic() - self.started.get(grant.action_id, time.monotonic()) >= self.work_s:
            return self._receipt(grant, "SUCCEEDED", self.PHASES)
        return self._receipt(grant, "RUNNING", self.PHASES[:1])

    def cancel(self, grant, *, reason):
        raise RuntimeError("Fleet dispatcher never cancels")

    def owner_identity(self, instance_id):
        return {"workcell_id": WORKCELL, "instance_id": instance_id, "simulation": True, "profile": "omx-cell-sim",
                "journal_id": "harbor-int"}

    def stop(self, **_):
        self.stops += 1
        return {"state": "LOCAL_LATCHED"}

    def rearm(self, **_):
        return {"state": "OPEN"}


class Producer(threading.Thread):
    """시뮬 목표 근거 생산자: ACTION_SUCCEEDED 단계마다 저장된 목표 자세 그대로(완벽히 놓았다고) 근거를 보낸다."""

    def __init__(self, url, db, store, tokens, Submission):
        super().__init__(daemon=True)
        self.url, self.db, self.store, self.tokens, self.Submission = url, db, store, tokens, Submission
        self.stopped, self.sent, self.errors = threading.Event(), [], []

    def run(self):
        with httpx.Client(base_url=self.url, timeout=5.0) as c:
            while not self.stopped.wait(0.2):
                try:
                    self._once(c)
                except Exception as e:   # 다음 바퀴에 다시
                    self.errors.append(repr(e))

    def _once(self, c):
        with sqlite3.connect(self.db) as con:
            ids = [r[0] for r in con.execute("SELECT DISTINCT mission_id FROM fleet_cell_steps "
                                             "WHERE status='ACTION_SUCCEEDED' AND action_kind='CELL_TRANSFER'")]
        for mid in ids:
            job = self.store.get(mid)
            step = job["steps"][job["current_step_index"]]
            if step["status"] != "ACTION_SUCCEEDED" or (mid, step["action_id"]) in self.sent:
                continue
            token = self.tokens.get(job["recipe_digest"])
            if token is None:
                self.errors.append(f"no producer for recipe {job['recipe_digest']}")
                continue
            g = step["grant"]
            target = step["step"]["goal_predicate"]["target"]
            pose = {"x_m": target["x"], "y_m": target["y"], "z_m": target["z"], "roll_rad": 0.0, "pitch_rad": 0.0,
                    "yaw_rad": target["yaw"]}
            now = time.time()
            issued = datetime.fromisoformat(g["issued_at"].replace("Z", "+00:00")).timestamp()
            evidence = {
                "producer_id": f"harbor-int-{job['recipe_digest'][:8]}", "evidence_id": "goal-" + step["action_id"],
                "evidence_source": "sim_model_pose", "evaluator_revision": PRODUCER_REVISION,
                "job_id": job["job_id"], "step_id": step["step_id"], "step_index": step["step_index"],
                "action_id": step["action_id"], "attempt_id": step["attempt_id"],
                "request_digest": step["request_digest"], "recipe_sha256": job["recipe_digest"],
                "cell_sha256": job["cell_digest"], "model_id": "cell_" + step["action_id"],
                "observation_id": "final-" + step["action_id"], "observation_digest": "d" * 64,
                "evidence_revision": "harbor-int", "observed_at": now, "model_pose_base": pose,
                "initial_observation_id": "initial-" + step["action_id"], "initial_observation_digest": "e" * 64,
                "initial_observed_at": issued, "initial_model_pose_base": pose, "gripper_state": "OPEN",
                "gripper_evidence_id": "gripper-" + step["action_id"], "gripper_evidence_revision": "harbor-int",
                "gripper_observed_at": now}
            body = self.Submission(mission_id=mid, evidence=evidence).model_dump(mode="json")
            r = c.post("/api/fleet/cell-goal-evidence", headers={"X-Goal-Evidence-Token": token}, json=body)
            if r.status_code == 200:
                self.sent.append((mid, step["action_id"]))
            else:
                self.errors.append(f"{r.status_code} {r.text[:300]}")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class RealFleet:
    """진짜 Fleet 앱 (simulation 프로필, 임시 SQLite)을 127.0.0.1에 띄운다. with 문으로 쓴다."""

    def __init__(self, root: Optional[Path] = None, port: Optional[int] = None, work_s: float = 1.0,
                 speed: float = 0.5, workdir: Optional[Path] = None, open_dispatch: bool = True,
                 transport_only: bool = False, extra_app: Optional[dict] = None):
        self.root = Path(root or rosy_root() or "")
        if not (self.root / "operations/fleet/fleet/server/app.py").is_file():
            raise RuntimeError(f"rosy-platform 소스 root가 아님: {self.root}")
        self.port, self.work_s, self.speed = port or _free_port(), work_s, speed
        self.transport_only = transport_only
        self.extra_app = extra_app or {}   # 같은 Fleet 프로세스에 더 설치할 create_app 인자(전체 흐름의 ARM)
        self.open_dispatch = open_dispatch   # False: 새 Fleet처럼 출발이 닫힌 채 둔다 (사람 · 앱이 열게)
        self.url = f"http://127.0.0.1:{self.port}"
        self.workdir = Path(workdir or tempfile.mkdtemp(prefix="harbor-real-fleet-"))
        self.db = self.workdir / "fleet.sqlite3"
        # 토큰: 이름 있는 운영자(앱) · 두 번째 사람(콘솔 승인) · Fleet service · 보기만. rosy-dev-*가 아님(X8)
        self.tokens = {name: f"int-{name}-{secrets.token_hex(12)}" for name in ("operator", "approver", "service", "viewer")}
        self.principals = {"operator": ("harbor-operator", "operator"), "approver": ("console-human", "operator"),
                           "service": (SERVICE, "service"), "viewer": ("viewer-1", "viewer")}
        self.transfers: dict[str, dict] = {}

    # ---------- 시작 · 끝 ----------
    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()

    def start(self) -> None:
        enable(self.root)
        import uvicorn
        import yaml
        from core_common.protocol.schemas import CellGoalEvidenceSubmission, DeviceActionReceipt
        from fleet.server.app import create_app
        from fleet.server.console import FleetConsole
        from fleet.server.mission_service import MissionService
        from fleet.server.mission_store import MissionStore
        from fleet.server.proposal_store import ProposalStore
        from fleet.server.site_map_store import SiteMapStore
        from fleet.server.task_service import FleetTaskService
        from fleet.server.task_store import FleetTaskStore
        from fleet.site_map import SiteMap
        from fleet.swarm.robots import RobotEndpoint
        from rosy.world.state.projections.map_pose import MapPose

        docs, cell_options = {}, {}
        self.producer_tokens = {}
        if not self.transport_only:
            from fleet.server.cell_compiler import PalletizingCellJobCompiler
            from fleet.server.cell_goal_evidence_registry import CellGoalProducer, CellGoalRegistry
            from rosy.execution.site.item_pose import ItemPoseTolerance
            docs = documents(self.root)
            compiler = PalletizingCellJobCompiler(tol_m=0.001)
            cell_doc = docs[("cell", CELL_ID)]
            producers, self.producer_tokens = [], {}
            for key, rid in TRANSFERS.items():
                bundle = compiler.compile(docs[("recipe", rid)], cell_doc).plan_bundle
                token = f"int-producer-{secrets.token_hex(12)}"
                self.producer_tokens[bundle.recipe_digest] = token
                producers.append(CellGoalProducer(
                    producer_id=f"harbor-int-{bundle.recipe_digest[:8]}", token=token, workcell_id=WORKCELL,
                    instance_id=INSTANCE, recipe_sha256=bundle.recipe_digest, cell_sha256=bundle.cell_digest,
                    evaluator_revisions=(PRODUCER_REVISION,), max_age_s=30,
                    valid_until=datetime.now(timezone.utc) + timedelta(days=1)))

        fakes = _fakes(self.root)
        self.drive = Drive(MapPose, PLACES["dock"], self.speed)
        drive = self.drive

        class SimRobot(fakes.FakeRobot):
            async def capabilities(self):
                # 운행 작업이 요구하는 CORE 능력 (trip_admission.py JOB_CAPS, D-760 7): 임대 · 목표 임대 · 임대 중 권한
                base = {"kind": "base_velocity", "robot_kind": "pinky_pro", "trip_max_linear": 0.2, "drive_modes": ["free"],
                        "trip_lease": True, "trip_lease_job": True,
                        "goal_lease": True, "line_follow_authority": True, "line_follow_authority_under_lease": True}
                return {**await super().capabilities(), "controls": {"items": [base]}}

            async def state(self):
                x, y, yaw = drive.pose()
                self._state["pose"] = {"x": x, "y": y, "yaw": yaw}
                return await super().state()

            async def navigation_goal(self, x, y, yaw, *, correlation_id=None):
                drive.goal(x, y, yaw)
                return await super().navigation_goal(x, y, yaw, correlation_id=correlation_id)

        self.robot = SimRobot(ROBOT)
        console = FleetConsole([RobotEndpoint(ROBOT, "http://127.0.0.1:9", "robot-token")], [self.robot])
        tasks = FleetTaskService(FleetTaskStore(self.db), robot_ids=(ROBOT,))
        maps = SiteMapStore(self.db)
        maps.import_if_empty(SiteMap.model_validate({
            "places": [{"id": p, "name": p, "x": x, "y": y, "kind": "junction"} for p, (x, y) in PLACES.items()],
            "edges": [{"id": f"{a}-{b}", "from": a, "to": b, "polyline": [list(PLACES[a]), list(PLACES[b])],
                       "width_m": 0.2, "speed_cap_mps": 0.2, "drive_mode": "free"}
                      for a, b in zip(RING, RING[1:])]}),
            source="harbor-int")
        users = {hashlib.sha256(tok.encode()).hexdigest(): {"principal_id": self.principals[n][0], "role": self.principals[n][1]}
                 for n, tok in self.tokens.items()}
        missions = MissionService(MissionStore(self.db))
        if not self.transport_only:
            self.omx = Omx(DeviceActionReceipt, self.work_s)
            tolerance = ItemPoseTolerance.from_mapping(yaml.safe_load(
                (self.root / "deploy/robot/omx/sim/item_pose_goal.yaml").read_text(encoding="utf-8")))
            cell_options = dict(cell_job_compiler=compiler, cell_app_service_id=SERVICE,
                enable_mission_dispatcher=True, omx_instances={WORKCELL: INSTANCE}, omx_action_transport=self.omx,
                omx_stop_transport=self.omx,
                omx_cell_grant_revisions={INSTANCE: {"capability_revision": "cell-transfer-v1",
                                                     "config_revision": "cell-config-v1"}},
                cell_item_pose_tolerance=tolerance, cell_goal_registry=CellGoalRegistry(tuple(producers)))
        self.app = create_app(
            console, task_service=tasks, site_users=users, mission_service=missions, proposal_store=ProposalStore(self.db),
            **cell_options, deployment_profile="simulation",
            site_maps=maps, map_pose_port=drive, lane_junction=drive,
            # 운행 작업(D-763)은 사이트 운행 임대 · 교통 권한이 켜져 있어야 받는다 (fleet_trips.py의 시뮬 설정과 같음)
            trip_lease={"required": True, "ttl_s": 8.0}, traffic_authority=True, **self.extra_app)
        self.server = uvicorn.Server(uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="warning"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 30
        while not self.server.started:
            if not self.thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError("Fleet 앱이 뜨지 않음")
            time.sleep(0.05)
        self._seed(docs)
        if not self.transport_only:
            self.producer = Producer(self.url, str(self.db), self.app.state.cell_job_store, self.producer_tokens,
                                     CellGoalEvidenceSubmission)
            self.producer.start()

    def stop(self) -> None:
        if getattr(self, "producer", None):
            self.producer.stopped.set()
        if getattr(self, "server", None):
            self.server.should_exit = True
            self.thread.join(timeout=15)

    # ---------- HTTP (사람 · 설정) ----------
    def client(self, who: str = "operator") -> httpx.Client:
        return httpx.Client(base_url=self.url, headers={"Authorization": f"Bearer {self.tokens[who]}"}, timeout=10.0)

    def _seed(self, docs) -> None:
        """사람이 Fleet 콘솔에서 하는 준비: 문서 저장, 컴파일 미리보기, 출발 허가(rearm, open_dispatch일 때)."""
        if self.transport_only:
            if self.open_dispatch:
                self.rearm()
            return
        with self.client("operator") as c:
            saved = {}
            for (kind, ident), doc in docs.items():
                r = c.post(f"/api/fleet/cell-app/documents/{kind}/{ident}", json={"document": doc, "expected_digest": None})
                r.raise_for_status()
                saved[(kind, ident)] = r.json()["digest"]
            for key, rid in TRANSFERS.items():
                ref = {"recipe_id": rid, "recipe_digest": saved[("recipe", rid)], "cell_id": CELL_ID,
                       "cell_digest": saved[("cell", CELL_ID)]}
                r = c.post("/api/fleet/cell-app/compile", json=ref)
                r.raise_for_status()
                assert r.json()["summary"]["transfer_count"] == 1, r.json()["summary"]   # 상자 하나 = 전달 하나
                self.transfers[key] = ref
        if self.open_dispatch:
            self.rearm()

    def rearm(self) -> dict:
        with self.client("approver") as c:
            gen = c.get("/api/fleet/dispatch-control").json()["generation"]
            r = c.post("/api/fleet/dispatch/rearm", json={"expected_generation": gen})
            r.raise_for_status()
            return r.json()

    def pending(self) -> set[str]:
        with self.client("approver") as c:
            return {r["source_id"] for r in c.get("/api/fleet/work", params={"limit": 100}).json()["rows"]
                    if r.get("source") == "cell_job" and r.get("status") == "PROPOSED"}

    def admit_pending(self, skip=frozenset()) -> list[str]:
        """콘솔의 두 번째 사람: 승인 기다리는 Cell 작업을(skip 빼고) 승인한다. 승인한 작업 id."""
        done = []
        with self.client("approver") as c:
            for row in c.get("/api/fleet/work", params={"limit": 100}).json()["rows"]:
                if row.get("source") == "cell_job" and row.get("status") == "PROPOSED" and row["source_id"] not in skip:
                    gen = c.get("/api/fleet/dispatch-control").json()["generation"]
                    r = c.post(f"/api/fleet/missions/{row['source_id']}/admit", json={"expected_generation": gen})
                    r.raise_for_status()
                    done.append(row["source_id"])
        return done


def main(argv=None) -> int:
    """손으로 띄워 볼 때만 쓴다. 시험은 `python -m tools.integration.real_fleet_http`."""
    import argparse
    a = argparse.ArgumentParser(prog="tools.integration.fleet_harness")
    a.add_argument("--platform-root", type=Path, default=rosy_root())
    a.add_argument("--port", type=int, default=8301)
    args = a.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    with RealFleet(root=args.platform_root, port=args.port) as f:
        print(f"진짜 Fleet (simulation): {f.url}  임시 장부 {f.workdir}")
        print("Ctrl+C로 끝. 토큰은 출력하지 않는다.")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
