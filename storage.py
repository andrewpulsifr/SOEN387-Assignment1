"""Process memory and the adapter the services use.

The dicts hold the fresh fixture and the state of the in-memory adapter,
including its idempotency outcomes, which a restart clears. The app started
with python3 app.py stores all of this in PostgreSQL instead.
"""

from booking_domain import Hold
from booking_repository import BookingUnitOfWorkProvider
from memory_booking import MemoryBookingCommands

EVENTS: dict[str, dict] = {
    "ev_101": {"id": "ev_101", "title": "Jazz Ensemble: Fall Concert"},
    "ev_102": {"id": "ev_102", "title": "Improv Night"},
    "ev_103": {"id": "ev_103", "title": "Film Society: Friday Screening"},
}
RESERVATIONS: dict[str, dict] = {}  # id -> reservation
IDEMPOTENCY: dict[str, dict] = {}   # key -> {"request_target": ..., "request_hash": ...,
                          #          "status": ..., "body": ..., "completed_at": ...}
WAITLIST: dict[str, list[dict]] = {}  # event_id -> entries in join order
HOLDS: dict[str, Hold] = {}  # id -> Hold
NEXT = {"n": 1}

# Explicit fresh Capacity, matching contracts/fixtures/fresh-events.json.
# Zero-capacity ev_102 has no fabricated historical Reservations.
# seatsLeft is never stored: readers compute it from Capacity, RESERVATIONS
# and the live HOLDS.
CAPACITIES = {"ev_101": 42, "ev_102": 0, "ev_103": 2}

# The adapter the services use. It starts on the in-memory dicts above, which
# tests use directly; app.main() replaces it with the PostgreSQL adapter.
BOOKING_COMMANDS: BookingUnitOfWorkProvider = MemoryBookingCommands(
    EVENTS, CAPACITIES, RESERVATIONS, NEXT, WAITLIST, IDEMPOTENCY, HOLDS
)
