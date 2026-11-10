"""Durable replay of keyed POST outcomes on PostgreSQL, through the services.

Checks that outcomes outlive process memory, that stored errors replay after
the state changes, that conflicting reuse leaves the binding alone, that a
failed command keeps neither its writes nor its key, that same-key requests
book once, the 24-hour boundary on an injected clock, and the retry rules for
SQLSTATEs raised by a test trigger. Replay over HTTP across a killed process
is in test_postgres_startup.py.

Run from the project root with ENCORE_TEST_DATABASE_URL set:
    python3 -m unittest discover -s tests -p 'test_postgres_replay.py' -v
"""

import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier
from unittest.mock import patch

import psycopg
from postgres_support import ServicesOnPostgresTestCase, requires_postgres

import idempotency
import services
import storage


@requires_postgres
class PostgresReplayTests(ServicesOnPostgresTestCase):
    """Keyed reservation and waitlist requests on a migrated, seeded database."""

    def seats_left(self, event_id):
        """Read the event's current seatsLeft through the service."""
        return services.get_event(event_id)[0]["seatsLeft"]

    def test_replay_survives_loss_of_process_memory(self):
        """A retry after process memory is lost replays instead of booking again."""
        first = services.create_reservation("ev_103", "lost-reply", b"{}",
                                            "application/json")
        storage.IDEMPOTENCY.clear()

        retry = services.create_reservation("ev_103", "lost-reply", b"{}",
                                            "application/json")

        self.assertEqual(first[1:], (201, False))
        self.assertEqual(retry, (first[0], 201, True))
        self.assertEqual(self.seats_left("ev_103"), 1)

    def test_stored_errors_replay_after_the_state_changes(self):
        """sold_out and already_on_waitlist replay after a fresh request would succeed."""
        sold_out = services.create_reservation("ev_102", "sold-out", b"", None)
        alice = b'{"userId":"alice"}'
        services.join_waitlist("ev_102", "alice-1", alice, "application/json")
        duplicate = services.join_waitlist("ev_102", "alice-2", alice,
                                           "application/json")

        self.execute("UPDATE events SET capacity = 5 WHERE id = 'ev_102'")
        self.execute("DELETE FROM waitlist_entries")

        self.assertEqual(sold_out, ({"error": "sold_out"}, 409, False))
        self.assertEqual(duplicate, ({"error": "already_on_waitlist"}, 409, False))
        self.assertEqual(services.create_reservation("ev_102", "sold-out", b"", None),
                         ({"error": "sold_out"}, 409, True))
        self.assertEqual(services.join_waitlist("ev_102", "alice-2", alice,
                                                "application/json"),
                         ({"error": "already_on_waitlist"}, 409, True))
        self.assertEqual(self.seats_left("ev_102"), 5)

    def test_conflicting_bindings_are_rejected_without_replacing_the_original(self):
        """Changed bytes, another event and another route return 422 key reuse."""
        first = services.create_reservation("ev_101", "bound", b"{}",
                                            "application/json")
        reuse = ({"error": "idempotency_key_reuse"}, 422, False)
        for name, attempt in [
            ("equivalent JSON, other bytes", lambda: services.create_reservation(
                "ev_101", "bound", b"{ }", "application/json")),
            ("absent body", lambda: services.create_reservation(
                "ev_101", "bound", b"", None)),
            ("other event", lambda: services.create_reservation(
                "ev_103", "bound", b"{}", "application/json")),
            ("other route", lambda: services.join_waitlist(
                "ev_101", "bound", b"{}", "application/json")),
        ]:
            with self.subTest(name):
                self.assertEqual(attempt(), reuse)

        self.assertEqual(services.create_reservation("ev_101", "bound", b"{}",
                                                     "application/json"),
                         (first[0], 201, True))
        self.assertEqual([self.seats_left(event) for event in ("ev_101", "ev_103")],
                         [41, 2])
        self.assertEqual(services.get_waitlist_entries("ev_101"), ({"entries": []}, 200))

    def test_failed_command_rolls_back_its_booking_and_key_claim(self):
        """A command that raises after booking leaves neither the seat nor the key."""
        def book_then_fail(repository):
            event = repository.get_event("ev_103")
            reservation = event.book(repository.next_reservation_id(), "x")
            repository.save_reservation(event, reservation)
            raise RuntimeError("abort after booking")

        with self.assertRaisesRegex(RuntimeError, "abort after booking"):
            idempotency.execute_idempotently(
                "rolled-back", b"", "POST /events/ev_103/reservations", book_then_fail
            )

        self.assertEqual(self.seats_left("ev_103"), 2)
        retry = services.create_reservation("ev_103", "rolled-back", b"", None)
        self.assertEqual((retry[0]["id"], retry[1:]), ("res_1", (201, False)))

    def test_concurrent_same_key_requests_book_one_seat(self):
        """Eight simultaneous same-key requests: one booking, seven replays of it."""
        barrier = Barrier(8)

        def click(_):
            barrier.wait(timeout=5)
            return services.create_reservation("ev_101", "eight-clicks", b"{}",
                                               "application/json")

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(click, range(8)))

        self.assertEqual(sorted(replayed for _, _, replayed in results),
                         [False] + [True] * 7)
        self.assertEqual({(body["id"], status) for body, status, _ in results},
                         {("res_1", 201)})
        self.assertEqual(self.seats_left("ev_101"), 41)

    def test_binding_expires_24_hours_after_completion(self):
        """One second before expiry it replays; at equality another binding may start."""
        completed = datetime(2026, 11, 9, 12, tzinfo=timezone.utc)
        with patch.object(idempotency, "utc_now", return_value=completed):
            first = services.create_reservation("ev_103", "day-key", b"", None)
        with patch.object(idempotency, "utc_now",
                          return_value=completed + timedelta(hours=24, seconds=-1)):
            before = services.create_reservation("ev_103", "day-key", b"", None)
            reuse = services.create_reservation("ev_101", "day-key", b"", None)
        with patch.object(idempotency, "utc_now",
                          return_value=completed + timedelta(hours=24)):
            at_expiry = services.create_reservation("ev_101", "day-key", b"", None)
            replay = services.create_reservation("ev_101", "day-key", b"", None)

        self.assertEqual(before, (first[0], 201, True))
        self.assertEqual(reuse, ({"error": "idempotency_key_reuse"}, 422, False))
        self.assertEqual((at_expiry[0]["eventId"], at_expiry[1:]),
                         ("ev_101", (201, False)))
        self.assertEqual(replay, (at_expiry[0], 201, True))
        self.assertEqual([self.seats_left(event) for event in ("ev_101", "ev_103")],
                         [41, 1])

    def test_recognized_aborts_retry_the_whole_command(self):
        """Serialization failures and deadlocks rerun the command up to the bound."""
        attempts = idempotency.MAX_COMMAND_ATTEMPTS
        for sqlstate in ("40001", "40P01"):
            with self.subTest(sqlstate=sqlstate):
                self.execute("DROP TRIGGER IF EXISTS fail_write ON reservations;"
                             " DROP FUNCTION IF EXISTS fail_write;"
                             " DROP SEQUENCE IF EXISTS write_attempts;")
                self.fail_writes(sqlstate, attempts - 1)
                key = f"retried-{sqlstate}"

                result = services.create_reservation("ev_101", key, b"", None)

                self.assertEqual((result[0]["status"], result[1:]),
                                 ("confirmed", (201, False)))
                self.assertEqual(self.write_attempts(), attempts)
                self.assertEqual(services.create_reservation("ev_101", key, b"", None),
                                 (result[0], 201, True))

    def test_exhausted_retries_return_uncached_503(self):
        """After the last recognized abort: 503, no booking and no stored binding."""
        self.fail_writes("40001", 100)

        result = services.create_reservation("ev_103", "exhausted", b"", None)

        self.assertEqual(result, ({"error": "service_unavailable"}, 503, False))
        self.assertEqual(self.write_attempts(), idempotency.MAX_COMMAND_ATTEMPTS)
        self.execute("DROP TRIGGER fail_write ON reservations")
        retry = services.create_reservation("ev_103", "exhausted", b"", None)
        self.assertEqual((retry[0]["id"], retry[1:]), ("res_1", (201, False)))

    def test_other_database_errors_propagate_without_retry(self):
        """Unrecognized SQLSTATEs, including completion-unknown, are raised once."""
        self.fail_writes("40003", 100)
        with self.assertRaises(psycopg.errors.StatementCompletionUnknown):
            services.create_reservation("ev_103", "uncertain", b"", None)
        self.assertEqual(self.write_attempts(), 1)
        self.assertEqual(self.seats_left("ev_103"), 2)


if __name__ == "__main__":
    unittest.main()
