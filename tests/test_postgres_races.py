"""Simultaneous Hold and booking requests over HTTP, against app.py on PostgreSQL.

Each test starts app.py on a freshly seeded database, sends its requests at
the same moment with run_together(), and accounts for every response. It then
reads the Holds and Reservations stored for ev_103 (Capacity 2) and checks
over HTTP that Reservations plus live Holds fill Capacity exactly. Holds last
300 seconds, longer than a run, so expiry cannot hide an oversell. A passing
run covers only the order its requests happened to run in.

Responses and final states go to the "encore.races" logger, which
tests/run_races.py prints; ordinary test runs show nothing.

Run from the project root with ENCORE_TEST_DATABASE_URL set:
    python3 -m unittest discover -s tests -p 'test_postgres_races.py' -v
"""

import logging
import unittest
from datetime import timedelta
from functools import partial
from uuid import uuid4

from conformance import post_with_headers, send
from hold_support import CREATE, create, lifetime, run_together, seats_left, validate
from postgres_support import AppOnPostgresTestCase, requires_postgres

CAPACITY = 2  # ev_103's Capacity in contracts/fixtures/fresh-events.json
HOLD_TTL_SECONDS = 300
SOLD_OUT = (409, {"error": "sold_out"})

log = logging.getLogger("encore.races")


def outcome(response):
    """Describe a keyed POST response as its status and resource ID or error code."""
    status, body, headers = response
    replay = " (replay)" if headers.get("Idempotency-Replayed") == "true" else ""
    return f"{status} {body.get('id') or body.get('error')}{replay}"


def hold_request(key):
    """Return a (label, request) pair that asks for a Hold on ev_103 with this key."""
    return f"POST /events/ev_103/holds key {key[:8]}", partial(create, "ev_103", key)


@requires_postgres
class RaceHttpTests(AppOnPostgresTestCase):
    """Races for ev_103's seats, each on its own app and database."""

    def racing_app(self):
        """Start app.py on freshly seeded data with 300-second Holds."""
        return self.running_app("--seed-fresh",
                                env={"ENCORE_HOLD_TTL_SECONDS": str(HOLD_TTL_SECONDS)})

    def race(self, labelled_requests):
        """Send the requests together and log each response; return them in order.

        labelled_requests is a list of (label, request) pairs, where each
        request is a function of no arguments.
        """
        responses = run_together([request for _, request in labelled_requests])
        for (label, _), response in zip(labelled_requests, responses):
            log.info("%s -> %s", label, outcome(response))
        return responses

    def stored_ids(self, statement):
        """Return the first column of every row the query finds, sorted."""
        return sorted(row[0] for row in self.query(statement))

    def final_state(self):
        """Return and log ev_103's seatsLeft, stored Reservation IDs, and each pending Hold's status.

        A pending Hold is stored and neither confirmed nor released; GET says
        whether it is still live. Call this while the app is running.
        """
        reservations = self.stored_ids(
            "SELECT id FROM reservations WHERE event_id = 'ev_103'")
        pending = self.stored_ids(
            "SELECT id FROM holds WHERE event_id = 'ev_103'"
            " AND reservation_id IS NULL AND released_at IS NULL")
        statuses = {hold_id: send("GET", f"/holds/{hold_id}")[1]["status"]
                    for hold_id in pending}
        seats = seats_left("ev_103")
        log.info("final ev_103 (Capacity %s): seatsLeft %s; stored Reservations %s;"
                 " pending Holds %s", CAPACITY, seats, reservations or "none",
                 ", ".join(f"{hold_id} {status}" for hold_id, status in statuses.items()))
        return seats, reservations, statuses

    def assert_fills_capacity(self, final, reservations, holds):
        """Assert ev_103 stores exactly these Reservations and live Holds, and no other.

        Together they fill Capacity, so seatsLeft is 0 and nothing oversold.
        """
        seats, stored_reservations, statuses = final
        self.assertEqual(stored_reservations, sorted(reservations))
        self.assertEqual(statuses, {hold_id: "held" for hold_id in holds})
        self.assertEqual(len(stored_reservations) + len(statuses), CAPACITY)
        self.assertEqual(seats, 0)

    def hold_first_seat(self):
        """Hold one of ev_103's two seats with a new key; return the receipt."""
        receipt = create("ev_103", str(uuid4()))[1]
        log.info("setup: Hold %s takes one of ev_103's two seats", receipt["id"])
        return receipt

    def test_five_distinct_keys_racing_for_two_seats_get_two_holds(self):
        """Two 201s with distinct Hold IDs, three 409 sold_out, and no oversell."""
        with self.racing_app():
            responses = self.race([hold_request(str(uuid4())) for _ in range(5)])
            # Checked before the final reads: an oversold Event cannot be read.
            self.assertEqual(sorted(status for status, _, _ in responses),
                             [201, 201, 409, 409, 409], responses)
            final = self.final_state()

        holds = [body for status, body, _ in responses if status == 201]
        for status, body, _ in responses:
            validate(CREATE, "post", status, body)
            if status == 409:
                self.assertEqual((status, body), SOLD_OUT)
        for hold in holds:
            self.assertEqual(lifetime(hold), timedelta(seconds=HOLD_TTL_SECONDS))
        hold_ids = [hold["id"] for hold in holds]
        self.assertEqual(len(set(hold_ids)), 2)
        self.assert_fills_capacity(final, [], hold_ids)

    def test_one_key_sent_twice_at_once_for_the_last_seat_creates_one_hold(self):
        """Both get the same 201 receipt, one marked as a replay, and one Hold is stored."""
        key = str(uuid4())
        with self.racing_app():
            first_seat = self.hold_first_seat()
            pair = self.race([hold_request(key)] * 2)
            self.assertEqual([status for status, _, _ in pair], [201, 201], pair)
            final = self.final_state()

        (_, receipt, _), (_, other, _) = pair
        validate(CREATE, "post", 201, receipt)
        self.assertEqual(other, receipt)
        replayed = [headers.get("Idempotency-Replayed") == "true" for _, _, headers in pair]
        self.assertEqual(sorted(replayed), [False, True])
        self.assert_fills_capacity(final, [], [first_seat["id"], receipt["id"]])

    def test_a_booking_and_a_hold_racing_for_the_last_seat_allocate_it_once(self):
        """One 201 and one 409 sold_out, whichever path runs first, and one seat taken."""
        booking_key = str(uuid4())
        with self.racing_app():
            first_seat = self.hold_first_seat()
            booking, hold = self.race([
                (f"POST /events/ev_103/reservations key {booking_key[:8]}",
                 partial(post_with_headers, "/events/ev_103/reservations", b"{}",
                         booking_key)),
                hold_request(str(uuid4())),
            ])
            self.assertEqual(sorted([booking[0], hold[0]]), [201, 409], (booking, hold))
            final = self.final_state()

        validate("/events/{eventId}/reservations", "post", booking[0], booking[1])
        validate(CREATE, "post", hold[0], hold[1])
        loser = booking if booking[0] == 409 else hold
        self.assertEqual(loser[:2], SOLD_OUT)
        reservations = [booking[1]["id"]] if booking[0] == 201 else []
        holds = [first_seat["id"]] + ([hold[1]["id"]] if hold[0] == 201 else [])
        self.assert_fills_capacity(final, reservations, holds)


if __name__ == "__main__":
    unittest.main()
