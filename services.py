"""Service functions behind the HTTP routes.

Each returns (body, status); keyed POSTs also return whether the response was
replayed. Commands run in one unit of work from storage.BOOKING_COMMANDS and
reads run in one snapshot, so every response comes from one consistent state.
"""

import uuid
from datetime import datetime, timezone

import idempotency
import storage
import validation
from booking_repository import BookingRepository, BookingUnitOfWorkProvider


def list_events(bookable):
    """List events; bookable == "true" keeps only events with a free seat."""
    with storage.BOOKING_COMMANDS.snapshot() as repository:
        events = repository.list_events()
    if bookable == "true":
        events = [event for event in events if event.seats_left > 0]
    return {"events": [_event_body(event) for event in events]}, 200


def get_event(event_id):
    """Return one event, or 404 not_found."""
    with storage.BOOKING_COMMANDS.snapshot() as repository:
        event = repository.get_event(event_id)
    if event is None:
        return {"error": "not_found"}, 404
    return _event_body(event), 200


def _event_body(event):
    """Build the Event response, with seatsLeft from the domain rule."""
    return {"id": event.id, "title": event.title, "seatsLeft": event.seats_left}


def create_reservation(event_id, idempotency_key, body_bytes, content_type,
                       *, commands: BookingUnitOfWorkProvider | None = None):
    """Create a reservation, replaying the stored response for a repeated request.

    `commands` is the booking adapter, defaulting to storage.BOOKING_COMMANDS.
    """
    return idempotency.execute_idempotently(
        idempotency_key, body_bytes, f"POST /events/{event_id}/reservations",
        lambda repository: _perform_reservation(
            repository, event_id, body_bytes, content_type
        ),
        commands,
    )


def _perform_reservation(repository: BookingRepository, event_id, body_bytes,
                         content_type):
    """Validate the request, then book one seat in the caller's unit of work.

    Returns 422 for an invalid request, 404 for a missing event, 409 when sold
    out and 201 with the reservation otherwise. createdAt is a fixed timestamp,
    so responses keep their shape. The capacity check, the seat write and the
    saved outcome share one unit of work, so the PostgreSQL adapter locks the
    event and commits all of them together.
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

    event = repository.get_event(event_id)
    if event is None:
        return {"error": "not_found"}, 404
    # The sold_out outcome commits with its replay record, so check before
    # taking a reservation number that the commit would otherwise use up.
    if event.seats_left == 0:
        return {"error": "sold_out"}, 409
    reservation = event.book(
        repository.next_reservation_id(), "2026-09-12T19:00:00Z"
    )
    repository.save_reservation(event, reservation)
    return reservation, 201


def get_reservation(reservation_id):
    """Return a stored reservation, or 404 not_found."""
    with storage.BOOKING_COMMANDS.snapshot() as repository:
        reservation = repository.get_reservation(reservation_id)
    if reservation is None:
        return {"error": "not_found"}, 404
    return reservation, 200


def join_waitlist(event_id, idempotency_key, body_bytes, content_type):
    """Join the waitlist, replaying the stored response for a repeated request.

    The join and its saved outcome commit together, so a retry with the same
    key cannot create a second entry.
    """
    return idempotency.execute_idempotently(
        idempotency_key, body_bytes, f"POST /events/{event_id}/waitlist",
        lambda repository: _perform_waitlist_join(
            repository, event_id, body_bytes, content_type
        )
    )


def _perform_waitlist_join(repository: BookingRepository, event_id, body_bytes,
                           content_type):
    """Validate the body, then add the user to the end of a full event's waitlist.

    Returns 422 invalid_request, 404 event_not_found, 409 event_not_full or
    already_on_waitlist, or 201 with the entry and its position. The checks and
    the insert share one unit of work, so two joins for the same user cannot
    both pass the membership check.
    """
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

    # Check eligibility and membership under the event's guard
    event = repository.get_event(event_id)
    if event is None:
        return {"error": "event_not_found"}, 404
    if event.seats_left > 0:
        return {"error": "event_not_full"}, 409
    entries = repository.waitlist_entries(event_id)
    if any(entry["userId"] == body["userId"] for entry in entries):
        return {"error": "already_on_waitlist"}, 409

    # Create a new waitlist entry
    entry = {
        "id": f"wl_{uuid.uuid4().hex}",
        "eventId": event_id,
        "userId": body["userId"],
        "createdAt": datetime.now(timezone.utc).isoformat(),
    }
    repository.add_waitlist_entry(entry)
    return {**entry, "position": len(entries) + 1}, 201


def _read_waitlist(event_id):
    """Return the event's entries in join order, or None if the event does not exist."""
    with storage.BOOKING_COMMANDS.snapshot() as repository:
        if repository.get_event(event_id) is None:
            return None
        return repository.waitlist_entries(event_id)


def get_waitlist_entries(event_id):
    """List the event's entries with positions from 1 in join order."""
    entries = _read_waitlist(event_id)
    if entries is None:
        return {"error": "event_not_found"}, 404

    # Add each entry's current position in the waitlist
    positioned_entries = [
        {**entry, "position": position}
        for position, entry in enumerate(entries, start=1)
    ]
    return {"entries": positioned_entries}, 200

def get_waitlist_entry_by_id(event_id, entry_id):
    """Return one entry with its current position, or a 404 error."""
    entries = _read_waitlist(event_id)
    if entries is None:
        return {"error": "event_not_found"}, 404

    ## Find the entry and its current position in the waitlist
    for position, entry in enumerate(entries, start=1):
        if entry["id"] == entry_id:
            return {**entry, "position": position}, 200

    return {"error": "entry_not_found"}, 404

def leave_waitlist(event_id, entry_id):
    """Remove an entry under the event's lock and return 204, or a 404 error.

    It needs no Idempotency-Key: repeating it cannot remove the entry twice.
    """
    with storage.BOOKING_COMMANDS.unit_of_work() as repository:
        if repository.get_event(event_id) is None:
            return {"error": "event_not_found"}, 404
        removed = repository.remove_waitlist_entry(event_id, entry_id)
    if removed:
        return None, 204
    return {"error": "entry_not_found"}, 404
