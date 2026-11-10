"""Replay saved outcomes of keyed POST requests.

Keys share one global namespace. Each keyed command runs in one unit of work
from the booking adapter: claiming the key, the operation's writes and the
saved outcome commit together or not at all. The key is claimed before the
operation locks any event. Outcomes stay replayable for 24 hours after the
operation completes, measured on this process's UTC clock, and survive
restarts on PostgreSQL.

A command that the adapter reports as TransactionAborted runs again from the
start, up to MAX_COMMAND_ATTEMPTS times in all, with no delay between attempts.
After the last abort the caller gets 503 service_unavailable, and nothing is
stored, so the same key can be retried. Other errors propagate on the first
attempt.
"""

from datetime import datetime, timedelta, timezone
import hashlib

import storage
from booking_repository import BookingUnitOfWorkProvider, TransactionAborted

IDEMPOTENCY_RETENTION = timedelta(hours=24)
MAX_COMMAND_ATTEMPTS = 3


def utc_now():
    """Return the current UTC time, the clock that retention is measured on."""
    return datetime.now(timezone.utc)


def execute_idempotently(key, body_bytes, request_target, operation,
                         commands: BookingUnitOfWorkProvider | None = None):
    """Run a keyed command once, or replay its saved outcome; return (body, status, replayed).

    A live outcome with the same target and exact body bytes is replayed, and
    a live outcome for another target or body gives 422 idempotency_key_reuse.
    Both checks happen before operation runs, and neither changes the stored
    outcome. A missing key gives 422 idempotency_key_required and stores nothing.

    operation(repository) runs inside the unit of work and returns expected
    errors as (body, status), so they are saved like successes. An exception
    from it rolls back the whole block, including the key claim, and
    propagates. Retries and 503 follow the module rules. `commands` defaults
    to storage.BOOKING_COMMANDS.
    """
    if not key:
        return {"error": "idempotency_key_required"}, 422, False

    commands = storage.BOOKING_COMMANDS if commands is None else commands
    request_hash = hashlib.sha256(body_bytes).hexdigest()
    for _ in range(MAX_COMMAND_ATTEMPTS):
        try:
            with commands.unit_of_work() as repository:
                return _run_once_for_key(repository, key, request_target,
                                         request_hash, operation)
        except TransactionAborted:
            continue
    return {"error": "service_unavailable"}, 503, False


def _run_once_for_key(repository, key, request_target, request_hash, operation):
    """Replay or reject the key's live outcome, or run the operation and save its outcome.

    An outcome is live until 24 hours after its completion time; at that
    instant or later the key starts a new attempt and may bind another request.
    """
    stored = repository.claim_key(key, request_target, request_hash)
    binding_is_live = (stored is not None
                       and utc_now() < stored["completed_at"] + IDEMPOTENCY_RETENTION)
    if binding_is_live:
        if (stored["request_target"] != request_target
                or stored["request_hash"] != request_hash):
            # Same key, different request: a client bug, and a dangerous one.
            return {"error": "idempotency_key_reuse"}, 422, False
        return stored["body"], stored["status"], True

    body, status = operation(repository)
    repository.save_outcome(key, {
        "request_target": request_target,
        "request_hash": request_hash,
        "status": status,
        "body": body,
        "completed_at": utc_now(),
    })
    return body, status, False
