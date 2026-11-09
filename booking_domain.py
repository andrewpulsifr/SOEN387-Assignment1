"""The Event capacity rule for immediate booking.

This module imports nothing, so the rule can run without storage or HTTP code.
It has no Hold lifecycle; an Event here only counts confirmed occupancy against
Capacity.
"""


class SoldOut(ValueError):
    """Raised by Event.book() when confirmed occupancy has reached Capacity."""


class Event:
    """An Event with a fixed Capacity and a count of confirmed occupancy.

    The caller supplies both numbers, so the Event never reads storage. The
    constructor rejects overbooked input (confirmed occupancy above Capacity)
    and negative values. book() takes the reservation ID and timestamp from the
    caller, which keeps the rule free of ID generation and clocks.
    """

    def __init__(self, event_id: str, capacity: int, confirmed_occupancy: int = 0,
                 *, title: str = ""):
        """Construct an Event from explicit Capacity and confirmed occupancy.

        The title is carried for event responses and takes no part in the rule.
        """
        if capacity < 0 or confirmed_occupancy < 0 or confirmed_occupancy > capacity:
            raise ValueError("Confirmed occupancy must be between zero and Capacity")
        self.id = event_id
        self.title = title
        self._capacity = capacity
        self._confirmed_occupancy = confirmed_occupancy

    @property
    def seats_left(self) -> int:
        """Compute available seats from Capacity and confirmed occupancy."""
        return self._capacity - self._confirmed_occupancy

    def book(self, reservation_id: str, created_at: str) -> dict[str, str]:
        """Confirm one seat and return the reservation as a dict.

        Raises SoldOut if no seat remains. On success, confirmed occupancy goes
        up by one on this Event object; saving the result is the caller's job.
        """
        if self.seats_left == 0:
            raise SoldOut("No seat remains")
        self._confirmed_occupancy += 1
        return {
            "id": reservation_id,
            "eventId": self.id,
            "status": "confirmed",
            "createdAt": created_at,
        }
