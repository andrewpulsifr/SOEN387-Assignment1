"""The storage boundary for events, reservations, waitlists and replay outcomes.

Services ask a repository for domain Events, reservation IDs, stored
reservations, waitlist entries and the stored outcomes of keyed POST requests.
Adapters own the storage details behind those calls; memory_booking.py and
postgres_booking.py implement them.

A Unit of Work is one atomic command block: every read, ID allocation and
write inside it succeeds together, or the block leaves no trace. Once a block
has loaded an event or claimed an idempotency key, no other command on that
event or key runs until the block ends. A snapshot is a read-only block whose
reads all see one consistent state.
"""

from contextlib import AbstractContextManager
from typing import Any, Protocol

from booking_domain import Event


class TransactionAborted(Exception):
    """A unit of work was rolled back by a recognized retryable abort.

    Adapters raise it only when the storage confirms the whole block rolled
    back, so the caller may run the command again from the start.
    """


class BookingRepository(Protocol):
    """The operations a command or snapshot block offers the services."""

    def get_event(self, event_id: str) -> Event | None:
        """Load an Event with its Capacity and confirmed occupancy, or None.

        Inside a unit of work this also claims the event for the rest of the
        block, so the occupancy stays current until the block ends.
        """
        ...

    def next_reservation_id(self) -> str:
        """Allocate the next reservation ID; a rolled-back block returns it."""
        ...

    def save_reservation(self, event: Event, reservation: dict[str, str]) -> None:
        """Store a reservation that event.book() just created."""
        ...

    def get_reservation(self, reservation_id: str) -> dict[str, str] | None:
        """Return a copy of a stored reservation, or None."""
        ...

    def list_events(self) -> list[Event]:
        """Load every Event with its Capacity and confirmed occupancy, ordered by ID."""
        ...

    def waitlist_entries(self, event_id: str) -> list[dict[str, str]]:
        """Return copies of the event's waitlist entries in join order."""
        ...

    def add_waitlist_entry(self, entry: dict[str, str]) -> None:
        """Append the entry to the end of its event's waitlist."""
        ...

    def remove_waitlist_entry(self, event_id: str, entry_id: str) -> bool:
        """Remove the entry from the event's waitlist and return whether it existed."""
        ...

    def claim_key(self, key: str, request_target: str,
                  request_hash: str) -> dict[str, Any] | None:
        """Claim an idempotency key, or return a copy of the outcome stored under it.

        None means the key was free and is now claimed for this request. Either
        way, a same-key command elsewhere waits until this block ends. The
        stored outcome has request_target, request_hash, status, body and
        completed_at; the caller decides whether it has expired.
        """
        ...

    def save_outcome(self, key: str, outcome: dict[str, Any]) -> None:
        """Store a copy of the outcome under the claimed key, replacing any old one."""
        ...


class BookingUnitOfWorkProvider(Protocol):
    """Open command and snapshot blocks and provide their repository."""

    def unit_of_work(self) -> AbstractContextManager[BookingRepository]:
        """Open a command block and yield the repository to use inside it."""
        ...

    def snapshot(self) -> AbstractContextManager[BookingRepository]:
        """Open a read-only block whose reads all see one consistent state."""
        ...
