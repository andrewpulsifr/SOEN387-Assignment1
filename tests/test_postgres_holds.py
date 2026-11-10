"""Hold creation and inspection over HTTP, against app.py on PostgreSQL.

Each test starts app.py on its own freshly seeded database and uses the
conformance helpers, so no handler code is imported. Checks cover receipts
and the current view, coded errors, live Holds filling an Event for both
booking paths and the waitlist, and expiry restoring seats without cleanup.

Run from the project root with ENCORE_TEST_DATABASE_URL set:
    python3 -m unittest discover -s tests -p 'test_postgres_holds.py' -v
"""

import time
import unittest
from datetime import datetime, timedelta
from uuid import uuid4

from conformance import get, post, schema_for, send
from jsonschema import Draft202012Validator
from postgres_support import AppOnPostgresTestCase, requires_postgres

CREATE = "/events/{eventId}/holds"
UNKNOWN_HOLD = "/holds/hold_" + "0" * 32


def create(event_id, key, body=None, content_type=None, query=""):
    """POST a Hold request with exactly these bytes; return (status, body, headers)."""
    headers = {} if key is None else {"Idempotency-Key": key}
    if content_type is not None:
        headers["Content-Type"] = content_type
    return send("POST", f"/events/{event_id}/holds{query}", body, headers)


def validate(path, method, status, body):
    """Check a response body against the contract schema for that status."""
    Draft202012Validator(schema_for(path, method, str(status)),
                         format_checker=Draft202012Validator.FORMAT_CHECKER).validate(body)


def lifetime(hold):
    """Return expiresAt minus createdAt for a Hold body."""
    return (datetime.fromisoformat(hold["expiresAt"])
            - datetime.fromisoformat(hold["createdAt"]))


def seats_left(event_id):
    """Read the event's seatsLeft over HTTP."""
    return get(f"/events/{event_id}")[1]["seatsLeft"]


