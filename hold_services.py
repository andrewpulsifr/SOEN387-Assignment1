"""Service functions for creating and inspecting Holds.

Creation is a keyed command: it runs in one unit of work through
idempotency.execute_idempotently, so the Hold and its stored outcome commit
together. Inspection runs in one snapshot, so the Hold's state and the time
used to judge its expiry come from the same observation. Confirming and
releasing a Hold are not implemented yet.
"""

import uuid
from datetime import timedelta, timezone

import idempotency
import storage
import validation
from booking_domain import Hold, SoldOut
from booking_repository import BookingRepository

# How long a new Hold lasts. app.main() sets it from ENCORE_HOLD_TTL_SECONDS.
HOLD_TTL = timedelta(seconds=90)


def create_hold(event_id, idempotency_key, body_bytes, content_type, query_args):
    """Hold one seat, or replay the stored outcome of a repeated request.

    Returns (body, status, replayed). The binding covers the endpoint, the
    event ID and the exact body bytes, so an absent body and {} differ. A
    replay returns the original receipt or error, even after the Hold has
    expired, and never creates or renews a Hold.
    """
    return idempotency.execute_idempotently(
        idempotency_key, body_bytes, f"POST /events/{event_id}/holds",
        lambda repository: _perform_hold(
            repository, event_id, body_bytes, content_type, query_args
        ),
    )


def _perform_hold(repository: BookingRepository, event_id, body_bytes,
                  content_type, query_args):
    """Validate the request, then hold one seat in the caller's unit of work.

    Returns 422 invalid_request, 404 event_not_found, 409 sold_out, or 201
    with the receipt. Every validation failure has the same outcome, so the
    order of the checks cannot be observed. get_event() locks the Event and
    then reads the clock, so the seat check sees current occupancy and time.
    """
    try:
        validation.validate_request(
            "POST", "/events/{eventId}/holds", {"eventId": event_id},
            body_bytes=body_bytes, content_type=content_type,
        )
    except validation.InvalidRequest:
        return {"error": "invalid_request"}, 422
    if query_args:
        return {"error": "invalid_request"}, 422

    event = repository.get_event(event_id)
    if event is None:
        return {"error": "event_not_found"}, 404
    try:
        hold = event.hold(f"hold_{uuid.uuid4().hex}", HOLD_TTL)
    except SoldOut:
        return {"error": "sold_out"}, 409
    repository.save_hold(hold)
    return {**_identity(hold), "status": "held"}, 201


def get_hold(hold_id, query_args, body_bytes):
    """Return the Hold's current state and countdown, or a coded error.

    Any query parameter or body gives 422 invalid_request; an unknown ID
    gives 404 hold_not_found. The route has already checked the ID's syntax.
    """
    if query_args or body_bytes:
        return {"error": "invalid_request"}, 422
    with storage.BOOKING_COMMANDS.snapshot() as repository:
        hold = repository.get_hold(hold_id)
        now = repository.now()
    if hold is None:
        return {"error": "hold_not_found"}, 404

    body = {**_identity(hold), "status": hold.status_at(now),
            "secondsRemaining": hold.seconds_remaining_at(now)}
    if hold.reservation_id is not None:
        body["reservationId"] = hold.reservation_id
    return body, 200


def _identity(hold: Hold):
    """Return the fields shared by the receipt and the current view."""
    return {"id": hold.id, "eventId": hold.event_id,
            "createdAt": _timestamp(hold.created_at),
            "expiresAt": _timestamp(hold.expires_at)}


def _timestamp(moment):
    """Format a time as UTC RFC 3339 with microseconds, e.g. 2026-11-09T20:00:00.000000Z."""
    return (moment.astimezone(timezone.utc).isoformat(timespec="microseconds")
            .replace("+00:00", "Z"))
