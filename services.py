import uuid
from datetime import datetime, timezone

import idempotency
import storage
import validation
from booking_domain import SoldOut
from booking_repository import BookingUnitOfWorkProvider


def list_events(bookable):
    events = list(storage.EVENTS.values())
    if bookable == "true":
        events = [e for e in events if e["seatsLeft"] > 0]
    return {"events": events}, 200


def get_event(event_id):
    event = storage.EVENTS.get(event_id)
    if event is None:
        return {"error": "not_found"}, 404
    return event, 200


def create_reservation(event_id, idempotency_key, body_bytes, content_type,
                       *, commands: BookingUnitOfWorkProvider | None = None):
    """Create a reservation, replaying the stored response for a repeated request.

    `commands` is the booking adapter, defaulting to storage.BOOKING_COMMANDS.
    """
    commands = storage.BOOKING_COMMANDS if commands is None else commands
    return idempotency.execute_idempotently(
        idempotency_key, body_bytes, f"POST /events/{event_id}/reservations",
        lambda: _perform_reservation(event_id, body_bytes, content_type, commands)
    )


def _perform_reservation(event_id, body_bytes, content_type,
                         commands: BookingUnitOfWorkProvider):
    """Validate the request, then book one seat in a single booking block.

    Returns 422 for an invalid request, 404 for a missing event, 409 when sold
    out and 201 with the reservation otherwise. createdAt is a fixed timestamp,
    so responses keep their shape. The capacity check and the seat write share
    one block so a database adapter can lock and commit them together.
    """
    # Check the request against the OpenAPI contract
    try:
        validation.validate_request(
            "POST",
            "/events/{eventId}/reservations",
            {"eventId": event_id},
            body_bytes=body_bytes,
            content_type=content_type,
        )
    except validation.InvalidRequest:
        return {"error": "invalid_request"}, 422

    try:
        with commands.unit_of_work() as repository:
            event = repository.get_event(event_id)
            if event is None:
                return {"error": "not_found"}, 404
            reservation = event.book(
                repository.next_reservation_id(), "2026-09-12T19:00:00Z"
            )
            repository.save_reservation(event, reservation)
        return reservation, 201
    except SoldOut:
        return {"error": "sold_out"}, 409


def get_reservation(reservation_id):
    reservation = storage.RESERVATIONS.get(reservation_id)
    if reservation is None:
        return {"error": "not_found"}, 404
    return reservation, 200


# Prevent repeated requests from creating duplicate waitlist entries
def join_waitlist(event_id, idempotency_key, body_bytes, content_type):
    return idempotency.execute_idempotently(
        idempotency_key, body_bytes, f"POST /events/{event_id}/waitlist",
        lambda: _perform_waitlist_join(
            event_id, body_bytes, content_type
        )
    )


def _perform_waitlist_join(event_id, body_bytes, content_type):
    # Check the request against the OpenAPI contract
    try:
        body = validation.validate_request(
            "POST",
            "/events/{eventId}/waitlist",
            {"eventId": event_id},
            body_bytes=body_bytes,
            content_type=content_type,
        )
    except validation.InvalidRequest:
        return {"error": "invalid_request"}, 422

    # Check if the event allows joining the waitlist
    event = storage.EVENTS.get(event_id)
    if event is None:
        return {"error": "event_not_found"}, 404
    if event["seatsLeft"] > 0:
        return {"error": "event_not_full"}, 409
    entries = storage.WAITLIST.get(event_id, [])
    if any(entry["userId"] == body["userId"] for entry in entries):
        return {"error": "already_on_waitlist"}, 409

    # Create a new waitlist entry
    entry = {
        "id": f"wl_{uuid.uuid4().hex}",
        "eventId": event_id,
        "userId": body["userId"],
        "createdAt": datetime.now(timezone.utc).isoformat(),
    }

    # Store the entry and return a copy with its position
    entries = storage.WAITLIST.setdefault(event_id, [])
    entries.append(entry)
    return {**entry, "position": len(entries)}, 201


def get_waitlist_entries(event_id):
    if event_id not in storage.EVENTS:
        return {"error": "event_not_found"}, 404

    # Add each entry's current position in the waitlist
    entries = storage.WAITLIST.get(event_id, [])
    positioned_entries = [
        {**entry, "position": position}
        for position, entry in enumerate(entries, start=1)
    ]
    return {"entries": positioned_entries}, 200

def get_waitlist_entry_by_id(event_id, entry_id):
    if event_id not in storage.EVENTS:
        return {"error": "event_not_found"}, 404

    ## Find the entry and its current position in the waitlist
    entries = storage.WAITLIST.get(event_id, [])
    for position, entry in enumerate(entries, start=1):
        if entry["id"] == entry_id:
            return {**entry, "position": position}, 200

    return {"error": "entry_not_found"}, 404

def leave_waitlist(event_id, entry_id):
    if event_id not in storage.EVENTS:
        return {"error": "event_not_found"}, 404

    # No Idempotency-Key is needed for leaving the waitlist.
    # Deleting is state-idempotent: repeating it cannot remove the entry twice.
    entries = storage.WAITLIST.get(event_id, [])
    for i, entry in enumerate(entries):
        if entry["id"] == entry_id:
            del entries[i]
            return None, 204

    return {"error": "entry_not_found"}, 404
