from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import sys
import unittest

from harbor.model import CargoHolding, PortConstraints, OutboundRequest, Bay, YardSnapshot
from harbor.outbound import plan_outbound
from harbor.site import Site
from harbor.availability import TransferVehicleState, transfer_candidates


NOW = datetime(2026, 10, 21, tzinfo=timezone.utc)


def yard(stack=("outbound", "rehandle")):
    return YardSnapshot(3, NOW, (
        Bay("yard", stack, 3), Bay("temp", (), 2), Bay("stand", (), 1),
    ))


REACH = frozenset({("A", "yard"), ("A", "temp"), ("A", "stand"), ("B", "yard"), ("B", "stand")})


def constraints():
    return PortConstraints("stand", ("temp",), frozenset({
        ("yard", "temp"), ("temp", "yard"), ("yard", "stand"),
    }), timedelta(seconds=5), "conveyor", REACH)


class OutboundTests(unittest.TestCase):
    def test_preview_reports_proposal_and_required_evidence(self):
        result = subprocess.run([sys.executable, "-m", "harbor"], check=True, capture_output=True, text=True,
                                cwd=Path(__file__).parents[1])
        proposal = json.loads(result.stdout)
        self.assertEqual(proposal["status"], "READY")
        self.assertEqual(len(proposal["moves"]), 3)
        self.assertEqual(proposal["required_completion"], [
            "outbound_placed_on_stand", "rehandles_restored", "arms_retreated",
        ])

    def plan(self, snapshot=None, rules=None, request=None, now=NOW):
        return plan_outbound(snapshot or yard(), request or OutboundRequest("outbound", 3),
                              rules or constraints(), now=now)

    def test_top_outbound_cargo_only_needs_its_own_move(self):
        proposal = self.plan(yard(("outbound",)))
        self.assertEqual(proposal.status, "READY")
        self.assertEqual([(m.cargo_id, m.source, m.destination) for m in proposal.moves],
                         [("outbound", "yard", "stand")])
        self.assertIn("outbound_placed_on_stand", proposal.required_completion)
        self.assertIn("arms_retreated", proposal.required_completion)

    def test_rehandle_is_restored_and_observation_is_unchanged(self):
        snapshot = yard()
        proposal = self.plan(snapshot)
        self.assertEqual(proposal.status, "READY")
        self.assertEqual([(m.cargo_id, m.source, m.destination) for m in proposal.moves], [
            ("rehandle", "yard", "temp"), ("outbound", "yard", "stand"),
            ("rehandle", "temp", "yard"),
        ])
        self.assertEqual(snapshot.bays[0].cargo, ("outbound", "rehandle"))
        self.assertEqual(proposal.source_revision, 3)
        self.assertEqual(proposal, self.plan(snapshot))

    def test_multiple_rehandles_restore_original_order(self):
        proposal = self.plan(yard(("outbound", "b1", "b2")))
        self.assertEqual([m.cargo_id for m in proposal.moves], ["b2", "b1", "outbound", "b1", "b2"])

    def test_no_legal_temporary_capacity_is_hold_without_partial_plan(self):
        snapshot = yard(("outbound", "b1", "b2"))
        snapshot = replace(snapshot, bays=(snapshot.bays[0], Bay("temp", (), 1), snapshot.bays[2]))
        proposal = self.plan(snapshot)
        self.assertEqual(proposal.status, "HOLD")
        self.assertEqual(proposal.reason, "NO_LEGAL_TEMPORARY_BAY")
        self.assertEqual(proposal.moves, ())

    def test_missing_return_reachability_is_hold(self):
        rules = replace(constraints(), allowed_moves=frozenset({("yard", "temp"), ("yard", "stand")}))
        self.assertEqual(self.plan(rules=rules).status, "HOLD")

    def test_unreachable_outbound_cargo_is_hold(self):
        self.assertEqual(self.plan(rules=replace(constraints(), allowed_moves=frozenset())).reason,
                         "OUTBOUND_MOVE_UNAVAILABLE")

    def test_stale_and_future_observations_are_hold(self):
        for delta in (-1, 6):
            with self.subTest(delta=delta):
                self.assertEqual(self.plan(now=NOW + timedelta(seconds=delta)).reason,
                                 "OBSERVATION_NOT_CURRENT")

    def test_unknown_or_known_physical_holding_is_not_a_free_bay(self):
        for holder in (None, "arm-a"):
            snapshot = replace(yard(), holdings=(CargoHolding("airborne", holder),))
            self.assertEqual(self.plan(snapshot).reason, "PHYSICAL_HOLDING_REQUIRES_RECONCILIATION")

    def test_revision_missing_outbound_and_full_destination_do_not_produce_moves(self):
        cases = [self.plan(request=OutboundRequest("outbound", 2)),
                 self.plan(request=OutboundRequest("missing", 3)),
                 self.plan(replace(yard(), bays=yard().bays[:2] + (Bay("stand", ("other",), 1),)))]
        for proposal in cases:
            self.assertEqual(proposal.status, "HOLD")
            self.assertFalse(proposal.moves)

    def test_outbound_on_stand_is_not_reported_as_completed(self):
        snapshot = replace(yard(()), bays=(Bay("yard", (), 3), Bay("temp", (), 2),
                                           Bay("stand", ("outbound",), 1)))
        self.assertEqual(self.plan(snapshot).reason, "OUTBOUND_ALREADY_ON_STAND_REQUIRES_EVIDENCE")

    def test_each_step_names_primary_and_alternate_yard_arms(self):
        proposal = self.plan()
        self.assertEqual([(m.primary_arm, m.alternate_arms) for m in proposal.moves],
                         [("A", ()), ("A", ("B",)), ("A", ())])
        self.assertNotIn("S", {a for m in proposal.moves for a in (m.primary_arm, *m.alternate_arms)})

    def test_missing_or_insufficient_reach_table_is_hold(self):
        for reach, reason in [(frozenset(), "ARM_REACH_UNVERIFIED"),
                              (REACH - {("A", "temp")}, "NO_ARM_REACHES_MOVE")]:
            with self.subTest(reason=reason):
                proposal = self.plan(rules=replace(constraints(), arm_reach=reach))
                self.assertEqual((proposal.status, proposal.reason, proposal.moves), ("HOLD", reason, ()))

    def test_per_arm_temporary_bays_pick_one_an_arm_can_serve(self):
        # A plate per arm: temp_a only A reaches, temp_b only B; the yard row here only B reaches.
        snapshot = YardSnapshot(3, NOW, (Bay("yard", ("outbound", "rehandle"), 3), Bay("temp_a", (), 3),
                                         Bay("temp_b", (), 3), Bay("stand", (), 1)))
        rules = PortConstraints("stand", ("temp_a", "temp_b"), frozenset({
            ("yard", "temp_a"), ("temp_a", "yard"), ("yard", "temp_b"), ("temp_b", "yard"), ("yard", "stand"),
        }), timedelta(seconds=5), "conveyor",
            frozenset({("A", "temp_a"), ("B", "yard"), ("B", "temp_b"), ("B", "stand")}))
        proposal = self.plan(snapshot, rules)
        self.assertEqual(proposal.status, "READY")
        self.assertEqual([(m.cargo_id, m.source, m.destination, m.primary_arm) for m in proposal.moves], [
            ("rehandle", "yard", "temp_b", "B"), ("outbound", "yard", "stand", "B"), ("rehandle", "temp_b", "yard", "B")])

    def test_supply_arm_s_cannot_be_in_the_reach_table(self):
        with self.assertRaises(ValueError):
            replace(constraints(), arm_reach=REACH | {("S", "yard")})

    def test_conveyor_and_outbound_stand_are_never_temporary_bays(self):
        for temporary in (("conveyor",), ("stand",), ("temp", "conveyor")):
            with self.subTest(temporary=temporary), self.assertRaises(ValueError):
                replace(constraints(), temporary_bays=temporary)

    def test_outbound_cargo_on_the_conveyor_is_hold(self):
        snapshot = replace(yard(()), bays=(Bay("conveyor", ("outbound",), 1), Bay("temp", (), 2),
                                           Bay("stand", (), 1), Bay("yard", (), 3)))
        self.assertEqual(self.plan(snapshot).reason, "OUTBOUND_NOT_IN_A_BAY")

    def test_unknown_bay_configuration_is_rejected(self):
        with self.assertRaises(ValueError):
            self.plan(rules=replace(constraints(), temporary_bays=("missing",)))


