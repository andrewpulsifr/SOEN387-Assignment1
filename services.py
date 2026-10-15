import hashlib

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
    if not key:
        return {"error": "idempotency_key_required"}, 422, False

    request_hash = hashlib.sha256(body_bytes or b"").hexdigest()
    seen = storage.IDEMPOTENCY.get(key)
    if seen is not None:
        if seen["request_hash"] != request_hash:
            # Same key, different request: a client bug, and a dangerous one.
            return {"error": "idempotency_key_reuse"}, 422, False
        return dict(seen["body"]), seen["status"], True  # replay the stored outcome

    # First time we see this key: perform the operation, then record the outcome.
    event = storage.EVENTS.get(event_id)
    if event is None:
        body, status = {"error": "not_found"}, 404
    elif event["seatsLeft"] <= 0:
        body, status = {"error": "sold_out"}, 409
    else:
        event["seatsLeft"] -= 1
        reservation_id = f"res_{storage.NEXT['n']}"
        storage.NEXT["n"] += 1
        body = {"id": reservation_id, "eventId": event_id,
                "status": "confirmed", "createdAt": "2026-09-12T19:00:00Z"}
        storage.RESERVATIONS[reservation_id] = body
        status = 201

    storage.IDEMPOTENCY[key] = {
        "request_hash": request_hash, "status": status, "body": dict(body)
    }
    return body, status, False


def get_reservation(res_id):
    reservation = storage.RESERVATIONS.get(res_id)
    if reservation is None:
        return {"error": "not_found"}, 404
    return reservation, 200
