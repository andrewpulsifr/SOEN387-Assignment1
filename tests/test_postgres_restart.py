"""One killed and restarted app.py on a retained PostgreSQL database, checked over HTTP.

Before the kill the test stores a live Hold, a confirmed Hold with its
Reservation, a released Hold, two waitlist entries, a 201 receipt and a 409
sold_out. After a restart without --seed-fresh, the live Hold keeps its
deadline while its countdown drops, the other records are unchanged, both
keys replay without writing, and the next Reservation number follows the
stored one. The "encore.restart" logger, printed by tests/run_restart.py,
records what the test saw.

Run from the project root with ENCORE_TEST_DATABASE_URL set:
    python3 -m unittest discover -s tests -p 'test_postgres_restart.py' -v
"""

import logging
import time
import unittest
from uuid import uuid4

from conformance import get, post
from hold_support import confirm, create, join_waitlist, release, seats_left, validate
from postgres_support import AppOnPostgresTestCase, requires_postgres

BOTH_STEPS = "Migrations applied: 001_initial.sql, 002_holds_and_reservation_link.sql"
NO_STEPS = "Migrations applied: none pending"
SETUP_PREFIXES = ("Migrations applied", "Fresh fixture", "Database already", "Hold TTL")

log = logging.getLogger("encore.restart")


def setup_lines(output):
    """Return the lines app.py printed while preparing the database, before serving."""
    return [line for line in output if line.startswith(SETUP_PREFIXES)]


@requires_postgres
class RestartHttpTests(AppOnPostgresTestCase):
    def stored_rows(self):
        """Return every Hold row and the migration ledger, as PostgreSQL stores them."""
        holds = self.query("SELECT id, created_at, expires_at, reservation_id, released_at"
                           " FROM holds ORDER BY created_at")
        ledger = self.query("SELECT version, name, applied_at FROM schema_migrations"
                            " ORDER BY version")
        return holds, ledger

    def test_holds_links_queue_and_receipts_survive_one_killed_process(self):
        live_key, sold_out_key = str(uuid4()), str(uuid4())
        first_output, restart_output = [], []
        with self.running_app("--seed-fresh", output_lines=first_output):
            live = create("ev_101", live_key)
            confirmed = create("ev_103", str(uuid4()))[1]
            reservation = confirm(confirmed["id"])
            spare = create("ev_103", str(uuid4()))[1]
            sold_out = create("ev_103", sold_out_key)
            released = release(spare["id"])
            queue = [join_waitlist("ev_102", user) for user in ("alice", "bob")]
            before = get(f"/holds/{live[1]['id']}")[1]
            rows_before = self.stored_rows()
        log.info("before restart: live Hold %s createdAt %s expiresAt %s secondsRemaining %s",
                 before["id"], before["createdAt"], before["expiresAt"],
                 before["secondsRemaining"])
        log.info("before restart: Hold %s confirmed -> %s %s; Hold %s released -> %s;"
                 " sold-out Hold request -> %s %s", confirmed["id"], reservation[0],
                 reservation[1]["id"], spare["id"], released[0], *sold_out[:2])
        log.info("before restart: ev_102 waitlist %s",
                 [entry[1]["userId"] for entry in queue])
        log.info("first start printed: %s", setup_lines(first_output))
        for version, name, applied_at in rows_before[1]:
            log.info("schema_migrations: %s %s applied at %s", version, name,
                     applied_at.isoformat())

        time.sleep(1)  # Downtime between kill and restart, so the countdown must drop.
        log.info("--- app.py killed; restarted on the same database without --seed-fresh ---")
        with self.running_app(output_lines=restart_output):
            after = get(f"/holds/{live[1]['id']}")[1]
            confirmed_after = get(f"/holds/{confirmed['id']}")
            reservation_after = get(f"/reservations/{reservation[1]['id']}")
            spare_after = get(f"/holds/{spare['id']}")[1]
            queue_after = get("/events/ev_102/waitlist")
            replays = [create("ev_101", live_key), create("ev_103", sold_out_key)]
            seats_after_replays = seats_left("ev_103")
            rows_after_replays = self.stored_rows()
            booking = post("/events/ev_103/reservations", b"{}", str(uuid4()))
        log.info("restart printed: %s", setup_lines(restart_output))
        log.info("after restart: live Hold %s %s expiresAt %s secondsRemaining %s",
                 after["id"], after["status"], after["expiresAt"], after["secondsRemaining"])
        log.info("after restart: Hold %s %s reservationId %s; GET /reservations/%s -> %s;"
                 " Hold %s %s", confirmed["id"], confirmed_after[1]["status"],
                 confirmed_after[1]["reservationId"], reservation[1]["id"],
                 reservation_after[0], spare["id"], spare_after["status"])
        log.info("after restart: ev_102 waitlist %s",
                 [(entry["userId"], entry["position"]) for entry in queue_after[1]["entries"]])
        for name, (status, body, headers) in zip(("live Hold key", "sold-out key"), replays):
            log.info("after restart: %s retried -> %s %s, Idempotency-Replayed %s",
                     name, status, body, headers["Idempotency-Replayed"])
        log.info("after restart: ev_103 seatsLeft %s; stored Hold rows and ledger %s",
                 seats_after_replays,
                 "unchanged" if rows_after_replays == rows_before else "CHANGED")
        log.info("after restart: new booking on ev_103 -> %s %s", *booking)

        self.assertEqual((live[0], reservation[0], sold_out[:2], released),
                         (201, 201, (409, {"error": "sold_out"}), (204, None)))
        self.assertEqual([entry[0] for entry in queue], [201, 201])
        self.assertEqual(setup_lines(first_output)[:2], [BOTH_STEPS, "Fresh fixture loaded"])
        self.assertEqual(setup_lines(restart_output)[0], NO_STEPS)
        self.assertNotIn("Fresh fixture loaded", setup_lines(restart_output))

        # Same Hold and deadline, a smaller countdown: nothing renewed the TTL.
        validate("/holds/{holdId}", "get", 200, after)
        self.assertEqual(after, {**live[1], "secondsRemaining": after["secondsRemaining"]})
        self.assertGreater(after["secondsRemaining"], 0)
        self.assertLess(after["secondsRemaining"], before["secondsRemaining"])
        self.assertEqual(rows_after_replays, rows_before)

        self.assertEqual(confirmed_after, (200, {
            **confirmed, "status": "confirmed", "secondsRemaining": 0,
            "reservationId": reservation[1]["id"]}))
        self.assertEqual(reservation_after, (200, reservation[1]))
        self.assertEqual(spare_after, {**spare, "status": "released", "secondsRemaining": 0})
        self.assertEqual(queue_after, (200, {"entries": [
            {**queue[0][1], "position": 1}, {**queue[1][1], "position": 2}]}))

        # Both keys replay their stored outcome. Running the sold-out request
        # again would have taken ev_103's free seat and returned 201.
        self.assertEqual([replay[:2] for replay in replays], [live[:2], sold_out[:2]])
        self.assertEqual([replay[2]["Idempotency-Replayed"] for replay in replays],
                         ["true", "true"])
        self.assertEqual(seats_after_replays, 1)
        self.assertEqual((reservation[1]["id"], booking[0], booking[1]["id"]),
                         ("res_1", 201, "res_2"))


if __name__ == "__main__":
    unittest.main()
