"""Keyed replay and the waitlist across workflows, over HTTP against app.py on PostgreSQL.

Each test starts app.py on its own seeded database and sends requests through
the conformance helpers, without importing handler code. The Hold suites each
stay within one workflow; these checks span the three keyed POSTs. A key
stays bound to its first route, event and exact body. A waitlist join's replay
keeps its creation position while GET shows the current one. Stored booking
and join outcomes replay unchanged while live Holds fill an event and give
seats back.

Run from the project root with ENCORE_TEST_DATABASE_URL set:
    python3 -m unittest discover -s tests -p 'test_postgres_cross_workflow.py' -v
"""

import unittest
from uuid import uuid4

from conformance import get, send
from hold_support import create, release, seats_left, validate, wait_until_not_held
from postgres_support import AppOnPostgresTestCase, requires_postgres

KEYED_ROUTES = ("reservations", "waitlist", "holds")
KEY_REUSE = (422, {"error": "idempotency_key_reuse"})


def keyed_post(route, event_id, body, key):
    """POST exactly these body bytes (None sends none) to an event's keyed route.

    Returns (status, body, headers).
    """
    headers = {"Idempotency-Key": key}
    if body is not None:
        headers["Content-Type"] = "application/json"
    return send("POST", f"/events/{event_id}/{route}", body, headers)