class ModelTests(unittest.TestCase):
    def test_duplicate_cargo_bays_and_overcapacity_rejected(self):
        for bays in [(Bay("a", ("x",), 1), Bay("b", ("x",), 1)),
                      (Bay("a", (), 1), Bay("a", (), 1))]:
            with self.subTest(bays=bays), self.assertRaises(ValueError):
                YardSnapshot(1, NOW, bays)
        with self.assertRaises(ValueError):
            Bay("a", ("x", "y"), 1)

    def test_observed_and_held_cargo_cannot_overlap(self):
        with self.assertRaises(ValueError):
            replace(yard(), holdings=(CargoHolding("outbound", "arm-a"),))

    def test_invalid_time_revision_and_identifiers_rejected(self):
        for changes in ({"revision": -1}, {"revision": True}, {"observed_at": NOW.replace(tzinfo=None)}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(yard(), **changes)
        with self.assertRaises(ValueError):
            OutboundRequest("", 1)
        with self.assertRaises(ValueError):
            Bay("a", (), True)
        with self.assertRaises(ValueError):
            replace(constraints(), max_observation_age=timedelta(0))

    def test_mutable_containers_rejected(self):
        with self.assertRaises(ValueError):
            Bay("a", ["x"], 1)
        with self.assertRaises(ValueError):
            replace(constraints(), temporary_bays=["temp"])


class SiteTests(unittest.TestCase):
    def site_data(self):
        return json.loads((Path(__file__).parents[1] / "config/site.example.json").read_text())

    def test_example_and_identity_change_keep_roles_and_pool(self):
        data = self.site_data()
        site = Site.from_dict(data)
        self.assertEqual(site.max_moving_robots, 1)
        self.assertEqual(site.charging_positions, 1)
        self.assertEqual(len(site.transfer_vehicle_ids), 3)
        data["arms"] = {role: "renamed-" + device for role, device in data["arms"].items()}
        data["transfer_vehicle_ids"] = ["new-1", "new-2", "new-3"]
        changed = Site.from_dict(data)
        self.assertEqual(changed.allowed_moves, site.allowed_moves)
        self.assertEqual(set(changed.arms), {"A", "B"})

    def test_supply_arm_s_is_an_external_step_not_a_commanded_arm(self):
        site = Site.from_dict(self.site_data())
        self.assertNotIn("S", site.arms)
        self.assertEqual(site.external_supply_arm, "demo-supply")
        data = self.site_data()
        data["arms"]["S"] = data.pop("external_supply_arm")
        with self.assertRaisesRegex(ValueError, "external step"):
            Site.from_dict(data)

    def test_two_location_layers(self):
        cells = Site.from_dict(self.site_data()).cell_locations
        self.assertEqual([cells.business_location(c) for c in ("yard", "temp_a", "temp_b", "stand", "conveyor", "supply-bench")],
                         ["yard", "yard", "yard", "outbound_point", None, None])
        with self.assertRaises(ValueError):
            cells.business_location("wait")

    def test_conveyor_mechanics_are_not_harbor_configuration(self):
        data = self.site_data()
        data["conveyor"] = {"capacity": 1, "occupancy_evidence": "camera"}
        with self.assertRaisesRegex(ValueError, "ADR 0029"):
            Site.from_dict(data)

    def test_yard_arms_never_move_cargo_onto_the_conveyor_or_supply_bench(self):
        for edge in (["yard", "conveyor"], ["yard", "supply-bench"], ["supply-bench", "yard"]):
            data = self.site_data()
            data["allowed_moves"].append(edge)
            with self.subTest(edge=edge), self.assertRaises(ValueError):
                Site.from_dict(data)

    def test_pre_adr_0005_keys_are_explicit_errors(self):
        for old, new in [("transport_ids", "transfer_vehicle_ids"), ("slots", "cell_locations.bays"),
                         ("waiting_slots", "waiting_positions"), ("charging_slots", "charging_positions"),
                         ("transfers", "allowed_moves")]:
            data = self.site_data()
            data[old] = []
            with self.subTest(old=old), self.assertRaisesRegex(ValueError, f"renamed to '{new}'"):
                Site.from_dict(data)

    def test_bad_site_configuration_rejected(self):
        for field, value in [("charging_positions", 2), ("max_moving_robots", 2),
                             ("transfer_vehicle_ids", ["p1", "p1", "p3"]),
                             ("arms", {"A": "a", "B": "a"}),
                             ("external_supply_arm", "demo-yard-a"),
                             ("waiting_positions", ["yard"]),
                             ("allowed_moves", [["yard", "missing"]])]:
            data = self.site_data()
            data[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                Site.from_dict(data)


class AvailabilityTests(unittest.TestCase):
    def test_manual_charging_loaded_unknown_and_maintenance_excluded(self):
        pool = ("p1", "p2", "p3")
        for status in ("charging", "loaded", "unknown", "maintenance"):
            states = (TransferVehicleState("p1", status), TransferVehicleState("p2", "available"))
            self.assertEqual(transfer_candidates(pool, states), ("p2",))

    def test_charger_arrival_is_not_availability_or_charging_confirmation(self):
        self.assertEqual(transfer_candidates(("p1",), (TransferVehicleState("p1", "waiting_charge"),)), ())

    def test_duplicate_unknown_robot_and_double_charging_rejected(self):
        for states in [(TransferVehicleState("p1", "available"), TransferVehicleState("p1", "loaded")),
                       (TransferVehicleState("other", "available"),),
                       (TransferVehicleState("p1", "charging"), TransferVehicleState("p2", "charging"))]:
            with self.subTest(states=states), self.assertRaises(ValueError):
                transfer_candidates(("p1", "p2", "p3"), states)


if __name__ == "__main__":
    unittest.main()
