import json
from datetime import datetime, timezone
import uuid

import idempotency
import storage


def list_events(bookable):
    events = list(storage.EVENTS.values())
    if bookable == "true":
        events = [e for e in events if e["seatsLeft"] > 0]
    return {"events": events}, 200


def get_event(event_id):
    ev = storage.EVENTS.get(event_id)
    if ev is None:
        return {"error": "not_found"}, 404
    return ev, 200


def create_reservation(event_id, key, body_bytes):
    return idempotency.execute_idempotently(
        key, body_bytes, f"POST /events/{event_id}/reservations",
        lambda: _perform_reservation(event_id)
    )


def _perform_reservation(event_id):
    event = storage.EVENTS.get(event_id)
    if event is None:
        outcome = ({"error": "not_found"}, 404)
    elif event["seatsLeft"] <= 0:
        outcome = ({"error": "sold_out"}, 409)
    else:
        event["seatsLeft"] -= 1
        reservation_id = f"res_{storage.NEXT['n']}"
        storage.NEXT["n"] += 1
        reservation = {"id": reservation_id, "eventId": event_id,
                       "status": "confirmed", "createdAt": "2026-09-12T19:00:00Z"}
        storage.RESERVATIONS[reservation_id] = reservation
        outcome = (reservation, 201)
    return outcome


def get_reservation(res_id):
    reservation = storage.RESERVATIONS.get(res_id)
    if reservation is None:
        return {"error": "not_found"}, 404
    return reservation, 200


def join_waitlist(event_id, key, body_bytes):
    return idempotency.execute_idempotently(
        key, body_bytes, f"POST /events/{event_id}/waitlist",
        lambda: _perform_waitlist_join(event_id, body_bytes)
    )


def _perform_waitlist_join(event_id, body_bytes):
    try:
        body = json.loads(body_bytes)
    except (ValueError, UnicodeDecodeError):
        return {"error": "invalid_request"}, 422
    if (not isinstance(body, dict) or set(body) != {"userId"}
            or not isinstance(body["userId"], str) or not body["userId"]):
        return {"error": "invalid_request"}, 422

    event = storage.EVENTS.get(event_id)
    if event is None:
        return {"error": "event_not_found"}, 404
    if event["seatsLeft"] > 0:
        return {"error": "event_not_full"}, 409
    entries = storage.WAITLIST.get(event_id, [])
    if any(entry["userId"] == body["userId"] for entry in entries):
        return {"error": "already_on_waitlist"}, 409

    entry = {
        "id": f"wl_{uuid.uuid4().hex}",
        "eventId": event_id,
        "userId": body["userId"],
        "createdAt": datetime.now(timezone.utc).isoformat(),
    }
    entries = storage.WAITLIST.setdefault(event_id, [])
    entries.append(entry)
    return {**entry, "position": len(entries)}, 201
