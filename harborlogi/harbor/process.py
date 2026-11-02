from dataclasses import dataclass
import re

from harbor.model import YARD_ARMS, identifier
from harbor.outbound import OutboundProposal


@dataclass(frozen=True)
class ProcessBinding:
    cargo_id: str
    source: str
    destination: str
    recipe_id: str
    recipe_digest: str
    cell_id: str
    cell_digest: str
    workcell_id: str
    instance_id: str
    process_artifact_digest: str

    def __post_init__(self):
        for value in (self.cargo_id, self.source, self.destination, self.workcell_id, self.instance_id):
            identifier(value)
            if len(value) > 96 or any(ord(c) < 32 for c in value):
                raise ValueError("Invalid process binding identifier")
        for value in (self.recipe_id, self.cell_id):
            if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", value):
                raise ValueError("Invalid installed document ID")
        for value in (self.recipe_digest, self.cell_digest, self.process_artifact_digest):
            if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
                raise ValueError("Invalid installed document digest")

    def compile_request(self):
        from harbor.fleet_contract import process_compile_request
        return process_compile_request(self)

    def proposal_request(self, request_key):
        from harbor.fleet_contract import process_proposal_request
        return process_proposal_request(self, request_key)


@dataclass(frozen=True)
class ArmTransferBinding:
    """Harbor move names map to installed physical references at this boundary.

    Entries may be installed per (move, arm): several entries share the business key
    with different instance references, and installation order names the primary arm
    followed by alternates (design 29). Entries without a yard arm keep the legacy
    single-binding shape.
    """
    cargo_id: str
    source: str
    destination: str
    workcell_id: str
    instance_id: str
    object_ref: str
    source_ref: str
    target_ref: str
    yard_arm: str = ''

    def __post_init__(self):
        for value in (self.cargo_id, self.source, self.destination, self.workcell_id, self.instance_id,
                      self.object_ref, self.source_ref, self.target_ref):
            identifier(value)
            if len(value) > 192 or value != value.strip() or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in value):
                raise ValueError("Invalid physical transfer binding reference")
        if self.yard_arm and self.yard_arm not in YARD_ARMS:
            raise ValueError("A per-arm transfer binding names one yard arm A/B")
        if self.source_ref == self.target_ref:
            raise ValueError("Physical source and target must differ")

    def proposal_request(self, request_key):
        from harbor.fleet_contract import arm_request
        return arm_request(self, request_key)

    def check(self, job, request_key):
        from harbor.fleet_contract import validate_arm_job
        return validate_arm_job(self, job, request_key)

    def check_receipt(self, job, receipt):
        from harbor.fleet_contract import validate_arm_receipt
        return validate_arm_receipt(self, job, receipt)


def anchor_address(area_id, row, column, level):
    """Canonical placement-anchor address naming convention (user decision 2026-10-27)."""
    return f'{area_id}-r{row}-c{column}-l{level}'


@dataclass(frozen=True)
class SpatialPlacementBinding:
    """An inbound placement bound to installed physical references (design 29).

    The business key is the full placement tuple; the physical target reference
    names the placement anchor address, and the yard arm for retreat evidence
    lives in the binding (user decision 2026-10-27). The ledger-facing move is
    a plain ARM transfer to that anchor address.
    """
    cargo_id: str
    footprint_revision: int
    area_id: str
    row: int
    column: int
    level: int
    orientation: int
    source: str
    yard_arm: str
    workcell_id: str
    instance_id: str
    object_ref: str
    source_ref: str
    target_ref: str

    def __post_init__(self):
        for value in (self.cargo_id, self.area_id, self.source, self.workcell_id,
                      self.instance_id, self.object_ref, self.source_ref, self.target_ref):
            identifier(value)
            if len(value) > 192 or value != value.strip() or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in value):
                raise ValueError('Invalid spatial placement binding reference')
        if self.yard_arm not in YARD_ARMS:
            raise ValueError('Spatial placement binding names one yard arm A/B')
        if self.orientation not in (0, 90):
            raise ValueError('Spatial orientation is 0 or 90 degrees')
        if type(self.footprint_revision) is not int or self.footprint_revision < 1:
            raise ValueError('Footprint revision must be positive')
        for name in ('row', 'column', 'level'):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError('Placement grid coordinates are positive integers')
        if self.source_ref == self.target_ref:
            raise ValueError('Physical source and target must differ')
        if self.target_ref != anchor_address(self.area_id, self.row, self.column, self.level):
            raise ValueError('Physical target must name the placement anchor address')

    def anchor(self):
        return anchor_address(self.area_id, self.row, self.column, self.level)

    def matches(self, recommendation, footprint_revision, cargo_id):
        """The binding authorizes exactly this placement tuple (design 29)."""
        return (self.cargo_id == cargo_id and self.footprint_revision == footprint_revision
                and self.area_id == recommendation['area_id'] and self.row == recommendation['row']
                and self.column == recommendation['column'] and self.level == recommendation['level']
                and self.orientation == recommendation.get('orientation', 0))

    def move(self):
        return ArmTransferBinding(self.cargo_id, self.source, self.anchor(), self.workcell_id,
                                  self.instance_id, self.object_ref, self.source_ref, self.target_ref)


