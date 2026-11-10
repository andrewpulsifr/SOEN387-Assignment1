"""Unit tests for the Event capacity rule and Hold lifecycle in booking_domain.py.

Checks Hold status and countdown just before, at and after the deadline, with
fractional seconds rounded up; that only live Holds take seats; that hold()
and book() share one availability rule; that confirm() moves a live Hold's
seat to a Reservation and rejects expired, released, confirmed and foreign
Holds; that release() frees only a live Hold's seat and rejects confirmed
and foreign Holds; that overbooked starting state is rejected; and that the
module imports nothing. The rules have no storage or clock, so these tests
say nothing about persistence or database locking.

Run from the project root:
    python3 -m unittest discover -s tests -p 'test_booking*.py' -v
"""

import ast
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import booking_domain
from booking_domain import (Event, Hold, HoldConfirmed, HoldExpired, HoldReleased,
                            SoldOut)

CREATED = datetime(2026, 11, 9, 20, 0, tzinfo=timezone.utc)
DEADLINE = CREATED + timedelta(seconds=90)


def held(hold_id="hold_a", **state):
    """Return a ev_103 Hold created at CREATED that expires at DEADLINE."""
    return Hold(hold_id, "ev_103", CREATED, DEADLINE, **state)


def ev_103_at(now, holds=None):
    """Return ev_103 at Capacity 2 with one confirmed seat, observed at now.

    holds defaults to Hold hold_a alone, which fills the Event.
    """
    if holds is None:
        holds = [held("hold_a")]
    return Event("ev_103", capacity=2, confirmed_occupancy=1, holds=holds, now=now)


class HoldTests(unittest.TestCase):
    def test_hold_is_live_strictly_before_its_deadline_and_expired_from_it(self):
        """Just before the deadline it is held; at equality and after it is expired."""
        for now, status, seconds in [
            (CREATED, "held", 90),
            (DEADLINE - timedelta(microseconds=1), "held", 1),
            (DEADLINE, "expired", 0),
            (DEADLINE + timedelta(hours=1), "expired", 0),
        ]:
            with self.subTest(now=now):
                self.assertEqual(held().status_at(now), status)
                self.assertEqual(held().seconds_remaining_at(now), seconds)

    def test_live_countdown_rounds_fractional_seconds_up(self):
        """89.5, 1.000001 and 0.2 seconds left report 90, 2 and 1."""
        for remaining, expected in [(timedelta(seconds=89.5), 90),
                                    (timedelta(seconds=1, microseconds=1), 2),
                                    (timedelta(seconds=0.2), 1),
                                    (timedelta(seconds=30), 30)]:
            with self.subTest(remaining=remaining):
                self.assertEqual(held().seconds_remaining_at(DEADLINE - remaining),
                                 expected)

    def test_terminal_holds_keep_their_state_and_report_zero_seconds(self):
        """Confirmed and released Holds stay so before and after the old deadline."""
        for state, status in [({"reservation_id": "res_4"}, "confirmed"),
                              ({"released": True}, "released")]:
            for now in (CREATED, DEADLINE + timedelta(seconds=1)):
                with self.subTest(status=status, now=now):
                    self.assertEqual(held(**state).status_at(now), status)
                    self.assertEqual(held(**state).seconds_remaining_at(now), 0)

    def test_rejects_a_deadline_that_is_not_after_creation(self):
        """A Hold must expire after it is created."""
        with self.assertRaises(ValueError):
            Hold("hold_a", "ev_103", CREATED, CREATED)


