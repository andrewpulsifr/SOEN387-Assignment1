"""The storage boundary for events, reservations, Holds, waitlists and replay outcomes.

Services ask a repository for domain Events and Holds, reservation IDs, stored
reservations, waitlist entries, the stored outcomes of keyed POST requests and
the current time. Adapters own the storage details and the clock behind those
calls; memory_booking.py and postgres_booking.py implement them.

A Unit of Work is one atomic command block: every read, ID allocation and
write inside it succeeds together, or the block leaves no trace. Once a block
has loaded an event or claimed an idempotency key, no other command on that
event or key runs until the block ends. A snapshot is a read-only block whose
reads all see one consistent state.
"""

from contextlib import AbstractContextManager
from datetime import datetime
from typing import Any, Protocol

from booking_domain import Event, Hold


class TransactionAborted(Exception):
    """A unit of work was rolled back by a recognized retryable abort.

    Adapters raise it only when the storage confirms the whole block rolled
    back, so the caller may run the command again from the start.
    """


class BookingRepository(Protocol):
    """The operations a command or snapshot block offers the services."""

    def get_event(self, event_id: str) -> Event | None:
        """Load an Event with its Capacity, occupancy and pending Holds, or None.

        Pending Holds are those not confirmed or released. The Event is
        observed at now(), so it can tell live Holds from expired ones. Inside
        a unit of work this first claims the event for the rest of the block,
        then reads the clock, so a command that waited for the claim judges
        expiry at the time it actually runs.
        """
        ...

    def now(self) -> datetime:
        """Return the block's current time from the shared clock.

        In a snapshot this is the moment its reads were fixed, so every read
        and the time agree. In a unit of work each call reads the clock again.
        """
        ...

    def save_hold(self, hold: Hold) -> None:
        """Store a Hold that event.hold() just created."""
        ...

    def get_hold(self, hold_id: str) -> Hold | None:
        """Return a copy of a stored Hold, or None."""
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
        """Load every Event like get_event(), all observed at one time, ordered by ID."""
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
