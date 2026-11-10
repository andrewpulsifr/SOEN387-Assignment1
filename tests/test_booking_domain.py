"""Unit tests for the Event capacity rule and Hold lifecycle in booking_domain.py.

Checks Hold status and countdown just before, at and after the deadline, with
fractional seconds rounded up; that only live Holds take seats; that hold()
and book() share one availability rule; that overbooked starting state is
rejected; and that the module imports nothing. The rules have no storage or
clock, so these tests say nothing about persistence or database locking.

Run from the project root:
    python3 -m unittest discover -s tests -p 'test_booking*.py' -v
"""

import ast
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import booking_domain
from booking_domain import Event, Hold, SoldOut

CREATED = datetime(2026, 11, 9, 20, 0, tzinfo=timezone.utc)
DEADLINE = CREATED + timedelta(seconds=90)


def held(hold_id="hold_a", **state):
    """Return a ev_103 Hold created at CREATED that expires at DEADLINE."""
    return Hold(hold_id, "ev_103", CREATED, DEADLINE, **state)


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


class DomainModuleTests(unittest.TestCase):
    def test_domain_module_has_no_imports(self):
        """The rules module imports nothing, so no framework or storage code can enter it."""
        source = Path(booking_domain.__file__).read_text(encoding="utf-8")
        imports = [ast.unparse(node) for node in ast.walk(ast.parse(source))
                   if isinstance(node, (ast.Import, ast.ImportFrom))]
        self.assertEqual(imports, [])


if __name__ == "__main__":
    unittest.main()
