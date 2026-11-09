"""Live HTTP smoke check of immediate booking against a running app.

Books ev_103 until it sells out and checks the 201, 409, 404 and 422
responses, schema conformance, and the seatsLeft, bookable filter and waitlist
effects. Then books ev_101 with the reply dropped (X-Demo-Lose-Reply) and
retries with the same Idempotency-Key: the retry must return the original
reservation with Idempotency-Replayed: true. The app keeps state in memory, so
this does not test PostgreSQL locking or persistence.

Run once against a fresh app, after the original conformance suite:
    python3 tests/direct_booking_smoke.py
It uses both ev_103 seats and one ev_101 seat. Restart the app to reset.
"""

import json
import urllib.request
from uuid import uuid4

from conformance import BASE, get, post, schema_for
from jsonschema import Draft202012Validator


def main():
    """Check booking/read/filter/queue consistency and actual reply-loss recovery."""
    assert get("/events/ev_103")[1]["seatsLeft"] == 2, "Fresh app required"
    results = [
        post(f"/events/{event}/reservations", b"{}", str(uuid4()))
        for event in ("ev_103", "ev_103", "ev_103", "ev_999", "bad-id")
    ]
    assert [status for status, body in results] == [201, 201, 409, 404, 422], results
    validator = Draft202012Validator(
        schema_for("/events/{eventId}/reservations", "post", "201")
    )
    for status, body in results[:2]:
        validator.validate(body)
        assert get(f"/reservations/{body['id']}") == (200, body)
    assert results[2][1] == {"error": "sold_out"}
    assert results[3][1] == {"error": "not_found"}
    assert results[4][1] == {"error": "invalid_request"}
    assert get("/events/ev_103")[1]["seatsLeft"] == 0
    assert "ev_103" not in [e["id"] for e in get("/events?bookable=true")[1]["events"]]
    assert post("/events/ev_103/waitlist", b'{"userId":"smoke"}', str(uuid4()))[0] == 201

    key = str(uuid4())
    request = urllib.request.Request(
        BASE + "/events/ev_101/reservations", data=b"{}", method="POST",
        headers={"Content-Type": "application/json", "Idempotency-Key": key,
                 "X-Demo-Lose-Reply": "true"},
    )
    try:
        with urllib.request.urlopen(request, timeout=0.2):
            raise AssertionError("Expected reply-loss timeout")
    except TimeoutError:
        pass

    retry = urllib.request.Request(
        BASE + "/events/ev_101/reservations", data=b"{}", method="POST",
        headers={"Content-Type": "application/json", "Idempotency-Key": key},
    )
    with urllib.request.urlopen(retry, timeout=5) as response:
        receipt = json.load(response)
        assert response.status == 201
        assert response.headers.get("Idempotency-Replayed") == "true"
    validator.validate(receipt)
    assert receipt["id"] == "res_3", receipt
    assert get("/events/ev_101")[1]["seatsLeft"] == 41
    assert get(f"/reservations/{receipt['id']}") == (200, receipt)
    print("PASS: booking statuses, strict DTO/GET, filter/queue, reply-loss/replay, IDs")
    print(json.dumps({"results": results, "replayed": receipt}, sort_keys=True))


if __name__ == "__main__":
    main()
