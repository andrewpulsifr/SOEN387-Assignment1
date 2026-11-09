"""The storage boundary for immediate booking.

Domain code asks a repository for an Event, a reservation ID and a place to
save the result. Adapters own the storage details behind those three calls.

A Unit of Work is one atomic booking block: every read, ID allocation and save
inside it succeeds together, or the block leaves no trace. Adapters provide
that guarantee; BookingUnitOfWorkProvider declares how to open a block.
"""

from contextlib import AbstractContextManager
from typing import Protocol

from booking_domain import Event


class BookingRepository(Protocol):
    """The three operations a booking block needs: load, allocate and save."""

    def get_event(self, event_id: str) -> Event | None:
        """Load explicit Capacity and confirmed occupancy for a booking decision."""
        ...

    def next_reservation_id(self) -> str:
        """Generate a reservation ID within the current booking block."""
        ...

    def save_reservation(self, event: Event, reservation: dict[str, str]) -> None:
        """Save the domain booking and publish its shared availability together."""
        ...


class BookingUnitOfWorkProvider(Protocol):
    """Open an atomic booking block and provide its repository to the service."""

    def unit_of_work(self) -> AbstractContextManager[BookingRepository]:
        """Open a booking block and yield the repository to use inside it."""
        ...
