"""Hold confirmation over HTTP, against app.py on PostgreSQL.

Each test starts app.py and uses the conformance helpers, so no handler code
is imported. Checks cover the first 201 and repeat 200 with one linked
Reservation at full allocation, the Hold and Reservation views, coded errors
that change nothing, expiry against a confirmation that outlasts its
deadline, and abort retries ending in an uncached 503. Confirming a released
Hold is checked only in the domain tests, because release has no route yet.

Run from the project root with ENCORE_TEST_DATABASE_URL set:
    python3 -m unittest discover -s tests -p 'test_postgres_confirm.py' -v
"""

import unittest
from uuid import uuid4

from conformance import get, post, send
from hold_support import create, seats_left, validate, wait_until_not_held
from postgres_support import AppOnPostgresTestCase, requires_postgres

import idempotency

CONFIRM = "/holds/{holdId}/confirm"


def confirm(hold_id, body=None, headers=None, query=""):
    """POST a confirmation with exactly these bytes; return (status, body)."""
    return send("POST", f"/holds/{hold_id}/confirm{query}", body, headers)[:2]


@requires_postgres
class ConfirmHttpTests(AppOnPostgresTestCase):
    def test_first_confirm_creates_one_linked_reservation_and_repeats_return_it(self):
        """At full allocation: 201 then 200 with one Reservation; no seat changes hands."""
        key = str(uuid4())
        with self.running_app("--seed-fresh"):
            receipt = create("ev_103", key)[1]
            other = create("ev_103", str(uuid4()))[1]
            replay_before = create("ev_103", key)[:2]
            first = confirm(receipt["id"])
            repeats = [confirm(receipt["id"]),
                       confirm(receipt["id"], b"", {"Idempotency-Key": str(uuid4()),
                                                    "Content-Type": "application/json"})]
            current = get(f"/holds/{receipt['id']}")
            reservation = get(f"/reservations/{first[1]['id']}")
            seats = seats_left("ev_103")
            blocked = [create("ev_103", str(uuid4()))[:2],
                       post("/events/ev_103/reservations", b"{}", str(uuid4()))]
            replay_after = create("ev_103", key)
            second = confirm(other["id"])
            seats_after_both = seats_left("ev_103")

        self.assertEqual(first[0], 201, first[1])
        validate(CONFIRM, "post", 201, first[1])
        self.assertEqual(first[1]["eventId"], "ev_103")
        self.assertEqual(repeats, [(200, first[1])] * 2)
        validate(CONFIRM, "post", 200, repeats[0][1])
        validate("/holds/{holdId}", "get", 200, current[1])
        self.assertEqual(current, (200, {**receipt, "status": "confirmed",
                                         "secondsRemaining": 0,
                                         "reservationId": first[1]["id"]}))
        self.assertEqual(reservation, (200, first[1]))
        self.assertEqual((seats, seats_after_both), (0, 0))
        self.assertEqual(blocked, [(409, {"error": "sold_out"})] * 2)
        self.assertEqual(replay_before, (201, receipt))
        self.assertEqual(replay_after[:2], (201, receipt))
        self.assertEqual(replay_after[2]["Idempotency-Replayed"], "true")
        # Numbering starts at res_1 and only a new Reservation takes a number,
        # so res_2 here shows the repeats created none.
        self.assertEqual((first[1]["id"], second), ("res_1", (201, {
            "id": "res_2", "eventId": "ev_103", "status": "confirmed",
            "createdAt": first[1]["createdAt"]})))

    def test_unknown_or_malformed_confirms_return_coded_errors_without_change(self):
        """An unknown ID gives 404; a malformed ID, query or body gives 422 first."""
        unknown = "hold_" + "0" * 32
        json_type = {"Content-Type": "application/json"}
        with self.running_app("--seed-fresh"):
            receipt = create("ev_103", str(uuid4()))[1]
            responses = [
                ("unknown Hold", confirm(unknown), 404, "hold_not_found"),
                ("malformed ID", confirm("hold_1"), 422, "invalid_request"),
                ("query parameter", confirm(receipt["id"], query="?verbose=true"),
                 422, "invalid_request"),
                ("empty object body", confirm(receipt["id"], b"{}", json_type),
                 422, "invalid_request"),
                ("body for an unknown ID", confirm(unknown, b"{}", json_type),
                 422, "invalid_request"),
            ]
            current = get(f"/holds/{receipt['id']}")[1]
            seats = seats_left("ev_103")
            first = confirm(receipt["id"])

        for name, response, status, code in responses:
            with self.subTest(name):
                self.assertEqual(response, (status, {"error": code}))
                validate(CONFIRM, "post", status, response[1])
        self.assertEqual((current["status"], seats), ("held", 1))
        self.assertEqual((first[0], first[1]["id"]), (201, "res_1"))

    def test_expired_holds_fail_and_confirmation_outlasts_the_deadline(self):
        """With a 2-second TTL, the unconfirmed Hold expires; the confirmed one stays confirmed."""
        key = str(uuid4())
        with self.running_app("--seed-fresh", env={"ENCORE_HOLD_TTL_SECONDS": "2"}):
            kept = create("ev_103", str(uuid4()))[1]
            lapsed = create("ev_103", key)[1]
            first = confirm(kept["id"])
            # kept was created first with the same TTL, so its deadline has
            # also passed once lapsed reads as expired.
            expired_view = wait_until_not_held(lapsed["id"])
            late = confirm(lapsed["id"])
            repeat = confirm(kept["id"])
            kept_view = get(f"/holds/{kept['id']}")[1]
            reservation = get(f"/reservations/{first[1]['id']}")
            seats = seats_left("ev_103")
            replay = create("ev_103", key)[:2]
            booking = post("/events/ev_103/reservations", b"{}", str(uuid4()))

        self.assertEqual(first[0], 201, first[1])
        self.assertEqual(expired_view, {**lapsed, "status": "expired",
                                        "secondsRemaining": 0})
        self.assertEqual(late, (409, {"error": "hold_expired"}))
        validate(CONFIRM, "post", 409, late[1])
        self.assertEqual(repeat, (200, first[1]))
        self.assertEqual(kept_view, {**kept, "status": "confirmed", "secondsRemaining": 0,
                                     "reservationId": first[1]["id"]})
        self.assertEqual(reservation, (200, first[1]))
        self.assertEqual(seats, 1)
        self.assertEqual(replay, (201, lapsed))
        # The rejected confirmation took no reservation number.
        self.assertEqual((booking[0], booking[1]["id"]), (201, "res_2"))

    def test_aborted_confirmation_retries_then_returns_uncached_503(self):
        """After the last abort: 503 and a still-live Hold; confirming again succeeds.

        Reservation INSERTs fail with SQLSTATE 40001 for 2 * MAX_COMMAND_ATTEMPTS
        - 1 tries, so the second request aborts on every attempt but its last.
        """
        attempts = idempotency.MAX_COMMAND_ATTEMPTS
        with self.running_app("--seed-fresh"):
            hold_id = create("ev_101", str(uuid4()))[1]["id"]
            self.fail_reservation_inserts("40001", 2 * attempts - 1)
            exhausted = confirm(hold_id)
            after_503 = (get(f"/holds/{hold_id}")[1]["status"], self.insert_attempts())
            retried = confirm(hold_id)
            link = get(f"/holds/{hold_id}")[1].get("reservationId")

        self.assertEqual(exhausted, (503, {"error": "service_unavailable"}))
        validate(CONFIRM, "post", 503, exhausted[1])
        self.assertEqual(after_503, ("held", attempts))
        self.assertEqual((retried[0], retried[1]["id"], link), (201, "res_1", "res_1"))
        self.assertEqual(self.insert_attempts(), 2 * attempts)


if __name__ == "__main__":
    unittest.main()
