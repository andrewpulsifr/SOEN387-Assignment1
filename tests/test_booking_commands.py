"""Unit tests for the in-memory booking block in memory_booking.py.

Checks that MemoryBookingCommands saves a booking and advances the ID
counter, and that an exception inside a block restores reservations, the
counter, the waitlist and Holds. Also checks that services.create_reservation opens
one unit_of_work() on an injected adapter and propagates failures. These
in-memory tests do not show PostgreSQL locking or persistence.

Run from the project root:
    python3 -m unittest discover -s tests -p 'test_booking*.py' -v
"""

import unittest
from contextlib import contextmanager
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import services
import storage
from memory_booking import MemoryBookingCommands


class BookingCommandTests(unittest.TestCase):
    """Booking block behavior of MemoryBookingCommands and its use by the service."""

    def setUp(self):
        """Each test owns an isolated capacity-two Event and empty booking state."""
        self.events = {"ev_103": {"id": "ev_103", "title": "Film"}}
        self.reservations: dict[str, dict] = {}
        self.next_number = {"n": 1}
        self.commands = MemoryBookingCommands(
            self.events, {"ev_103": 2}, self.reservations, self.next_number
        )
        self.original_outcomes = deepcopy(storage.IDEMPOTENCY)
        storage.IDEMPOTENCY.clear()

    def tearDown(self):
        """Restore the idempotency store that the service-level check fills."""
        storage.IDEMPOTENCY.clear()
        storage.IDEMPOTENCY.update(self.original_outcomes)

    def seats_left(self):
        """Read ev_103's availability through the adapter."""
        with self.commands.snapshot() as repository:
            event = repository.get_event("ev_103")
            assert event is not None
            return event.seats_left

    def test_unit_of_work_persists_domain_booking_and_rehydrates_occupancy(self):
        """Success saves the Reservation and ID counter; a reload has one seat fewer."""
        with self.commands.unit_of_work() as repository:
            event = repository.get_event("ev_103")
            assert event is not None
            reservation = event.book(
                repository.next_reservation_id(), "2026-11-08T20:00:00Z"
            )
            repository.save_reservation(event, reservation)

        with self.commands.unit_of_work() as repository:
            reloaded = repository.get_event("ev_103")
            assert reloaded is not None
            self.assertEqual(reloaded.seats_left, 1)
            self.assertIsNone(repository.get_event("ev_999"))
            self.assertEqual(repository.next_reservation_id(), "res_2")
        self.assertEqual(self.reservations, {"res_1": {
            "id": "res_1", "eventId": "ev_103", "status": "confirmed",
            "createdAt": "2026-11-08T20:00:00Z",
        }})

    def test_failed_unit_of_work_restores_booking_availability_and_identity(self):
        """A RuntimeError after save_reservation restores reservations and the ID counter.

        A reservation that existed before the block is left untouched.
        """
        existing = {"id": "res_90", "eventId": "ev_101", "status": "confirmed",
                    "createdAt": "2026-11-08T19:00:00Z"}
        self.reservations["res_90"] = existing.copy()
        self.next_number["n"] = 1
        with self.assertRaisesRegex(RuntimeError, "abort"):
            with self.commands.unit_of_work() as repository:
                event = repository.get_event("ev_103")
                assert event is not None
                reservation = event.book(
                    repository.next_reservation_id(), "2026-11-08T20:00:00Z"
                )
                repository.save_reservation(event, reservation)
                raise RuntimeError("abort")

        with self.commands.unit_of_work() as repository:
            reloaded = repository.get_event("ev_103")
            assert reloaded is not None
            self.assertEqual(reloaded.seats_left, 2)
            self.assertEqual(repository.next_reservation_id(), "res_1")
        self.assertEqual(self.reservations, {"res_90": existing})

    def test_failed_unit_of_work_restores_the_waitlist(self):
        """A RuntimeError after a join and a leave puts both waitlist changes back."""
        kept = {"id": "wl_1", "eventId": "ev_103", "userId": "kept", "createdAt": "t1"}
        self.commands.add_waitlist_entry(kept)
        with (self.assertRaisesRegex(RuntimeError, "abort"),
              self.commands.unit_of_work() as repository):
            repository.add_waitlist_entry(
                {"id": "wl_2", "eventId": "ev_103", "userId": "new", "createdAt": "t2"}
            )
            repository.remove_waitlist_entry("ev_103", "wl_1")
            raise RuntimeError("abort")

        with self.commands.snapshot() as repository:
            self.assertEqual(repository.waitlist_entries("ev_103"), [kept])

    def test_failed_unit_of_work_removes_its_hold_and_frees_the_seat(self):
        """A RuntimeError after save_hold leaves no Hold and ev_103's seat free."""
        with (self.assertRaisesRegex(RuntimeError, "abort"),
              self.commands.unit_of_work() as repository):
            event = repository.get_event("ev_103")
            assert event is not None
            repository.save_hold(event.hold("hold_a", timedelta(seconds=90)))
            self.assertEqual(repository.get_event("ev_103").seats_left, 1)
            raise RuntimeError("abort")

        with self.commands.snapshot() as repository:
            self.assertIsNone(repository.get_hold("hold_a"))
        self.assertEqual(self.seats_left(), 2)

    def test_service_uses_unit_of_work_only_adapter(self):
        """The service opens one booking block and saves the reservation."""
        adapter = SimpleNamespace(
            unit_of_work=Mock(wraps=self.commands.unit_of_work)
        )
        result = services.create_reservation(
            "ev_103", "unit-of-work", b"", None, commands=adapter
        )
        self.assertEqual(result[1:], (201, False))
        self.assertEqual(self.reservations, {"res_1": result[0]})
        self.assertEqual(self.seats_left(), 1)
        adapter.unit_of_work.assert_called_once_with()

    def test_unit_of_work_failure_propagates_and_preserves_state(self):
        """An error from unit_of_work() propagates unchanged and preserves booking state.

        The error is raised when opening the block, on entering it, and after
        the service has written its booking. Each case leaves reservations,
        the ID counter and availability as they were.
        """
        for phase in ("open", "enter", "execute"):
            with self.subTest(phase=phase):
                failure = RuntimeError(phase)

                @contextmanager
                def failing_work():
                    """Abort before entry or after the service writes its booking."""
                    with self.commands.unit_of_work() as repository:
                        if phase == "enter":
                            raise failure
                        yield repository
                        raise failure

                unit_of_work = (Mock(side_effect=failure) if phase == "open"
                                else Mock(wraps=failing_work))
                adapter = SimpleNamespace(unit_of_work=unit_of_work)
                with self.assertRaises(RuntimeError) as caught:
                    services.create_reservation(
                        "ev_103", "failure-" + phase, b"", None, commands=adapter
                    )
                self.assertIs(caught.exception, failure)
                unit_of_work.assert_called_once_with()
                self.assertEqual(self.reservations, {})
                self.assertEqual(self.next_number, {"n": 1})
                self.assertEqual(self.seats_left(), 2)


if __name__ == "__main__":
    unittest.main()
