"""Start the API as a process on PostgreSQL, stop it, and start it again.

Checks over HTTP that reservations, waitlist order and reservation numbering
survive a killed process, that --seed-fresh leaves a populated database alone,
and that a failed migration stops startup and leaves no partial schema.
Idempotency replay is not checked here; it is still kept in process memory.

Run from the project root with ENCORE_TEST_DATABASE_URL set:
    python3 -m unittest discover -s tests -p 'test_postgres_startup.py' -v
"""

import os
import socket
import subprocess
import sys
import time
import unittest
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import conformance
import psycopg
from conformance import delete, get, post
from postgres_support import PostgresTestCase, requires_postgres

APP = Path(__file__).resolve().parents[1] / "app.py"


def free_port():
    """Return a local TCP port that is free right now."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@requires_postgres
class StartupTests(PostgresTestCase):
    def app_command(self, *args):
        """Build the app.py command line for self.port plus extra arguments."""
        return [sys.executable, str(APP), "--port", str(self.port), *args]

    def app_env(self):
        """Return the environment that points app.py at the test database."""
        return {**os.environ, "ENCORE_DATABASE_URL": self.database_url}

    @contextmanager
    def running_app(self, *args):
        """Start app.py, wait until it serves requests, and kill it afterwards."""
        self.port = free_port()
        base = f"http://127.0.0.1:{self.port}"
        process = subprocess.Popen(self.app_command(*args), env=self.app_env(),
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        assert process.stdout is not None
        output = process.stdout
        try:
            deadline = time.monotonic() + 15
            while True:
                if process.poll() is not None:
                    self.fail(f"app exited early:\n{output.read().decode()}")
                try:
                    urllib.request.urlopen(base + "/events", timeout=1).close()
                    break
                except OSError:
                    if time.monotonic() > deadline:
                        self.fail("app did not start serving within 15 seconds")
                    time.sleep(0.1)
            with patch.object(conformance, "BASE", base):
                yield
        finally:
            process.kill()
            process.wait(timeout=5)
            output.close()

    def join(self, user_id):
        """Join ev_102's waitlist over HTTP with a new idempotency key."""
        return post("/events/ev_102/waitlist", f'{{"userId":"{user_id}"}}'.encode(),
                    str(uuid4()))

    def test_bookings_queue_order_and_ids_survive_a_killed_process(self):
        with self.running_app("--seed-fresh"):
            first = post("/events/ev_103/reservations", b"{}", str(uuid4()))
            second = post("/events/ev_103/reservations", b"{}", str(uuid4()))
            sold_out = post("/events/ev_103/reservations", b"{}", str(uuid4()))
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


if __name__ == "__main__":
    unittest.main()
