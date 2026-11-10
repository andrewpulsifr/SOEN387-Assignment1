"""A confirmation that waits for its Event's lock past the Hold's deadline, over HTTP.

The test starts app.py on PostgreSQL with 2-second Holds, creates a Hold on
ev_103 and locks ev_103's row from a second connection. It sends the
confirmation and reads pg_stat_activity to see it waiting on that lock before
the stored deadline and again after it, then releases the lock. The service
reads the clock after taking the lock, so the confirmation gets 409
hold_expired; a clock read on arrival or at transaction start would confirm
it. Observations go to the "encore.races" logger, which tests/run_races.py
prints.

Run from the project root with ENCORE_TEST_DATABASE_URL set:
    python3 -m unittest discover -s tests -p 'test_postgres_lock_wait.py' -v
"""

import logging
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import psycopg
from conformance import send
from hold_support import confirm, create, seats_left, validate
from postgres_support import AppOnPostgresTestCase, requires_postgres

HOLD_TTL_SECONDS = 2
CAPACITY = 2  # ev_103's Capacity in contracts/fixtures/fresh-events.json

log = logging.getLogger("encore.races")


def poll(read, done, timeout, waiting_for):
    """Call read() until done(result) is true and return that result.

    Fails with AssertionError, naming waiting_for, after timeout seconds.
    """
    give_up = time.monotonic() + timeout
    while True:
        result = read()
        if done(result):
            return result
        if time.monotonic() > give_up:
            raise AssertionError(f"no {waiting_for} within {timeout} seconds: {result}")
        time.sleep(0.02)


def lock_waiters(observer, holder_pid):
    """Return the database clock and the statements now waiting on holder_pid's locks.

    observer must be an autocommit connection: inside one transaction,
    PostgreSQL keeps showing the pg_stat_activity it read first.
    """
    return observer.execute(
        "SELECT clock_timestamp(), ARRAY(SELECT query FROM pg_stat_activity"
        " WHERE datname = current_database() AND wait_event_type = 'Lock'"
        " AND %s = ANY(pg_blocking_pids(pid)))", (holder_pid,)).fetchone()


@requires_postgres
class LockWaitHttpTests(AppOnPostgresTestCase):
    """A confirmation held up by a rival's Event lock, on its own app and database."""

    def test_confirmation_blocked_past_the_deadline_returns_hold_expired(self):
        """Waiting on the lock while live, released after expiresAt: 409, no Reservation."""
        with self.running_app("--seed-fresh",
                              env={"ENCORE_HOLD_TTL_SECONDS": str(HOLD_TTL_SECONDS)}):
            receipt = create("ev_103", str(uuid4()))[1]
            hold_id = receipt["id"]
            [(deadline,)] = self.query("SELECT expires_at FROM holds WHERE id = %s",
                                       (hold_id,))
            log.info("setup: Hold %s on ev_103, stored deadline %s",
                     hold_id, deadline.isoformat())
            # Exit order is reverse: the lock goes first, so a failing test
            # never leaves the request waiting while the thread pool shuts down.
            with (ThreadPoolExecutor(max_workers=1) as pool,
                  psycopg.connect(self.database_url, autocommit=True) as observer,
                  psycopg.connect(self.database_url) as holder):
                holder.execute("SELECT 1 FROM events WHERE id = 'ev_103' FOR UPDATE")
                holder_pid = holder.info.backend_pid
                pending = pool.submit(confirm, hold_id)

                seen_before, waiting_before = poll(
                    lambda: lock_waiters(observer, holder_pid),
                    lambda found: found[1], 1, "confirmation waiting on the lock")
                view_before = send("GET", f"/holds/{hold_id}")[1]
                log.info("%s: waiting on the lock: %s; GET says %s",
                         seen_before.isoformat(), waiting_before, view_before["status"])
                poll(lambda: lock_waiters(observer, holder_pid)[0],
                     lambda now: now > deadline, HOLD_TTL_SECONDS + 1,
                     "database time past the deadline")
                seen_after, waiting_after = lock_waiters(observer, holder_pid)
                log.info("%s: still waiting on the lock: %s; releasing it",
                         seen_after.isoformat(), waiting_after)
                holder.rollback()
                late = pending.result(timeout=5)

            log.info("POST /holds/%s/confirm -> %s %s", hold_id, late[0], late[1])
            view_after = send("GET", f"/holds/{hold_id}")[1]
            seats = seats_left("ev_103")
            reservations = self.query("SELECT count(*) FROM reservations")[0][0]
            stored_hold = self.query("SELECT reservation_id, released_at"
                                     " FROM holds WHERE id = %s", (hold_id,))
        log.info("final ev_103 (Capacity %s): seatsLeft %s; stored Reservations %s;"
                 " Hold %s", CAPACITY, seats, reservations, view_after["status"])

        self.assertLess(seen_before, deadline)
        self.assertEqual(view_before["status"], "held")
        self.assertGreater(seen_after, deadline)
        for waiting in (waiting_before, waiting_after):
            self.assertEqual(len(waiting), 1, waiting)
            self.assertIn("FROM events WHERE id = $1 FOR UPDATE", waiting[0])
        self.assertEqual(late, (409, {"error": "hold_expired"}))
        validate("/holds/{holdId}/confirm", "post", 409, late[1])
        self.assertEqual(view_after, {**receipt, "status": "expired",
                                      "secondsRemaining": 0})
        self.assertEqual((seats, reservations), (CAPACITY, 0))
        self.assertEqual(stored_hold, [(None, None)])


if __name__ == "__main__":
    unittest.main()