class EventTests(unittest.TestCase):
    def test_rejects_overbooked_initial_occupancy(self):
        """Confirmed occupancy plus live Holds above Capacity raises ValueError."""
        for confirmed, holds in [(3, []), (1, [held("hold_a"), held("hold_b")])]:
            with (self.subTest(confirmed=confirmed, holds=len(holds)),
                  self.assertRaises(ValueError)):
                Event("ev_103", capacity=2, confirmed_occupancy=confirmed,
                      holds=holds, now=CREATED)

    def test_availability_is_capacity_minus_confirmed_occupancy(self):
        """Empty, partially occupied and full Events expose computed availability."""
        for capacity, occupancy, available in [(42, 0, 42), (2, 1, 1), (0, 0, 0)]:
            with self.subTest(capacity=capacity, occupancy=occupancy):
                event = Event("ev_103", capacity, occupancy, now=CREATED)
                self.assertEqual(event.seats_left, available)

    def test_only_live_holds_occupy_seats(self):
        """Expired, released and confirmed Holds leave their seat to the rule."""
        holds = [held("hold_live"), held("hold_released", released=True),
                 held("hold_confirmed", reservation_id="res_1"),
                 Hold("hold_old", "ev_103", CREATED - timedelta(minutes=5),
                      CREATED)]
        event = Event("ev_103", capacity=4, confirmed_occupancy=1, holds=holds,
                      now=CREATED)
        self.assertEqual(event.seats_left, 2)
        at_deadline = Event("ev_103", capacity=4, confirmed_occupancy=1,
                            holds=holds, now=DEADLINE)
        self.assertEqual(at_deadline.seats_left, 3)

    def test_hold_takes_one_seat_with_a_fixed_deadline_until_none_remain(self):
        """Holds start at the observation time and last the supplied TTL."""
        event = Event("ev_103", capacity=2, holds=[held("hold_a")], now=CREATED)
        hold = event.hold("hold_b", timedelta(seconds=90))
        self.assertEqual((hold.id, hold.event_id, hold.created_at, hold.expires_at),
                         ("hold_b", "ev_103", CREATED, DEADLINE))
        self.assertEqual(hold.status_at(CREATED), "held")
        self.assertEqual(event.seats_left, 0)
        with self.assertRaises(SoldOut):
            event.hold("hold_c", timedelta(seconds=90))
        with self.assertRaises(SoldOut):
            event.book("res_1", "2026-11-08T20:00:00Z")

    def test_confirmed_reservations_alone_can_fill_the_event_for_holds(self):
        """With every seat confirmed, hold() raises SoldOut and adds no Hold."""
        event = Event("ev_103", capacity=2, confirmed_occupancy=2, now=CREATED)
        with self.assertRaises(SoldOut):
            event.hold("hold_a", timedelta(seconds=90))
        self.assertEqual(event.seats_left, 0)

    def test_direct_booking_uses_one_seat_and_supplied_identity_and_time(self):
        """The last seat creates one confirmed Reservation; another booking fails."""
        event = Event("ev_103", capacity=2, confirmed_occupancy=1, now=CREATED)
        reservation = event.book("res_7", "2026-11-08T20:00:00Z")
        self.assertEqual(reservation, {
            "id": "res_7", "eventId": "ev_103", "status": "confirmed",
            "createdAt": "2026-11-08T20:00:00Z",
        })
        self.assertEqual(event.seats_left, 0)
        with self.assertRaises(SoldOut):
            event.book("res_8", "2026-11-08T20:01:00Z")
        self.assertEqual(event.seats_left, 0)


