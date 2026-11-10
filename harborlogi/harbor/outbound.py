from dataclasses import dataclass
from datetime import datetime, timedelta

from harbor.model import YARD_ARMS, PortConstraints, OutboundRequest, YardSnapshot, aware_time


@dataclass(frozen=True)
class CargoMove:
    cargo_id: str
    source: str
    destination: str
    # Step roles (ADR 0005 decision 3); Fleet claims the arm and switches to an alternate on HOLD/OFFLINE.
    primary_arm: str
    alternate_arms: tuple[str, ...] = ()


@dataclass(frozen=True)
class OutboundProposal:
    status: str
    reason: str
    source_revision: int
    moves: tuple[CargoMove, ...] = ()
    required_completion: tuple[str, ...] = (
        "outbound_placed_on_stand", "rehandles_restored", "arms_retreated",
    )


def plan_outbound(snapshot: YardSnapshot, request: OutboundRequest,
                   constraints: PortConstraints, *, now: datetime, arm_candidates=None) -> OutboundProposal:
    """Propose inventory moves without changing observations or granting execution.

    Arm assignment prefers the installed per-(move, arm) correspondence when present
    (design 29); without it the legacy installer arm_reach table decides, and with
    neither the plan holds unverified.
    """
    aware_time(now)
    bays = {s.bay_id: s for s in snapshot.bays}
    referenced = {constraints.outbound_stand, *constraints.temporary_bays}
    referenced.update(s for edge in constraints.allowed_moves for s in edge)
    referenced.discard(constraints.conveyor)  # Intake path, not a yard observation.
    if not referenced <= bays.keys():
        raise ValueError("Constraints reference an unknown bay")

    def hold(reason):
        return OutboundProposal("HOLD", reason, snapshot.revision)

    if snapshot.revision != request.expected_revision:
        return hold("REVISION_CONFLICT")
    if not timedelta(0) <= now - snapshot.observed_at <= constraints.max_observation_age:
        return hold("OBSERVATION_NOT_CURRENT")
    if snapshot.holdings:
        return hold("PHYSICAL_HOLDING_REQUIRES_RECONCILIATION")
    source = next((s for s in snapshot.bays if request.cargo_id in s.cargo), None)
    if source is None:
        return hold("OUTBOUND_NOT_OBSERVED")
    if source.bay_id == constraints.conveyor:
        return hold("OUTBOUND_NOT_IN_A_BAY")  # Restoring rehandles there would load the conveyor.
    if source.bay_id == constraints.outbound_stand:
        return hold("OUTBOUND_ALREADY_ON_STAND_REQUIRES_EVIDENCE")
    output = bays[constraints.outbound_stand]
    if len(output.cargo) >= output.capacity:
        return hold("OUTBOUND_STAND_FULL")
    if (source.bay_id, output.bay_id) not in constraints.allowed_moves:
        return hold("OUTBOUND_MOVE_UNAVAILABLE")

    planned = {s.bay_id: list(s.cargo) for s in snapshot.bays}
    moves = []
    displaced = []
    while planned[source.bay_id][-1] != request.cargo_id:
        legal = [s for s in constraints.temporary_bays
                 if s != source.bay_id
                 and len(planned[s]) < bays[s].capacity
                 and (source.bay_id, s) in constraints.allowed_moves
                 and (s, source.bay_id) in constraints.allowed_moves]
        # Temporary bays can belong to one arm each (a plate per arm): prefer one an arm serves both ways.
        # With none, keep the first legal bay so the reach check below holds with NO_ARM_REACHES_MOVE as before.
        temporary = next((s for s in legal if arm_candidates is None and any(
            (a, source.bay_id) in constraints.arm_reach and (a, s) in constraints.arm_reach for a in YARD_ARMS)),
            legal[0] if legal else None)
        if temporary is None:
            return hold("NO_LEGAL_TEMPORARY_BAY")
        cargo = planned[source.bay_id].pop()
        planned[temporary].append(cargo)
        moves.append((cargo, source.bay_id, temporary))
        displaced.append((cargo, temporary))
    moves.append((request.cargo_id, source.bay_id, output.bay_id))
    moves.extend((cargo, temporary, source.bay_id) for cargo, temporary in reversed(displaced))

    if arm_candidates is None and not constraints.arm_reach:
        return hold("ARM_REACH_UNVERIFIED")
    assigned = []
    for cargo, origin, destination in moves:
        if arm_candidates is None:
            arms = [a for a in YARD_ARMS if (a, origin) in constraints.arm_reach and (a, destination) in constraints.arm_reach]
        else:
            named = arm_candidates.get((cargo, origin, destination), ())
            arms = list(dict.fromkeys(a for a in named if a in YARD_ARMS))
        if not arms:
            return hold("NO_ARM_REACHES_MOVE")
        assigned.append(CargoMove(cargo, origin, destination, arms[0], tuple(arms[1:])))
    return OutboundProposal("READY", "PROPOSAL_REQUIRES_ADMISSION_AND_EVIDENCE",
                             snapshot.revision, tuple(assigned))
