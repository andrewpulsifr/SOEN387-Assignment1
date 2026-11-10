"""Booking and waitlist commands on PostgreSQL, through the service functions.

Checks that event reads count stored reservations, that waitlist positions
follow join order, that a failed unit of work leaves nothing behind, that
booking, join and leave wait for the event row lock and then see the rival
transaction's write, and that a Hold request that waited for the lock judges
expiry with a clock read after the wait. Each test seeds its own database;
see postgres_support.

Run from the project root with ENCORE_TEST_DATABASE_URL set:
    python3 -m unittest discover -s tests -p 'test_postgres_booking.py' -v
"""

import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from uuid import UUID, uuid4

import psycopg
from postgres_support import ServicesOnPostgresTestCase, requires_postgres

import hold_services
import services
from postgres_booking import PostgresBookingCommands


@requires_postgres
class PostgresBookingTests(ServicesOnPostgresTestCase):
    def reserve(self, event_id):
        """Book one seat with a new idempotency key."""
        return services.create_reservation(event_id, str(uuid4()), b"", None)

    def join(self, event_id, user_id):
        """Join the event's waitlist with a new idempotency key."""
        body = f'{{"userId":"{user_id}"}}'.encode()
        return services.join_waitlist(event_id, str(uuid4()), body, "application/json")

    def test_event_list_and_bookable_filter_count_stored_reservations(self):
        self.reserve("ev_103")
        self.reserve("ev_103")
        self.reserve("ev_101")

        self.assertEqual(services.list_events(None), ({"events": [
            {"id": "ev_101", "title": "Jazz Ensemble: Fall Concert", "seatsLeft": 41},
            {"id": "ev_102", "title": "Improv Night", "seatsLeft": 0},
            {"id": "ev_103", "title": "Film Society: Friday Screening", "seatsLeft": 0},
        ]}, 200))
        bookable, status = services.list_events("true")
        self.assertEqual((status, [event["id"] for event in bookable["events"]]),
                         (200, ["ev_101"]))

    def test_waitlist_positions_follow_join_order_not_entry_ids(self):
        """Entry IDs sort in reverse join order, yet positions follow the joins."""
        descending_ids = [UUID(int=n) for n in (3, 2, 1)]
        with patch.object(services.uuid, "uuid4", side_effect=descending_ids):
            alice, bob, carol = (self.join("ev_102", user)[0]
                                 for user in ("alice", "bob", "carol"))
        self.assertEqual([alice["position"], bob["position"], carol["position"]],
                         [1, 2, 3])
        self.assertEqual(self.join("ev_102", "alice")[0:2],
                         ({"error": "already_on_waitlist"}, 409))
        self.assertEqual(services.leave_waitlist("ev_102", bob["id"]), (None, 204))
        self.assertEqual(services.leave_waitlist("ev_102", bob["id"])[1], 404)

        self.assertEqual(services.get_waitlist_entries("ev_102"), ({"entries": [
            {**alice, "position": 1}, {**carol, "position": 2},
        ]}, 200))
        self.assertEqual(services.get_waitlist_entry_by_id("ev_102", carol["id"]),
                         ({**carol, "position": 2}, 200))
        self.assertEqual(self.join("ev_101", "erin")[0:2],
                         ({"error": "event_not_full"}, 409))

    def test_failed_unit_of_work_leaves_no_reservation_or_used_id(self):
        commands = PostgresBookingCommands(self.database_url)
        with (self.assertRaisesRegex(RuntimeError, "abort"),
              commands.unit_of_work() as repository):
            event = repository.get_event("ev_103")
            assert event is not None
            reservation = event.book(repository.next_reservation_id(), "x")
            repository.save_reservation(event, reservation)
            raise RuntimeError("abort")

        self.assertEqual(services.get_reservation("res_1"), ({"error": "not_found"}, 404))
        self.assertEqual(services.get_event("ev_103")[0]["seatsLeft"], 2)
        self.assertEqual(self.reserve("ev_103")[0]["id"], "res_1")

    def run_while_locked(self, event_id, rival_write, command):
        """Run command while a rival transaction holds the event row lock.

        The rival locks the row, makes rival_write (if any), and commits once
        the command has waited half a second. Returns the command's result.
        """
        with psycopg.connect(self.database_url) as rival:
            rival.execute("SELECT 1 FROM events WHERE id = %s FOR UPDATE", (event_id,))
            if rival_write:
                rival.execute(rival_write)
            with ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(command)
                with self.assertRaises(TimeoutError):
                    pending.result(timeout=0.5)
                rival.commit()
                return pending.result(timeout=5)

    def test_booking_that_waited_for_the_lock_counts_the_rival_booking(self):
        self.reserve("ev_103")
        result = self.run_while_locked(
            "ev_103",
            "INSERT INTO reservations VALUES ('res_rival', 'ev_103', 'confirmed', 'x')",
            lambda: self.reserve("ev_103"),
        )
        self.assertEqual(result[:2], ({"error": "sold_out"}, 409))
        self.assertEqual(services.get_event("ev_103")[0]["seatsLeft"], 0)

    def test_waitlist_join_that_waited_for_the_lock_sees_the_rival_entry(self):
        result = self.run_while_locked(
            "ev_102",
            "INSERT INTO waitlist_entries (id, event_id, user_id, created_at)"
            " VALUES ('wl_rival', 'ev_102', 'gina', 'x')",
            lambda: self.join("ev_102", "gina"),
        )
        self.assertEqual(result[:2], ({"error": "already_on_waitlist"}, 409))

    def test_hold_that_waited_for_the_lock_judges_expiry_after_the_wait(self):
        """Rival Holds that expire during the lock wait no longer fill ev_103.

        The rival's two Holds expire 0.3 seconds after it locks the row and it
        commits after more than 0.5 seconds. A clock read when the waiting
        transaction began would still see them live and answer sold_out.
        """
        expiring = ("INSERT INTO holds (id, event_id, created_at, expires_at)"
                    " SELECT 'hold_rival_' || n, 'ev_103', clock_timestamp(),"
                    " clock_timestamp() + interval '0.3 seconds'"
                    " FROM generate_series(1, 2) AS n")
        result = self.run_while_locked(
            "ev_103", expiring,
            lambda: hold_services.create_hold("ev_103", str(uuid4()), b"", None, {}),
        )
        self.assertEqual((result[0].get("status"), result[1:]), ("held", (201, False)))

    def test_waitlist_leave_waits_for_the_event_row_lock(self):
        entry = self.join("ev_102", "frank")[0]
        result = self.run_while_locked(
            "ev_102", None, lambda: services.leave_waitlist("ev_102", entry["id"])
        )
        self.assertEqual(result, (None, 204))


if __name__ == "__main__":
    unittest.main()
