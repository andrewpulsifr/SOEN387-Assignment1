"""Service functions for creating, inspecting, confirming and releasing Holds.

Creation is a keyed command: it runs in one unit of work through
idempotency.execute_idempotently, so the Hold and its stored outcome commit
together. Inspection runs in one snapshot, so the Hold's state and the time
used to judge its expiry come from the same observation. Confirmation and
release take no key and store no outcome; they run through
idempotency.run_command for the same retry rules. A repeat confirmation
returns the Reservation already linked, and a repeat release changes nothing.
"""

import uuid
from datetime import timedelta, timezone

import idempotency
import services
import storage
import validation
from booking_domain import Hold, HoldConfirmed, HoldExpired, HoldReleased, SoldOut
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


def confirm_hold(hold_id, query_args, body_bytes):
    """Confirm a live Hold into a new Reservation, or return the one it already has.

    Returns 201 with the new Reservation, 200 with the linked one, 404
    hold_not_found, 409 hold_expired or hold_released, 422 invalid_request
    for a malformed ID, any query or any body, or 503 service_unavailable.
    The Reservation and its link commit together. A 409 raises out of the
    unit of work, so the block rolls back, including the reservation number
    it allocated. Neither a 409 nor a 503 changes anything, so after a 503
    the caller can GET the Hold or confirm again.
    """
    if _is_invalid_request("POST", "/holds/{holdId}/confirm", hold_id,
                           query_args, body_bytes):
        return {"error": "invalid_request"}, 422

    try:
        return idempotency.run_command(
            lambda repository: _perform_confirm(repository, hold_id)
        )
    except HoldExpired:
        return {"error": "hold_expired"}, 409
    except HoldReleased:
        return {"error": "hold_released"}, 409
    except idempotency.ServiceUnavailable:
        return {"error": "service_unavailable"}, 503


def release_hold(hold_id, query_args, body_bytes):
    """Release a live Hold early; a released or expired Hold stays as it is.

    Returns (None, 204) for a live, released or expired Hold, 404
    hold_not_found, 409 already_confirmed with the linked reservationId, 422
    invalid_request for a malformed ID, any query or any body, or 503
    service_unavailable. Only a live Hold is written, so a repeat frees no
    second seat and an expired Hold is never marked released. A 409 or 503
    changes nothing, so the caller can GET the Hold or release again.
    """
    if _is_invalid_request("DELETE", "/holds/{holdId}", hold_id, query_args, body_bytes):
        return {"error": "invalid_request"}, 422
    try:
        return idempotency.run_command(
            lambda repository: _perform_release(repository, hold_id)
        )
    except idempotency.ServiceUnavailable:
        return {"error": "service_unavailable"}, 503


def _perform_release(repository: BookingRepository, hold_id):
    """Release the reread Hold under its Event's lock, storing only a real change."""
    found = _lock_event_and_reread(repository, hold_id)
    if found is None:
        return {"error": "hold_not_found"}, 404
    event, hold = found
    try:
        released_at = event.release(hold)
    except HoldConfirmed:
        return {"error": "already_confirmed", "reservationId": hold.reservation_id}, 409
    if released_at is not None:
        repository.save_release(hold, released_at)
    return None, 204


def _lock_event_and_reread(repository: BookingRepository, hold_id):
    """Find the Hold's Event, lock it, then reread the Hold; None for an unknown Hold.

    The first read only finds the Event. A command that held the lock first
    may have changed the Hold since then, so callers decide on the reread.
    Every command that changes a Hold locks its Event first, so the reread
    stays current until the block ends. The Event judges expiry with the
    clock read after its lock.
    """
    before_lock = repository.get_hold(hold_id)
    if before_lock is None:
        return None
    event = repository.get_event(before_lock.event_id)
    hold = repository.get_hold(hold_id)
    assert event is not None and hold is not None
    return event, hold


def _perform_confirm(repository: BookingRepository, hold_id):
    """Confirm the reread Hold under its Event's lock, or recover its Reservation."""
    found = _lock_event_and_reread(repository, hold_id)
    if found is None:
        return {"error": "hold_not_found"}, 404
    event, hold = found
    if hold.reservation_id is not None:
        return repository.get_reservation(hold.reservation_id), 200

    reservation = event.confirm(hold, repository.next_reservation_id(),
                                services.RESERVATION_CREATED_AT)
    repository.save_confirmation(hold, reservation)
    return reservation, 201


def _is_invalid_request(method, path, hold_id, query_args, body_bytes):
    """Return whether the Hold ID is malformed or the request has a query or body."""
    try:
        validation.validate_request(method, path, {"holdId": hold_id})
    except validation.InvalidRequest:
        return True
    return bool(query_args or body_bytes)


def _identity(hold: Hold):
    """Return the fields shared by the receipt and the current view."""
    return {"id": hold.id, "eventId": hold.event_id,
            "createdAt": _timestamp(hold.created_at),
            "expiresAt": _timestamp(hold.expires_at)}


def _timestamp(moment):
    """Format a time as UTC RFC 3339 with microseconds, e.g. 2026-11-09T20:00:00.000000Z."""
    return (moment.astimezone(timezone.utc).isoformat(timespec="microseconds")
            .replace("+00:00", "Z"))
