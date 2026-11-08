from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
import importlib
from pathlib import Path
import tempfile
import unittest

from harbor.rosy_http import RosyClient


class Fleet:
    def __init__(self):
        self.posts = 0
        self.jobs = {}
        self.lose_response = False

    def trip(self, candidates, steps, *, request_key):
        self.posts += 1
        from harbor.transport import TripIntent
        intent = TripIntent(request_key, tuple(candidates), steps[0]["to"], tuple(steps[0]["via"]))
        self.jobs[request_key] = {"mission_id": "transport-job", "request_key": request_key,
            "request_digest": intent.digest, "robot_id": candidates[0], "status": "READY",
            "current_step_index": 0, "reason": None, "updated_at": "2026-10-24T00:00:00+00:00",
            "steps": [{"step_index": 0, "step": steps[0], "status": "READY",
                       "action_id": None, "attempt_id": None, "reason": None}]}
        if self.lose_response:
            raise TimeoutError("response lost after commit")
        return {"job": self.jobs[request_key]}

    def trip_request(self, key):
        return {"job": self.jobs[key]}


class TransportTests(unittest.TestCase):
    def setUp(self):
        try:
            self.api = importlib.import_module("harbor.transport")
        except ModuleNotFoundError:
            self.fail("The neutral transport intent consumer has not been implemented")
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "business.sqlite3"
        self.intent = self.api.TripIntent("harbor/이송?key=1", ("robot-1", "robot-2"), "도착지", ("경유지",))
        self.ledger = self.api.TransportLedger(self.path)
        self.fleet = Fleet()

    def test_motion_request_contains_no_cargo_or_process_semantics(self):
        self.ledger.prepare(self.intent)
        self.ledger.submit(self.intent.request_key, self.fleet, operator_confirmed=True)
        self.assertEqual(self.fleet.posts, 1)
        self.assertEqual(self.ledger.read(self.intent.request_key)["state"], "ACKNOWLEDGED")

    def test_adapter_returns_normalized_facts_and_ledger_keeps_compatibility_result(self):
        from harbor.execution import read_trip_execution
        self.ledger.prepare(self.intent)
        job = self.ledger.submit(self.intent.request_key, self.fleet, operator_confirmed=True)
        snapshot = read_trip_execution(self.fleet, self.intent)
        self.assertEqual(snapshot.phase, 'awaiting_execution')
        self.assertEqual(snapshot.mission_id, job['mission_id'])
        self.assertEqual(snapshot.request_key, self.intent.request_key)
        self.assertIsNone(snapshot.attempt_id)
        self.assertFalse(snapshot.goal_confirmed)

    def test_request_digest_matches_the_existing_fleet_unicode_encoding(self):
        self.assertEqual(self.intent.digest, "0842a72ee6f927525270599d02ccde95735ca1370bac4bfc36d2fe816bb7cf77")  # sha256

    def test_start_requires_explicit_operator_confirmation(self):
        self.ledger.prepare(self.intent)
        for confirmation in (False, 1, "yes"):
            with self.subTest(confirmation=confirmation), self.assertRaises(ValueError):
                self.ledger.submit(self.intent.request_key, self.fleet, operator_confirmed=confirmation)
        self.assertEqual(self.fleet.posts, 0)

    def test_lost_post_is_reconciled_by_get_without_second_submission(self):
        self.ledger.prepare(self.intent)
        self.fleet.lose_response = True
        with self.assertRaises(TimeoutError):
            self.ledger.submit(self.intent.request_key, self.fleet, operator_confirmed=True)
        self.assertEqual(self.ledger.read(self.intent.request_key)["state"], "UNCERTAIN")
        restarted = self.api.TransportLedger(self.path)
        with self.assertRaises(ValueError):
            restarted.submit(self.intent.request_key, self.fleet, operator_confirmed=True)
        job = restarted.reconcile(self.intent.request_key, self.fleet)
        self.assertEqual(job["mission_id"], "transport-job")
        self.assertEqual(self.fleet.posts, 1)

    def test_conflicting_key_is_not_prepared_and_unknown_get_never_reposts(self):
        self.ledger.prepare(self.intent)
        with self.assertRaises(ValueError):
            self.ledger.prepare(replace(self.intent, destination="another-place"))
        with self.assertRaises(KeyError):
            self.ledger.reconcile(self.intent.request_key, self.fleet)
        self.assertEqual(self.fleet.posts, 0)

    def test_mismatched_digest_robot_or_steps_does_not_acknowledge(self):
        self.ledger.prepare(self.intent)
        self.fleet.trip(self.intent.candidates, self.intent.steps, request_key=self.intent.request_key)
        original = self.fleet.jobs[self.intent.request_key]
        for changes in ({"request_digest": "0" * 64}, {"robot_id": "another-robot"},
                        {"steps": [{**original["steps"][0], "step": {"to": "wrong-place", "via": []}}]}):
            self.fleet.jobs[self.intent.request_key] = {**original, **changes}
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.ledger.reconcile(self.intent.request_key, self.fleet)
            self.assertEqual(self.ledger.read(self.intent.request_key)["state"], "PLANNED")

    def test_robot_arrival_is_only_an_execution_acknowledgement(self):
        self.ledger.prepare(self.intent)
        self.ledger.submit(self.intent.request_key, self.fleet, operator_confirmed=True)
        job = self.fleet.jobs[self.intent.request_key]
        job.update(status="GOAL_CONFIRMED", current_step_index=1)
        job["steps"][0].update(status="GOAL_CONFIRMED", action_id="trip-1", attempt_id="trip-1")
        result = self.ledger.reconcile(self.intent.request_key, self.fleet)
        self.assertEqual(result["status"], "GOAL_CONFIRMED")
        self.assertEqual(self.ledger.read(self.intent.request_key)["state"], "ACKNOWLEDGED")
        self.assertNotIn("COMPLETED", self.ledger.read(self.intent.request_key).values())

    def test_late_response_after_restart_cannot_remove_uncertainty(self):
        self.ledger.prepare(self.intent)
        original = self.fleet.trip_request
        def read_after_restart(key):
            self.api.TransportLedger(self.path)
            return original(key)
        self.fleet.trip_request = read_after_restart
        with self.assertRaises(ValueError):
            self.ledger.submit(self.intent.request_key, self.fleet, operator_confirmed=True)
        self.assertEqual(self.ledger.read(self.intent.request_key)["state"], "UNCERTAIN")

    def test_snapshot_identity_cannot_change_after_acknowledgement(self):
        self.ledger.prepare(self.intent)
        self.ledger.submit(self.intent.request_key, self.fleet, operator_confirmed=True)
        self.fleet.jobs[self.intent.request_key]["mission_id"] = "another-job"
        with self.assertRaises(ValueError):
            self.ledger.reconcile(self.intent.request_key, self.fleet)

    def test_concurrent_submissions_send_one_durable_intent(self):
        self.ledger.prepare(self.intent)
        def submit():
            try:
                return self.ledger.submit(self.intent.request_key, self.fleet, operator_confirmed=True)
            except ValueError:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: submit(), range(2)))
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(self.fleet.posts, 1)

    def test_malformed_snapshot_and_invalid_intent_are_rejected(self):
        with self.assertRaises(ValueError):
            replace(self.intent, via=None)
        self.ledger.prepare(self.intent)
        self.fleet.trip(self.intent.candidates, self.intent.steps, request_key=self.intent.request_key)
        original = self.fleet.jobs[self.intent.request_key]
        for job in ([], {**original, 'mission_id': 'bad\nidentity'},
                    {**original, 'current_step_index': False},
                    {**original, 'status': 'GOAL_CONFIRMED', 'current_step_index': 0}):
            with self.subTest(job=job), self.assertRaises(ValueError):
                self.intent.check(job)


class HttpTransportTests(unittest.TestCase):
    def test_key_lookup_encodes_reserved_and_non_ascii_characters(self):
        calls = []
        client = RosyClient(transport=lambda *args: calls.append(args) or {"job": {}})
        self.assertTrue(callable(getattr(client, "trip_request", None)), "Trip GET adapter is missing")
        client.trip_request("harbor/이송?key=1")
        self.assertEqual(calls, [("GET", "/api/fleet/trip-jobs?request_key=harbor%2F%EC%9D%B4%EC%86%A1%3Fkey%3D1", None)])

    def test_job_lookup_encodes_id_without_mutation(self):
        calls = []
        client = RosyClient(transport=lambda *args: calls.append(args) or {"job": {}})
        self.assertTrue(callable(getattr(client, "trip_job", None)), "Trip snapshot adapter is missing")
        client.trip_job("job-1")
        self.assertEqual(calls, [("GET", "/api/fleet/trip-jobs/job-1", None)])
