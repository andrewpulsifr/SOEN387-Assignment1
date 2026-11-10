"""Hold release over HTTP, against app.py on PostgreSQL.

Each test starts app.py and uses the conformance helpers, so no handler code
is imported. Checks cover an empty 204 that frees exactly one seat, repeats
that free none, a released Hold's view and confirmation, expired Holds that
stay expired, a confirmed Hold's 409 before and after its deadline, coded
errors that change nothing, and abort retries ending in an uncached 503. A
race between release and confirmation is in test_postgres_races.py.

Run from the project root with ENCORE_TEST_DATABASE_URL set:
    python3 -m unittest discover -s tests -p 'test_postgres_release.py' -v
"""

import unittest
from uuid import uuid4

from conformance import get
from hold_support import (
    confirm,
    create,
    release,
    seats_left,
    validate,
    wait_until_not_held,
)
from postgres_support import AppOnPostgresTestCase, requires_postgres

import idempotency

RELEASE = "/holds/{holdId}"


@requires_postgres
class ReleaseHttpTests(AppOnPostgresTestCase):
    def test_release_frees_one_seat_and_repeats_free_none(self):
        """At full allocation: 204 frees one seat; repeats, with or without a key, free none."""
        key = str(uuid4())
        with self.running_app("--seed-fresh"):
            receipt = create("ev_103", key)[1]
            create("ev_103", str(uuid4()))
            seats_before = seats_left("ev_103")
            first = release(receipt["id"])
            seats_after = seats_left("ev_103")
            repeats = [release(receipt["id"]),
                       release(receipt["id"], b"", {"Idempotency-Key": str(uuid4()),
                                                    "Content-Type": "application/json"})]
            current = get(f"/holds/{receipt['id']}")
            late_confirm = confirm(receipt["id"])
            replay = create("ev_103", key)
            after_replay = get(f"/holds/{receipt['id']}")
            refill = [create("ev_103", str(uuid4()))[:2] for _ in range(2)]

        self.assertEqual((seats_before, seats_after), (0, 1))
        self.assertEqual(first, (204, None))
        self.assertEqual(repeats, [(204, None)] * 2)
        validate(RELEASE, "get", 200, current[1])
        self.assertEqual(current, (200, {**receipt, "status": "released",
                                         "secondsRemaining": 0}))
        self.assertEqual(late_confirm, (409, {"error": "hold_released"}))
        validate("/holds/{holdId}/confirm", "post", 409, late_confirm[1])
        self.assertEqual(replay[:2], (201, receipt))
        self.assertEqual(replay[2]["Idempotency-Replayed"], "true")
        self.assertEqual(after_replay, current)
        # Exactly one seat came back: the first new Hold takes it, the second sells out.
        self.assertEqual(refill[0][0], 201, refill[0][1])
        self.assertEqual(refill[1], (409, {"error": "sold_out"}))

    def test_expired_holds_stay_expired_and_confirmed_holds_keep_their_reservation(self):
        """With a 2-second TTL: expired DELETE is 204 and relabels nothing; confirmed is 409."""
        key = str(uuid4())
        with self.running_app("--seed-fresh", env={"ENCORE_HOLD_TTL_SECONDS": "2"}):
            kept = create("ev_103", str(uuid4()))[1]
            lapsed = create("ev_103", key)[1]
            reservation = confirm(kept["id"])[1]
            refused_live = release(kept["id"])
            # kept was created first with the same TTL, so its deadline has
            # also passed once lapsed reads as expired.
            expired_view = wait_until_not_held(lapsed["id"])
            seats_before = seats_left("ev_103")
            expired_releases = [release(lapsed["id"]), release(lapsed["id"])]
            refused_late = release(kept["id"])
            views = (get(f"/holds/{lapsed['id']}")[1], get(f"/holds/{kept['id']}")[1],
                     get(f"/reservations/{reservation['id']}"))
            seats_after = seats_left("ev_103")
            replay = create("ev_103", key)[:2]

        refusal = {"error": "already_confirmed", "reservationId": reservation["id"]}
        self.assertEqual((refused_live, refused_late), ((409, refusal), (409, refusal)))
        validate(RELEASE, "delete", 409, refused_late[1])
        self.assertEqual(expired_releases, [(204, None)] * 2)
        self.assertEqual(views, (
            expired_view,
            {**kept, "status": "confirmed", "secondsRemaining": 0,
             "reservationId": reservation["id"]},
            (200, reservation)))
        self.assertEqual(expired_view["status"], "expired")
        # One confirmed Reservation on Capacity 2, before and after every DELETE.
        self.assertEqual((seats_before, seats_after), (1, 1))
        self.assertEqual(replay, (201, lapsed))

    def test_unknown_or_malformed_releases_return_coded_errors_without_change(self):
        """An unknown ID gives 404; a malformed ID, query or body gives 422 first."""
        unknown = "hold_" + "0" * 32
        json_type = {"Content-Type": "application/json"}
        with self.running_app("--seed-fresh"):
            receipt = create("ev_103", str(uuid4()))[1]
            responses = [
                ("unknown Hold", release(unknown), 404, "hold_not_found"),
                ("malformed ID", release("hold_1"), 422, "invalid_request"),
                ("query parameter", release(receipt["id"], query="?force=true"),
                 422, "invalid_request"),
                ("empty object body", release(receipt["id"], b"{}", json_type),
                 422, "invalid_request"),
                ("body for an unknown ID", release(unknown, b"{}", json_type),
                 422, "invalid_request"),
            ]
            current = get(f"/holds/{receipt['id']}")[1]
            seats = seats_left("ev_103")

        for name, response, status, code in responses:
            with self.subTest(name):
                self.assertEqual(response, (status, {"error": code}))
                validate(RELEASE, "delete", status, response[1])
        self.assertEqual((current["status"], seats), ("held", 1))

    def test_aborted_release_retries_then_returns_uncached_503(self):
        """After the last abort: 503 and a still-held Hold; releasing again frees the seat.

        Hold UPDATEs fail with SQLSTATE 40001 for 2 * MAX_COMMAND_ATTEMPTS - 1
        tries, so the second request aborts on every attempt but its last.
        """
        attempts = idempotency.MAX_COMMAND_ATTEMPTS
        with self.running_app("--seed-fresh"):
            hold_id = create("ev_103", str(uuid4()))[1]["id"]
            self.fail_writes("40001", 2 * attempts - 1, on="UPDATE ON holds")
            exhausted = release(hold_id)
            after_503 = (get(f"/holds/{hold_id}")[1]["status"], seats_left("ev_103"),
                         self.write_attempts())
            retried = release(hold_id)
            after_retry = (get(f"/holds/{hold_id}")[1]["status"], seats_left("ev_103"))

        self.assertEqual(exhausted, (503, {"error": "service_unavailable"}))
        validate(RELEASE, "delete", 503, exhausted[1])
        self.assertEqual(after_503, ("held", 1, attempts))
        self.assertEqual(retried, (204, None))
        self.assertEqual(after_retry, ("released", 2))
        self.assertEqual(self.write_attempts(), 2 * attempts)


if __name__ == "__main__":
    unittest.main()
