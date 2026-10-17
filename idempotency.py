"""Replay saved outcomes from the single-process, in-memory service.

Keys share one global namespace. Records expire lazily after 24 hours while
the process stays running. A restart clears them. The lock coordinates threads
in this process only.
"""

from copy import deepcopy
import hashlib
from threading import Lock
from time import monotonic

import storage

_OPERATION_LOCK = Lock()
IDEMPOTENCY_RETENTION_SECONDS = 24 * 60 * 60


def execute_idempotently(key, body_bytes, request_target, operation):
    """Replay a saved outcome for the same key, target, and request body.

    The callback returns expected errors as (body, status), so the helper saves
    them with successful responses. Unexpected exceptions propagate without a
    record. A callback may leave partial changes before raising, so a retry can
    run it again. Callbacks must not invoke this helper recursively.

    The replay flag is diagnostic and may be exposed as Idempotency-Replayed.
    Outcomes are lazily expired when their key reaches the 24-hour retention
    window. The in-memory store clears on restart.
    """
    if not key:
        return {"error": "idempotency_key_required"}, 422, False

    request_digest = hashlib.sha256(body_bytes).hexdigest()
    # Serialize the idempotency lookup and update to prevent duplicate execution.
    with _OPERATION_LOCK:
        seen = storage.IDEMPOTENCY.get(key)
        if seen is not None:
            if monotonic() - seen["stored_at"] >= IDEMPOTENCY_RETENTION_SECONDS:
                del storage.IDEMPOTENCY[key]
                seen = None
        if seen is not None:
            if (seen["request_target"] != request_target
                    or seen["request_hash"] != request_digest):
                # Same key, different request: a client bug, and a dangerous one.
                return {"error": "idempotency_key_reuse"}, 422, False
            return deepcopy(seen["body"]), seen["status"], True  # replay the stored outcome

        # First time we see this key: perform the operation, then record the outcome.
        body, status = operation()
        storage.IDEMPOTENCY[key] = {
            "request_target": request_target,
            "request_hash": request_digest,
            "status": status,
            "body": deepcopy(body),
            "stored_at": monotonic(),
        }
        return body, status, False