@requires_postgres
class CrossWorkflowHttpTests(AppOnPostgresTestCase):
    """HTTP checks that span the keyed POST routes, each on its own app and database."""

    def assert_replayed(self, response, original):
        """Assert that response repeats original's status and body as a marked replay."""
        self.assertEqual(response[:2], original[:2])
        self.assertEqual(response[2]["Idempotency-Replayed"], "true")

    def test_a_key_stays_bound_to_its_first_route_and_event(self):
        """The same bytes sent to any other route or event are key reuse; the first replays."""
        first_requests = [("reservations", "ev_101", b"{}"),
                          ("waitlist", "ev_102", b'{"userId":"ana"}'),
                          ("holds", "ev_101", None)]
        results = []
        with self.running_app("--seed-fresh"):
            for route, event_id, body in first_requests:
                key = str(uuid4())
                first = keyed_post(route, event_id, body, key)
                other_targets = [(other, event_id) for other in KEYED_ROUTES
                                 if other != route] + [(route, "ev_103")]
                reuses = [(other, keyed_post(other, other_event, body, key)[:2])
                          for other, other_event in other_targets]
                results.append((route, first, reuses, keyed_post(route, event_id, body, key)))
            seats = (seats_left("ev_101"), seats_left("ev_103"))
            queues = (get("/events/ev_102/waitlist")[1]["entries"],
                      get("/events/ev_103/waitlist")[1]["entries"])

        for route, first, reuses, replay in results:
            with self.subTest(first_route=route):
                self.assertEqual(first[0], 201, first[1])
                for other, response in reuses:
                    self.assertEqual(response, KEY_REUSE, other)
                    validate(f"/events/{{eventId}}/{other}", "post", 422, response[1])
                self.assert_replayed(replay, first)
        # Only the three first requests took effect: one booking and one Hold
        # on ev_101, and ana's entry on ev_102.
        self.assertEqual(seats, (40, 2))
        self.assertEqual([entry["userId"] for entry in queues[0]], ["ana"])
        self.assertEqual(queues[1], [])

    def test_other_body_bytes_are_key_reuse_even_when_the_json_is_equivalent(self):
        """Absent and {} differ, and so do {} and { }; the first request still replays."""
        changes = [("reservations", "ev_101", None, b"{}"),
                   ("reservations", "ev_101", b"{}", None),
                   ("reservations", "ev_101", b"{}", b"{ }"),
                   ("waitlist", "ev_102", b'{"userId":"bob"}', b'{ "userId": "bob" }'),
                   ("holds", "ev_101", b"{}", None),
                   ("holds", "ev_101", b"{}", b"{ }")]
        results = []
        with self.running_app("--seed-fresh"):
            for route, event_id, body, changed_body in changes:
                key = str(uuid4())
                first = keyed_post(route, event_id, body, key)
                reuse = keyed_post(route, event_id, changed_body, key)[:2]
                results.append((route, body, changed_body, first, reuse,
                                keyed_post(route, event_id, body, key)))
            seats = seats_left("ev_101")
            queue = get("/events/ev_102/waitlist")[1]["entries"]

        for route, body, changed_body, first, reuse, replay in results:
            with self.subTest(route=route, first_body=body, retry_body=changed_body):
                self.assertEqual(first[0], 201, first[1])
                self.assertEqual(reuse, KEY_REUSE)
                validate(f"/events/{{eventId}}/{route}", "post", 422, reuse[1])
                self.assert_replayed(replay, first)
        # Three bookings and two Holds on ev_101, and bob's single entry.
        self.assertEqual(seats, 37)
        self.assertEqual([entry["userId"] for entry in queue], ["bob"])

    def test_a_join_replay_keeps_its_creation_position_while_get_shows_the_current_one(self):
        """After ana leaves ev_102, bob's GET says 1, his replay still says 2, and ana stays out."""
        keys = {"ana": str(uuid4()), "bob": str(uuid4())}
        bodies = {user: f'{{"userId":"{user}"}}'.encode() for user in keys}
        with self.running_app("--seed-fresh"):
            ana, bob = (keyed_post("waitlist", "ev_102", bodies[user], keys[user])
                        for user in ("ana", "bob"))
            left = send("DELETE", f"/events/ev_102/waitlist/{ana[1]['id']}")[:2]
            bob_now = get(f"/events/ev_102/waitlist/{bob[1]['id']}")
            replays = [keyed_post("waitlist", "ev_102", bodies[user], keys[user])
                       for user in ("ana", "bob")]
            queue = get("/events/ev_102/waitlist")

        self.assertEqual([(ana[0], ana[1]["position"]), (bob[0], bob[1]["position"])],
                         [(201, 1), (201, 2)])
        self.assertEqual(left, (204, None))
        self.assertEqual(bob_now, (200, {**bob[1], "position": 1}))
        for replay, original in zip(replays, (ana, bob)):
            self.assert_replayed(replay, original)
            validate("/events/{eventId}/waitlist", "post", 201, replay[1])
        # Replaying ana's join returned her receipt without adding her back.
        self.assertEqual(queue, (200, {"entries": [{**bob[1], "position": 1}]}))

    def test_stored_outcomes_replay_unchanged_while_holds_fill_and_free_the_event(self):
        """With a 2-second TTL on ev_103 (Capacity 2), replays ignore the seats taken and returned.

        ana's join is refused while seats remain, and a booking sells out once
        two live Holds fill the event. Releasing one Hold and letting the other
        expire each return a seat, yet both refusals replay unchanged and bob's
        join keeps its 201.
        """
        early_key, sold_out_key, bob_key = (str(uuid4()) for _ in range(3))
        with self.running_app("--seed-fresh", env={"ENCORE_HOLD_TTL_SECONDS": "2"}):
            early_join = keyed_post("waitlist", "ev_103", b'{"userId":"ana"}', early_key)
            released, lapsing = (create("ev_103", str(uuid4()))[1] for _ in range(2))
            sold_out = keyed_post("reservations", "ev_103", b"{}", sold_out_key)
            bob = keyed_post("waitlist", "ev_103", b'{"userId":"bob"}', bob_key)
            seats_while_full = seats_left("ev_103")
            early_replay = keyed_post("waitlist", "ev_103", b'{"userId":"ana"}', early_key)

            released_status = (release(released["id"])[0],
                               get(f"/holds/{released['id']}")[1]["status"])
            after_release = keyed_post("reservations", "ev_103", b"{}", sold_out_key)
            booking = keyed_post("reservations", "ev_103", b"{}", str(uuid4()))

            lapsed_status = wait_until_not_held(lapsing["id"])["status"]
            seats_after_expiry = seats_left("ev_103")
            after_expiry = [keyed_post("reservations", "ev_103", b"{}", sold_out_key),
                            keyed_post("waitlist", "ev_103", b'{"userId":"bob"}', bob_key)]
            queue = get("/events/ev_103/waitlist")[1]["entries"]

        self.assertEqual(early_join[:2], (409, {"error": "event_not_full"}))
        self.assertEqual(sold_out[:2], (409, {"error": "sold_out"}))
        validate("/events/{eventId}/reservations", "post", 409, sold_out[1])
        self.assertEqual((bob[0], bob[1]["position"]), (201, 1))
        self.assertEqual(seats_while_full, 0)
        self.assert_replayed(early_replay, early_join)
        validate("/events/{eventId}/waitlist", "post", 409, early_replay[1])

        # "released", not "expired": the seat came back through the release.
        self.assertEqual(released_status, (204, "released"))
        self.assert_replayed(after_release, sold_out)
        self.assertEqual(booking[0], 201, booking[1])

        # One booking now stands on Capacity 2, so a seat is free again.
        self.assertEqual((lapsed_status, seats_after_expiry), ("expired", 1))
        self.assert_replayed(after_expiry[0], sold_out)
        self.assert_replayed(after_expiry[1], bob)
        validate("/events/{eventId}/waitlist", "post", 201, after_expiry[1][1])
        # bob is still queued once: replaying his join added no second entry.
        self.assertEqual([(entry["id"], entry["position"]) for entry in queue],
                         [(bob[1]["id"], 1)])


if __name__ == "__main__":
    unittest.main()
