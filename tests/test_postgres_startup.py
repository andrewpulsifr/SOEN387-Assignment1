"""Start the API as a process on PostgreSQL, stop it, and start it again.

Checks over HTTP that reservations, waitlist order, reservation numbering and
stored replay outcomes survive a killed process, that --seed-fresh leaves a
populated database alone, and that a failed migration or an invalid
ENCORE_HOLD_TTL_SECONDS stops startup and leaves no partial schema.

Run from the project root with ENCORE_TEST_DATABASE_URL set:
    python3 -m unittest discover -s tests -p 'test_postgres_startup.py' -v
"""

import subprocess
import unittest
from uuid import uuid4

import psycopg
from conformance import delete, get, post, post_with_headers
from postgres_support import AppOnPostgresTestCase, free_port, requires_postgres


@requires_postgres
class StartupTests(AppOnPostgresTestCase):
    def join(self, user_id):
        """Join ev_102's waitlist over HTTP with a new idempotency key."""
        return post("/events/ev_102/waitlist", f'{{"userId":"{user_id}"}}'.encode(),
                    str(uuid4()))

    def test_bookings_queue_order_and_ids_survive_a_killed_process(self):
        keys = [str(uuid4()) for _ in range(3)]
        with self.running_app("--seed-fresh"):
            first, second, sold_out = (post("/events/ev_103/reservations", b"{}", key)
                                       for key in keys)
            (_, alice), (_, bob), (_, carol) = (self.join(user)
                                                for user in ("alice", "bob", "carol"))
            self.assertEqual(delete(f"/events/ev_102/waitlist/{bob['id']}"), 204)
        self.assertEqual([first[0], second[0], sold_out[0]], [201, 201, 409])

        for restart_args in ((), ("--seed-fresh",)):
            with self.subTest(restart_args=restart_args), self.running_app(*restart_args):
                self.assertEqual(get(f"/reservations/{first[1]['id']}"), (200, first[1]))
                self.assertEqual(get(f"/reservations/{second[1]['id']}"),
                                 (200, second[1]))
                self.assertEqual(get("/events/ev_103")[1]["seatsLeft"], 0)
                self.assertEqual(get("/events/ev_102/waitlist"), (200, {"entries": [
                    {**alice, "position": 1}, {**carol, "position": 2},
                ]}))
                for key, original in zip(keys, (first, second, sold_out)):
                    status, body, headers = post_with_headers(
                        "/events/ev_103/reservations", b"{}", key)
                    self.assertEqual((status, body), original)
                    self.assertEqual(headers["Idempotency-Replayed"], "true")

        with self.running_app():
            third = post("/events/ev_101/reservations", b"{}", str(uuid4()))
            dave = self.join("dave")
        self.assertEqual([first[1]["id"], second[1]["id"], third[1]["id"]],
                         ["res_1", "res_2", "res_3"])
        self.assertEqual(dave[1]["position"], 3)

    def test_failed_migration_stops_startup_and_rolls_back(self):
        with psycopg.connect(self.database_url) as conn:
            conn.execute("CREATE TABLE waitlist_entries (id INT)")
        self.port = free_port()

        result = subprocess.run(self.app_command(), env=self.app_env(), check=False,
                                capture_output=True, text=True, timeout=30)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Startup stopped", result.stderr)
        self.assertEqual(self.query("SELECT count(*) FROM schema_migrations"), [(0,)])
        self.assertEqual(self.query("SELECT to_regclass('events')"), [(None,)])

    def test_invalid_hold_ttl_stops_startup_before_migrating(self):
        """Zero, negative, fractional and non-numeric TTLs exit 1 and leave no schema."""
        self.port = free_port()
        for ttl in ("0", "-5", "1.5", "ninety", ""):
            with self.subTest(ttl=ttl):
                result = subprocess.run(
                    self.app_command(), env=self.app_env({"ENCORE_HOLD_TTL_SECONDS": ttl}),
                    check=False, capture_output=True, text=True, timeout=30,
                )
                self.assertEqual(result.returncode, 1)
                self.assertIn("ENCORE_HOLD_TTL_SECONDS", result.stderr)
        self.assertEqual(self.query("SELECT to_regclass('schema_migrations')"), [(None,)])


if __name__ == "__main__":
    unittest.main()