class ConfirmTests(unittest.TestCase):
    def test_confirming_a_live_hold_replaces_its_seat_with_a_linked_reservation(self):
        """At full allocation, confirmation succeeds and seats_left stays 0."""
        event = ev_103_at(DEADLINE - timedelta(microseconds=1))
        hold = held("hold_a")
        reservation = event.confirm(hold, "res_9", "2026-11-08T20:00:00Z")
        self.assertEqual(reservation, {
            "id": "res_9", "eventId": "ev_103", "status": "confirmed",
            "createdAt": "2026-11-08T20:00:00Z",
        })
        self.assertEqual(hold.reservation_id, "res_9")
        self.assertEqual(hold.status_at(DEADLINE + timedelta(hours=1)), "confirmed")
        self.assertEqual(event.seats_left, 0)

    def test_holds_that_are_not_live_are_rejected_without_change(self):
        """Expired at and after the deadline, or released, raise; the free seat stays free.

        The released Hold is absent from the Event, as when storage loads only
        Holds that are neither confirmed nor released.
        """
        for now, state, error, loaded in [
            (DEADLINE, {}, HoldExpired, [held("hold_a")]),
            (DEADLINE + timedelta(seconds=1), {}, HoldExpired, [held("hold_a")]),
            (CREATED, {"released": True}, HoldReleased, []),
        ]:
            with self.subTest(now=now, error=error.__name__):
                event = ev_103_at(now, loaded)
                hold = held("hold_a", **state)
                with self.assertRaises(error):
                    event.confirm(hold, "res_9", "2026-11-08T20:00:00Z")
                self.assertIsNone(hold.reservation_id)
                self.assertEqual(event.seats_left, 1)

    def test_rejects_a_confirmed_hold_or_one_this_event_does_not_hold(self):
        """None can take a second Reservation or change this Event's seats."""
        event = ev_103_at(CREATED)
        for hold, link in [(held("hold_a", reservation_id="res_4"), "res_4"),
                           (Hold("hold_a", "ev_101", CREATED, DEADLINE), None),
                           (held("hold_unknown"), None)]:
            with self.subTest(event_id=hold.event_id, link=link):
                with self.assertRaises(ValueError):
                    event.confirm(hold, "res_9", "2026-11-08T20:00:00Z")
                self.assertEqual(hold.reservation_id, link)
                self.assertEqual(event.seats_left, 0)


class ReleaseTests(unittest.TestCase):
    def test_releasing_a_live_hold_frees_its_seat_for_good(self):
        """Just before the deadline the release frees one seat and reports that time."""
        just_before = DEADLINE - timedelta(microseconds=1)
        event = ev_103_at(just_before)
        hold = held("hold_a")
        self.assertEqual(event.release(hold), just_before)
        self.assertTrue(hold.released)
        self.assertEqual(hold.status_at(DEADLINE + timedelta(hours=1)), "released")
        self.assertEqual(event.seats_left, 1)

    def test_released_and_expired_holds_are_left_as_they_are(self):
        """A repeat release, or one at or after the deadline, changes nothing.

        The released Hold is absent from the Event, as when storage loads only
        Holds that are neither confirmed nor released.
        """
        for now, state, status, loaded in [
            (CREATED, {"released": True}, "released", []),
            (DEADLINE, {}, "expired", [held("hold_a")]),
            (DEADLINE + timedelta(seconds=1), {}, "expired", [held("hold_a")]),
        ]:
            with self.subTest(now=now, status=status):
                event = ev_103_at(now, loaded)
                hold = held("hold_a", **state)
                self.assertIsNone(event.release(hold))
                self.assertEqual(hold.status_at(now), status)
                self.assertEqual(event.seats_left, 1)

    def test_rejects_a_confirmed_hold_or_one_this_event_does_not_hold(self):
        """A confirmed Hold keeps its Reservation; none of these frees a seat."""
        event = ev_103_at(CREATED)
        for hold, error in [(held("hold_a", reservation_id="res_4"), HoldConfirmed),
                            (Hold("hold_a", "ev_101", CREATED, DEADLINE), ValueError),
                            (held("hold_unknown"), ValueError)]:
            with self.subTest(hold=hold.id, event_id=hold.event_id, error=error.__name__):
                link = hold.reservation_id
                with self.assertRaises(error):
                    event.release(hold)
                self.assertEqual((hold.reservation_id, hold.released), (link, False))
                self.assertEqual(event.seats_left, 0)


class DomainModuleTests(unittest.TestCase):
    def test_domain_module_has_no_imports(self):
        """The rules module imports nothing, so no framework or storage code can enter it."""
        source = Path(booking_domain.__file__).read_text(encoding="utf-8")
        imports = [ast.unparse(node) for node in ast.walk(ast.parse(source))
                   if isinstance(node, (ast.Import, ast.ImportFrom))]
        self.assertEqual(imports, [])


if __name__ == "__main__":
    unittest.main()
