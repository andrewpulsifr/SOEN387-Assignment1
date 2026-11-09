from memory_booking import MemoryBookingCommands

EVENTS: dict[str, dict] = {
    "ev_101": {"id": "ev_101", "title": "Jazz Ensemble: Fall Concert", "seatsLeft": 42},
    "ev_102": {"id": "ev_102", "title": "Improv Night", "seatsLeft": 0},
    "ev_103": {"id": "ev_103", "title": "Film Society: Friday Screening", "seatsLeft": 2},
}
RESERVATIONS: dict[str, dict] = {}  # id -> reservation
IDEMPOTENCY: dict[str, dict] = {}   # key -> {"request_target": ..., "request_hash": ...,
                          #          "status": ..., "body": ..., "stored_at": ...}
WAITLIST: dict[str, list[dict]] = {}  # event_id -> entries in join order
NEXT = {"n": 1}

# Explicit fresh Capacity, matching contracts/fixtures/fresh-events.json.
# Zero-capacity ev_102 has no fabricated historical Reservations.
# Capacity is listed per event because seatsLeft in EVENTS is derived from it
# and from the reservations, and is published for readers. Booking reads
# Capacity from here and never uses the stored seatsLeft as its source.
CAPACITIES = {"ev_101": 42, "ev_102": 0, "ev_103": 2}

# The one place the booking adapter is tied to the shared dicts above.
# Callers get the adapter from here instead of importing the dicts themselves.
BOOKING_COMMANDS = MemoryBookingCommands(EVENTS, CAPACITIES, RESERVATIONS, NEXT)
