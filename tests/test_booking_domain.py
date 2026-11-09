"""Unit tests for the Event capacity rule in booking_domain.py.

Checks that an Event derives seats_left from Capacity and confirmed occupancy,
rejects an overbooked starting state, and books a seat with the ID and time
the caller supplies until none remain. The Event has no storage, so these
tests say nothing about persistence or database locking.

Run from the project root:
    python3 -m unittest discover -s tests -p 'test_booking*.py' -v
"""

import unittest

from booking_domain import Event, SoldOut


class EventTests(unittest.TestCase):
    def test_rejects_overbooked_initial_occupancy(self):
        """Constructing an Event with confirmed occupancy above Capacity raises ValueError."""
        with self.assertRaises(ValueError):
            Event("ev_103", capacity=2, confirmed_occupancy=3)

    def test_availability_is_capacity_minus_confirmed_occupancy(self):
        """Empty, partially occupied and full Events expose computed availability."""
        for capacity, occupancy, available in [(42, 0, 42), (2, 1, 1), (0, 0, 0)]:
            with self.subTest(capacity=capacity, occupancy=occupancy):
                event = Event("ev_103", capacity, occupancy)
                self.assertEqual(event.seats_left, available)

    def test_direct_booking_uses_one_seat_and_supplied_identity_and_time(self):
        """The last seat creates one confirmed Reservation; another booking fails."""
        event = Event("ev_103", capacity=2, confirmed_occupancy=1)
        reservation = event.book("res_7", "2026-11-08T20:00:00Z")
        self.assertEqual(reservation, {
            "id": "res_7", "eventId": "ev_103", "status": "confirmed",
            "createdAt": "2026-11-08T20:00:00Z",
        })
        self.assertEqual(event.seats_left, 0)
        with self.assertRaises(SoldOut):
            event.book("res_8", "2026-11-08T20:01:00Z")
        self.assertEqual(event.seats_left, 0)


if __name__ == "__main__":
    unittest.main()
