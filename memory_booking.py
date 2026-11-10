"""In-memory implementation of the booking repository protocols.

It wraps the process-local dicts in storage.py, including the stored replay
outcomes, and is the test double for the PostgreSQL adapter. One lock lets a
single block run at a time across all events and keys, and a command block
that raises restores the dicts to their state at entry. State lives only in
this process and is lost on restart. It never raises TransactionAborted.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from threading import RLock
from typing import Any

from booking_domain import Event
from booking_repository import BookingRepository


class MemoryBookingCommands:
    """Block provider and repository in one object, backed by the storage dicts.

    unit_of_work() and snapshot() both yield this same object as the repository.
    """

    def __init__(self, events: dict[str, dict], capacities: dict[str, int],
                 reservations: dict[str, dict], next_number: dict[str, int],
                 waitlist: dict[str, list[dict]] | None = None,
                 outcomes: dict[str, dict] | None = None):
        """Keep references to the dicts without copying them.

        waitlist and outcomes (idempotency key -> stored outcome) default to
        new dicts.
        """
        self.events = events
        self.capacities = capacities
        self.reservations = reservations
        self.next_number = next_number
        self.waitlist = {} if waitlist is None else waitlist
        self.outcomes = {} if outcomes is None else outcomes
        self._lock = RLock()

    @contextmanager
    def unit_of_work(self) -> Iterator[BookingRepository]:
        """Open an atomic command block and yield this object as the repository.

        On success the changes stay. On any exception (including
        KeyboardInterrupt) reservations, the ID counter, the waitlist and the
        stored outcomes go back to their state at entry, then the exception is
        re-raised. Events and Capacities are never written by a command, so
        they are not copied.
        """
        with self._lock:
            reservations_before = deepcopy(self.reservations)
            next_before = self.next_number.copy()
            waitlist_before = deepcopy(self.waitlist)
            outcomes_before = deepcopy(self.outcomes)
            try:
                yield self
            except BaseException:
                self.reservations.clear()
                self.reservations.update(reservations_before)
                self.next_number.clear()
                self.next_number.update(next_before)
                self.waitlist.clear()
                self.waitlist.update(waitlist_before)
                self.outcomes.clear()
                self.outcomes.update(outcomes_before)
                raise

    @contextmanager
    def snapshot(self) -> Iterator[BookingRepository]:
        """Yield this object for reads while no other block can run."""
        with self._lock:
            yield self

    def get_event(self, event_id: str) -> Event | None:
        """Return the Event with occupancy counted from stored reservations, or None."""
        if event_id not in self.events:
            return None
        confirmed = sum(
            reservation["eventId"] == event_id
            for reservation in self.reservations.values()
        )
        return Event(event_id, self.capacities[event_id], confirmed,
                     title=self.events[event_id]["title"])

    def get_reservation(self, reservation_id: str) -> dict[str, str] | None:
        """Return a copy of the stored reservation, or None."""
        reservation = self.reservations.get(reservation_id)
        return None if reservation is None else reservation.copy()

    def list_events(self) -> list[Event]:
        """Return every Event, ordered by ID."""
        return [event for event_id in sorted(self.events)
                if (event := self.get_event(event_id)) is not None]

    def waitlist_entries(self, event_id: str) -> list[dict[str, str]]:
        """Return copies of the event's entries in join order."""
        return [entry.copy() for entry in self.waitlist.get(event_id, [])]

    def add_waitlist_entry(self, entry: dict[str, str]) -> None:
        """Append a copy of the entry to its event's list."""
        self.waitlist.setdefault(entry["eventId"], []).append(entry.copy())

    def remove_waitlist_entry(self, event_id: str, entry_id: str) -> bool:
        """Remove the entry from the event's list and return whether it existed."""
        entries = self.waitlist.get(event_id, [])
        for index, entry in enumerate(entries):
            if entry["id"] == entry_id:
                del entries[index]
                return True
        return False

    def next_reservation_id(self) -> str:
        """Return the next res_N ID and advance the counter; a rollback rewinds it."""
        reservation_id = f"res_{self.next_number['n']}"
        self.next_number["n"] += 1
        return reservation_id

    def save_reservation(self, event: Event, reservation: dict[str, str]) -> None:
        """Store a copy of the reservation."""
        self.reservations[reservation["id"]] = reservation.copy()

    def claim_key(self, key: str, request_target: str,
                  request_hash: str) -> dict[str, Any] | None:
        """Return a copy of the key's stored outcome, or None if it has none.

        The block's lock already keeps every other command out, so a free key
        needs no placeholder record.
        """
        return deepcopy(self.outcomes.get(key))

    def save_outcome(self, key: str, outcome: dict[str, Any]) -> None:
        """Store a copy of the outcome under the key."""
        self.outcomes[key] = deepcopy(outcome)