@requires_postgres
class HoldHttpTests(AppOnPostgresTestCase):
    def assert_receipt(self, response, event_id):
        """Assert a 201 Hold receipt for event_id that lasts the default 90 seconds."""
        status, body, _ = response
        self.assertEqual(status, 201, body)
        validate(CREATE, "post", 201, body)
        self.assertEqual((body["eventId"], body["status"]), (event_id, "held"))
        self.assertEqual(lifetime(body), timedelta(seconds=90))
        return body

    def test_create_and_inspect_with_absent_body_or_empty_object(self):
        """Both valid bodies hold a seat; replay repeats the receipt; GET shows the countdown."""
        absent_key, empty_key = str(uuid4()), str(uuid4())
        with self.running_app("--seed-fresh"):
            absent = self.assert_receipt(create("ev_101", absent_key), "ev_101")
            empty = self.assert_receipt(
                create("ev_101", empty_key, b"{}", "application/json; charset=utf-8"),
                "ev_101")
            status, current, _ = send("GET", f"/holds/{absent['id']}")
            replay = create("ev_101", absent_key)
            reuse = [create("ev_101", absent_key, b"{}", "application/json")[:2],
                     create("ev_103", absent_key)[:2],
                     post("/events/ev_101/reservations", b"", absent_key)]
            seats = (seats_left("ev_101"), seats_left("ev_103"))

        self.assertNotEqual(absent["id"], empty["id"])
        self.assertEqual(status, 200, current)
        validate("/holds/{holdId}", "get", 200, current)
        self.assertEqual({k: current[k] for k in absent}, absent)
        self.assertIn(current["secondsRemaining"], range(1, 91))
        self.assertEqual(replay[:2], (201, absent))
        self.assertEqual(replay[2]["Idempotency-Replayed"], "true")
        self.assertEqual(reuse, [(422, {"error": "idempotency_key_reuse"})] * 3)
        self.assertEqual(seats, (40, 2))

    def test_invalid_requests_return_coded_errors_without_holding_a_seat(self):
        """Missing keys, options, bad bodies, queries and unknown IDs follow the contract."""
        json_type = "application/json"
        creations = [
            ("missing key", "ev_103", None, b"{}", json_type, "", 422,
             "idempotency_key_required"),
            ("person field", "ev_103", "k", b'{"userId":"ana"}', json_type, "", 422,
             "invalid_request"),
            ("quantity field", "ev_103", "k", b'{"quantity":1}', json_type, "", 422,
             "invalid_request"),
            ("TTL field", "ev_103", "k", b'{"ttl":5}', json_type, "", 422,
             "invalid_request"),
            ("null", "ev_103", "k", b"null", json_type, "", 422, "invalid_request"),
            ("array", "ev_103", "k", b"[]", json_type, "", 422, "invalid_request"),
            ("scalar", "ev_103", "k", b"1", json_type, "", 422, "invalid_request"),
            ("whitespace", "ev_103", "k", b" ", json_type, "", 422, "invalid_request"),
            ("malformed JSON", "ev_103", "k", b"{", json_type, "", 422,
             "invalid_request"),
            ("text content type", "ev_103", "k", b"{}", "text/plain", "", 422,
             "invalid_request"),
            ("+json content type", "ev_103", "k", b"{}", "application/merge-patch+json",
             "", 422, "invalid_request"),
            ("query parameter", "ev_103", "k", None, None, "?ttl=5", 422,
             "invalid_request"),
            ("malformed event ID", "bad-id", "k", None, None, "", 422, "invalid_request"),
            ("unknown event", "ev_999", "k", None, None, "", 404, "event_not_found"),
        ]
        reads = [
            ("unknown Hold", UNKNOWN_HOLD, None, 404, "hold_not_found"),
            ("malformed Hold ID", "/holds/hold_1", None, 422, "invalid_request"),
            ("query parameter", UNKNOWN_HOLD + "?verbose=true", None, 422,
             "invalid_request"),
            ("nonempty body", UNKNOWN_HOLD, b"{}", 422, "invalid_request"),
        ]
        with self.running_app("--seed-fresh"):
            for name, event_id, key, body, content_type, query, status, code in creations:
                with self.subTest(create=name):
                    key = key and str(uuid4())
                    response = create(event_id, key, body, content_type, query)
                    self.assertEqual(response[:2], (status, {"error": code}))
                    validate(CREATE, "post", status, response[1])
                    if key:
                        replay = create(event_id, key, body, content_type, query)
                        self.assertEqual(replay[:2], response[:2])
                        self.assertEqual(replay[2]["Idempotency-Replayed"], "true")
            for name, path, body, status, code in reads:
                with self.subTest(read=name):
                    response = send("GET", path, body, {"Content-Type": json_type})
                    self.assertEqual(response[:2], (status, {"error": code}))
                    validate("/holds/{holdId}", "get", status, response[1])
            self.assertEqual(seats_left("ev_103"), 2)

    def test_live_holds_fill_the_event_for_both_booking_paths_and_the_waitlist(self):
        """Two live Holds use ev_103's Capacity: holds and bookings sell out, joins open."""
        sold_out_key = str(uuid4())
        with self.running_app("--seed-fresh"):
            for _ in range(2):
                self.assert_receipt(create("ev_103", str(uuid4())), "ev_103")
            sold_out = create("ev_103", sold_out_key)
            booking = post("/events/ev_103/reservations", b"{}", str(uuid4()))
            event = get("/events/ev_103")
            bookable = get("/events?bookable=true")[1]["events"]
            join = post("/events/ev_103/waitlist", b'{"userId":"ana"}', str(uuid4()))
            replay = create("ev_103", sold_out_key)

        self.assertEqual(sold_out[:2], (409, {"error": "sold_out"}))
        validate(CREATE, "post", 409, sold_out[1])
        self.assertEqual(booking, (409, {"error": "sold_out"}))
        self.assertEqual(event[1]["seatsLeft"], 0)
        self.assertNotIn("ev_103", [listed["id"] for listed in bookable])
        self.assertEqual(join[0], 201, join[1])
        self.assertEqual(replay[:2], sold_out[:2])
        self.assertEqual(replay[2]["Idempotency-Replayed"], "true")

    def wait_until_not_held(self, hold_id, timeout=10):
        """Poll GET until the Hold stops being held; return that response body."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status, current, _ = send("GET", f"/holds/{hold_id}")
            self.assertEqual(status, 200, current)
            if current["status"] != "held":
                return current
            time.sleep(0.05)
        self.fail(f"Hold {hold_id} was still held after {timeout} seconds")

    def test_expiry_restores_availability_at_once_and_replay_never_renews(self):
        """With a 2-second TTL, expiry frees ev_103 for every reader and both booking paths."""
        hold_key, sold_out_key = str(uuid4()), str(uuid4())
        with self.running_app("--seed-fresh", env={"ENCORE_HOLD_TTL_SECONDS": "2"}):
            _, receipt, _ = create("ev_103", hold_key)
            create("ev_103", str(uuid4()))
            sold_out = create("ev_103", sold_out_key)
            ana = post("/events/ev_103/waitlist", b'{"userId":"ana"}', str(uuid4()))

            expired = self.wait_until_not_held(receipt["id"])
            seats_after_expiry = seats_left("ev_103")
            bookable = get("/events?bookable=true")[1]["events"]
            bob = post("/events/ev_103/waitlist", b'{"userId":"bob"}', str(uuid4()))
            queue = get("/events/ev_103/waitlist")[1]["entries"]
            replay = create("ev_103", hold_key)
            sold_out_replay = create("ev_103", sold_out_key)
            after_replay = (send("GET", f"/holds/{receipt['id']}")[1],
                            seats_left("ev_103"))
            booking = post("/events/ev_103/reservations", b"{}", str(uuid4()))
            new_hold = create("ev_103", str(uuid4()))
            full_again = create("ev_103", str(uuid4()))

        self.assertEqual(lifetime(receipt), timedelta(seconds=2))
        self.assertEqual((sold_out[0], ana[0]), (409, 201))
        validate("/holds/{holdId}", "get", 200, expired)
        self.assertEqual(expired, {**receipt, "status": "expired", "secondsRemaining": 0})
        self.assertEqual(seats_after_expiry, 2)
        self.assertIn("ev_103", [listed["id"] for listed in bookable])
        self.assertEqual(bob, (409, {"error": "event_not_full"}))
        self.assertEqual([(entry["id"], entry["position"]) for entry in queue],
                         [(ana[1]["id"], 1)])
        self.assertEqual(replay[:2], (201, receipt))
        self.assertEqual(sold_out_replay[:2], (409, {"error": "sold_out"}))
        self.assertEqual(after_replay, (expired, 2))
        self.assertEqual(booking[0], 201, booking[1])
        self.assertEqual(new_hold[0], 201, new_hold[1])
        # One confirmed Reservation and one live Hold now fill ev_103 again.
        self.assertEqual(full_again[:2], (409, {"error": "sold_out"}))


if __name__ == "__main__":
    unittest.main()
