"""HTTP helpers shared by the Hold tests that run app.py on PostgreSQL.

They send requests through the conformance helpers, so no handler code is
imported, and check bodies against the contract's response schemas.
"""

import time
from datetime import datetime

from conformance import get, schema_for, send
from jsonschema import Draft202012Validator

CREATE = "/events/{eventId}/holds"


def create(event_id, key, body=None, content_type=None, query=""):
    """POST a Hold request with exactly these bytes; return (status, body, headers)."""
    headers = {} if key is None else {"Idempotency-Key": key}
    if content_type is not None:
        headers["Content-Type"] = content_type
    return send("POST", f"/events/{event_id}/holds{query}", body, headers)


def validate(path, method, status, body):
    """Check a response body against the contract schema for that status."""
    Draft202012Validator(schema_for(path, method, str(status)),
                         format_checker=Draft202012Validator.FORMAT_CHECKER).validate(body)


def lifetime(hold):
    """Return expiresAt minus createdAt for a Hold body."""
    return (datetime.fromisoformat(hold["expiresAt"])
            - datetime.fromisoformat(hold["createdAt"]))


def seats_left(event_id):
    """Read the event's seatsLeft over HTTP."""
    return get(f"/events/{event_id}")[1]["seatsLeft"]


def wait_until_not_held(hold_id, timeout=10):
    """Poll GET until the Hold stops being held; return that response body."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status, current, _ = send("GET", f"/holds/{hold_id}")
        assert status == 200, current
        if current["status"] != "held":
            return current
        time.sleep(0.05)
    raise AssertionError(f"Hold {hold_id} was still held after {timeout} seconds")