@dataclass(frozen=True)
class SimulationCellTemplate:
    """Temporary simulation-only binding for R2 rearrangement moves (design 32, 2026-10-31).

    Each move becomes an ARM transfer between placement anchor addresses on one
    workcell, run by the instance of the move's primary arm. The command gate
    accepts it only on the simulation path (synthetic observation, loopback Fleet).
    shortcut: anchors are named by convention, not installed and verified per move;
    replace with design 29 per-move cell bindings before any physical run.
    """
    simulation_cell_template: bool
    workcell_id: str
    instances: dict

    def __post_init__(self):
        if self.simulation_cell_template is not True:
            raise ValueError('Expected a simulation cell template')
        identifier(self.workcell_id)
        if (not isinstance(self.instances, dict) or not self.instances
                or not set(self.instances) <= set(YARD_ARMS)):
            raise ValueError('Simulation instances are keyed by yard arm A/B')
        for value in self.instances.values():
            identifier(value)

    def moves(self, plan_moves):
        bound = []
        for move in plan_moves:
            arm = move['primary_arm']
            if arm not in self.instances:
                raise ValueError('No simulation instance for this arm')
            ends = [anchor_address(p['area_id'], p['row'], p['column'], p['level'])
                    for p in (move['source_placement'], move['destination_placement'])]
            bound.append(ArmTransferBinding(move['cargo_id'], move['source'], move['destination'], self.workcell_id,
                                            self.instances[arm], move['cargo_id'], *ends, arm))
        return tuple(bound)


def load_bindings(items):
    """Installed binding entries: a footprint key makes a spatial placement binding."""
    bindings = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError('Installed bindings are objects')
        if 'simulation_cell_template' in item:
            bindings.append(SimulationCellTemplate(**item))
        else:
            bindings.append(SpatialPlacementBinding(**item) if 'footprint_revision' in item
                            else ArmTransferBinding(**item))
    return tuple(bindings)


def arm_candidates(bindings):
    """Ordered per-move arm correspondence from installed per-arm bindings (design 29).

    Installation order names the primary arm first; the legacy arm_reach table is
    replaced by this correspondence once it is verified and installed.
    """
    candidates = {}
    for binding in bindings:
        if isinstance(binding, ArmTransferBinding) and binding.yard_arm:
            key = (binding.cargo_id, binding.source, binding.destination)
            arms = candidates.setdefault(key, [])
            if binding.yard_arm not in arms:
                arms.append(binding.yard_arm)
    return {key: tuple(arms) for key, arms in candidates.items()}


def bind_plan(proposal: OutboundProposal,
              bindings: tuple[ProcessBinding | ArmTransferBinding, ...]) -> tuple[ProcessBinding | ArmTransferBinding, ...]:
    if proposal.status != "READY" or not proposal.moves:
        raise ValueError("Only a ready nonempty inventory proposal can be bound")
    if not isinstance(bindings, tuple) or not all(isinstance(b, (ProcessBinding, ArmTransferBinding)) for b in bindings):
        raise ValueError("Expected immutable installed bindings")
    by_move = {}
    for binding in bindings:
        key = (binding.cargo_id, binding.source, binding.destination)
        entries = by_move.setdefault(key, [])
        if isinstance(binding, ProcessBinding) or not binding.yard_arm:
            if entries:
                raise ValueError("Ambiguous process binding")  # One legacy entry per move.
            entries.append(binding)
        else:
            if any(isinstance(entry, ArmTransferBinding) and entry.yard_arm == binding.yard_arm for entry in entries):
                raise ValueError("Ambiguous process binding")  # Same arm twice for one move.
            entries.append(binding)
    try:
        # Per-arm entries resolve to the primary (first installed) arm's binding.
        return tuple(by_move[(m.cargo_id, m.source, m.destination)][0] for m in proposal.moves)
    except KeyError as exc:
        raise ValueError("A move has no installed process binding") from exc
