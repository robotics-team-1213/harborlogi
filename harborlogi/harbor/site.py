from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from harbor.model import YARD_ARMS, identifier, identifiers

# Business locations (logical, ADR 0005 decision 9): Harbor judges order and completion here.
# "vessel" is only where Pinky picks cargo up in the first scope; it has no cell location.
BUSINESS_LOCATIONS = ("vessel", "yard", "outbound_point")

# Pre-ADR-0005 configuration keys; reading one is an error, never a silent ignore.
_RENAMED = {
    "transport_ids": "transfer_vehicle_ids",
    "slots": "cell_locations.bays",
    "waiting_slots": "waiting_positions",
    "charging_slots": "charging_positions",
    "transfers": "allowed_moves",
}


@dataclass(frozen=True)
class CellLocations:
    """Physical cell locations; platform geometry owns their shape and reach."""
    supply_bench: str
    conveyor: str
    bays: tuple[str, ...]
    temporary_bays: tuple[str, ...]
    outbound_stand: str

    def __post_init__(self):
        identifiers(self.bays)
        identifiers(self.temporary_bays)
        if not self.bays or not self.temporary_bays:
            raise ValueError("Expected at least one bay and one temporary bay")
        identifiers((self.supply_bench, self.conveyor, *self.bays, *self.temporary_bays, self.outbound_stand))

    def business_location(self, cell_id: str) -> str | None:
        """Map a cell location to its business location; the intake path has none."""
        if cell_id in self.bays or cell_id in self.temporary_bays:
            return "yard"
        if cell_id == self.outbound_stand:
            return "outbound_point"
        if cell_id in (self.supply_bench, self.conveyor):
            return None
        raise ValueError("Unknown cell location")


@dataclass(frozen=True)
class Site:
    arms: Mapping[str, str]
    external_supply_arm: str
    transfer_vehicle_ids: tuple[str, ...]
    cell_locations: CellLocations
    waiting_positions: tuple[str, ...]
    allowed_moves: frozenset[tuple[str, str]]
    charging_positions: int
    max_moving_robots: int

    def __post_init__(self):
        if not isinstance(self.arms, Mapping) or set(self.arms) != set(YARD_ARMS):
            raise ValueError("Harbor assigns only yard arms A/B; supply arm S is an external step")
        identifier(self.external_supply_arm)
        identifiers(self.transfer_vehicle_ids)
        identifiers((*self.arms.values(), self.external_supply_arm))
        if len(self.transfer_vehicle_ids) != 3 or {*self.arms.values(), self.external_supply_arm} & set(self.transfer_vehicle_ids):
            raise ValueError("Expected three distinct transfer vehicles, separate from arms")
        if not isinstance(self.cell_locations, CellLocations):
            raise ValueError("Expected cell locations")
        identifiers(self.waiting_positions)
        cells = self.cell_locations
        if not self.waiting_positions or set(self.waiting_positions) & {
                cells.supply_bench, cells.conveyor, *cells.bays, *cells.temporary_bays, cells.outbound_stand}:
            raise ValueError("Expected waiting positions separate from cell locations")
        if type(self.charging_positions) is not int or self.charging_positions != 1:
            raise ValueError("Current site has one conceptual charging position")
        if type(self.max_moving_robots) is not int or self.max_moving_robots != 1:
            raise ValueError("Current site retains serial movement")
        if not isinstance(self.allowed_moves, frozenset):
            raise ValueError("Expected immutable allowed moves")
        yard_arm_cells = {*cells.bays, *cells.temporary_bays, cells.outbound_stand}
        for edge in self.allowed_moves:
            if not isinstance(edge, tuple) or len(edge) != 2:
                raise ValueError("Expected directed move")
            identifiers(edge)
            # A/B take cargo off the conveyor end but never put it on the conveyor or the supply bench.
            if edge[0] not in yard_arm_cells | {cells.conveyor} or edge[1] not in yard_arm_cells:
                raise ValueError("Allowed move references a location yard arms cannot use")
        object.__setattr__(self, "arms", MappingProxyType(dict(self.arms)))

    @classmethod
    def from_dict(cls, data: dict) -> "Site":
        if not isinstance(data, dict):
            raise ValueError("Expected a site configuration object")
        for old, new in _RENAMED.items():
            if old in data:
                raise ValueError(f"Site configuration field {old!r} was renamed to {new!r} (ADR 0005)")
        if isinstance(data.get("arms"), dict) and "S" in data["arms"]:
            raise ValueError("Supply arm S is an external step: move it from 'arms' to 'external_supply_arm'")
        if "conveyor" in data:
            raise ValueError("Conveyor mechanics belong to the ROSY cell (ADR 0029): remove 'conveyor'")
        expected = {"arms", "external_supply_arm", "transfer_vehicle_ids", "cell_locations",
                    "waiting_positions", "allowed_moves", "charging_positions", "max_moving_robots"}
        if set(data) != expected:
            raise ValueError("Unknown or missing site configuration fields")
        cells = data["cell_locations"]
        if not isinstance(cells, dict) or set(cells) != {"supply_bench", "conveyor", "bays", "temporary_bays", "outbound_stand"}:
            raise ValueError("Unknown or missing cell location fields")
        for value in (data["transfer_vehicle_ids"], data["waiting_positions"], data["allowed_moves"],
                      cells["bays"], cells["temporary_bays"]):
            if not isinstance(value, list):
                raise ValueError("Expected configuration lists")
        if not all(isinstance(edge, list) for edge in data["allowed_moves"]):
            raise ValueError("Expected move pairs")
        return cls(data["arms"], data["external_supply_arm"], tuple(data["transfer_vehicle_ids"]),
                   CellLocations(cells["supply_bench"], cells["conveyor"], tuple(cells["bays"]),
                                 tuple(cells["temporary_bays"]), cells["outbound_stand"]),
                   tuple(data["waiting_positions"]), frozenset(tuple(e) for e in data["allowed_moves"]),
                   data["charging_positions"], data["max_moving_robots"])
