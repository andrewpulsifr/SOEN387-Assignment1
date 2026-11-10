"""Event capacity and the Hold lifecycle, as plain rules.

This module imports nothing, so the rules run without storage, HTTP code or a
clock. Callers pass in every ID and time. Availability is stated once, in
Event.seats_left: Capacity minus confirmed Reservations minus live Holds.
Confirmation moves a live Hold's seat to a Reservation. Releasing a Hold is
not implemented here yet.
"""


class SoldOut(ValueError):
    """Raised by Event.book() and Event.hold() when no seat is available."""


class HoldExpired(ValueError):
    """Raised by Event.confirm() for a Hold whose deadline has been reached."""


class HoldReleased(ValueError):
    """Raised by Event.confirm() for a Hold that was released."""


class Hold:
    """One provisional seat for an Event, with a deadline fixed at creation.

    A Hold is live while it has no linked Reservation, has not been released,
    and the time is strictly before expires_at. At expires_at it has expired;
    nothing has to mark it. Times are timezone-aware datetimes.
    """

    def __init__(self, hold_id: str, event_id: str, created_at, expires_at, *,
                 reservation_id: str | None = None, released: bool = False):
        """Rebuild a Hold; reservation_id marks it confirmed and released marks it released.

        Raises ValueError unless created_at is before expires_at.
        """
        if not created_at < expires_at:
            raise ValueError("A Hold must expire after it is created")
        self.id = hold_id
        self.event_id = event_id
        self.created_at = created_at
        self.expires_at = expires_at
        self.reservation_id = reservation_id
        self.released = released

    def status_at(self, now) -> str:
        """Return held, confirmed, released or expired at time now.

        Confirmation and release outlast the deadline, so only an unended
        Hold can expire.
        """
        if self.reservation_id is not None:
            return "confirmed"
        if self.released:
            return "released"
        if now < self.expires_at:
            return "held"
        return "expired"

    def is_live_at(self, now) -> bool:
        """Return whether the Hold still occupies its seat at now."""
        return self.status_at(now) == "held"

    def seconds_remaining_at(self, now) -> int:
        """Return whole seconds left at now, rounding any fraction up; 0 unless live."""
        if not self.is_live_at(now):
            return 0
        remaining = self.expires_at - now
        partial_second = 1 if remaining.microseconds else 0
        return remaining.days * 86400 + remaining.seconds + partial_second


class Event:
    """An Event observed at one moment: Capacity, confirmed occupancy and its Holds.

    The caller supplies the numbers, the Holds and the observation time `now`,
    so the Event never reads storage or a clock. It judges which Holds are
    live at `now`. book(), hold() and confirm() take their IDs from the caller.
    """

    def __init__(self, event_id: str, capacity: int, confirmed_occupancy: int = 0,
                 *, now, title: str = "", holds=()):
        """Construct an Event observed at now.

        holds may include expired or ended Holds; only live ones take a seat.
        Raises ValueError for negative numbers or for confirmed occupancy plus
        live Holds above Capacity. The title takes no part in the rule.
        """
        self.id = event_id
        self.title = title
        self._capacity = capacity
        self._confirmed_occupancy = confirmed_occupancy
        self._holds = list(holds)
        self._now = now
        if capacity < 0 or confirmed_occupancy < 0 or self.seats_left < 0:
            raise ValueError(
                "Confirmed occupancy plus live Holds must be between zero and Capacity"
            )

    @property
    def seats_left(self) -> int:
        """Compute available seats from Capacity, confirmed occupancy and live Holds."""
        live_holds = sum(hold.is_live_at(self._now) for hold in self._holds)
        return self._capacity - self._confirmed_occupancy - live_holds

    def hold(self, hold_id: str, ttl) -> Hold:
        """Allocate one seat to a new Hold created at now that expires ttl later.

        ttl is a timedelta. Raises SoldOut if no seat is available. The Hold
        counts against this Event at once; saving it is the caller's job.
        """
        if self.seats_left == 0:
            raise SoldOut("No seat remains")
        hold = Hold(hold_id, self.id, self._now, self._now + ttl)
        self._holds.append(hold)
        return hold

    def book(self, reservation_id: str, created_at: str) -> dict[str, str]:
        """Confirm one seat and return the reservation as a dict.

        Raises SoldOut if no seat remains, live Holds included. On success,
        confirmed occupancy goes up by one on this Event object; saving the
        result is the caller's job.
        """
        if self.seats_left == 0:
            raise SoldOut("No seat remains")
        self._confirmed_occupancy += 1
        return self._reservation(reservation_id, created_at)

    def confirm(self, hold: Hold, reservation_id: str, created_at: str) -> dict[str, str]:
        """Replace a live Hold's seat with a confirmed Reservation linked from the Hold.

        hold may be a different copy from the one this Event loaded, such as
        one reread from storage. Raises HoldReleased for a released Hold,
        HoldExpired when now is at or after its deadline, and ValueError for
        a Hold already confirmed, from another Event, or missing from this
        Event's Holds; each leaves both objects unchanged. On success
        hold.reservation_id is set and the
        seat moves to confirmed occupancy, so seats_left stays the same.
        Saving the result is the caller's job.
        """
        if hold.event_id != self.id or hold.reservation_id is not None:
            raise ValueError("Only an unconfirmed Hold of this Event can be confirmed")
        status = hold.status_at(self._now)
        if status == "released":
            raise HoldReleased("The Hold was released")
        if status == "expired":
            raise HoldExpired("The Hold's deadline has been reached")
        if hold.id not in {other.id for other in self._holds}:
            raise ValueError("A live Hold must be one this Event loaded")
        hold.reservation_id = reservation_id
        self._holds = [other for other in self._holds if other.id != hold.id]
        self._confirmed_occupancy += 1
        return self._reservation(reservation_id, created_at)

    def _reservation(self, reservation_id: str, created_at: str) -> dict[str, str]:
        """Return a confirmed reservation of this Event as a dict."""
        return {
            "id": reservation_id,
            "eventId": self.id,
            "status": "confirmed",
            "createdAt": created_at,
        }
