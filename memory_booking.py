"""In-memory implementation of the booking repository protocols.

It wraps the process-local dicts in storage.py so booking code can use them
through BookingRepository. A booking block is atomic: if it raises, the dicts
are restored to their state at entry. State lives only in this process, with no
database locking or durable replay. The waitlist, queue and event reads still
use the dicts directly.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy

from booking_domain import Event
from booking_repository import BookingRepository


class MemoryBookingCommands:
    """Booking commands and repository in one object, backed by the storage dicts.

    Callers open a block with unit_of_work() and get this same
    object back as the repository.
    """

    def __init__(self, events: dict[str, dict], capacities: dict[str, int],
                 reservations: dict[str, dict], next_number: dict[str, int]):
        """Keep references to the shared dicts. Nothing is copied, so storage.py stays the only owner of the data."""
        self.events = events
        self.capacities = capacities
        self.reservations = reservations
        self.next_number = next_number

    @contextmanager
    def unit_of_work(self) -> Iterator[BookingRepository]:
        """Open an atomic booking block and yield this object as the repository.

        On success the changes stay. On any exception (including
        KeyboardInterrupt) events, reservations and the ID counter go back to
        their state at entry, then the exception is re-raised. Capacities are
        never written during a booking, so they are not snapshotted.
        """
        events_before = deepcopy(self.events)
        reservations_before = deepcopy(self.reservations)
        next_before = self.next_number.copy()
        try:
            yield self
        except BaseException:
            self.events.clear()
            self.events.update(events_before)
            self.reservations.clear()
            self.reservations.update(reservations_before)
            self.next_number.clear()
            self.next_number.update(next_before)
            raise

    def get_event(self, event_id: str) -> Event | None:
        """Return the Event, counting occupancy from stored reservations rather than seatsLeft, or None if it does not exist."""
        if event_id not in self.events:
            return None
        confirmed = sum(
            reservation["eventId"] == event_id
            for reservation in self.reservations.values()
        )
        return Event(event_id, self.capacities[event_id], confirmed)

    def next_reservation_id(self) -> str:
        """Return the next res_N ID and advance the counter.

        A rollback also rewinds the counter, so a failed booking does not use up an ID.
        """
        reservation_id = f"res_{self.next_number['n']}"
        self.next_number["n"] += 1
        return reservation_id

    def save_reservation(self, event: Event, reservation: dict[str, str]) -> None:
        """Store a copy of the reservation and write the event's new seatsLeft for event readers."""
        self.reservations[reservation["id"]] = reservation.copy()
        self.events[event.id]["seatsLeft"] = event.seats_left
