"""PostgreSQL implementation of the booking repository protocols.

Each block opens its own connection; there is no connection pool.
unit_of_work() is one transaction that commits when the block ends normally
and rolls back if it raises. Inside it, get_event() locks the event row and
claim_key() locks the key's idempotency_outcomes row until the transaction
ends, so commands on the same event or key run one at a time.
snapshot() is a read-only REPEATABLE READ transaction, so every read in it
sees the same committed state.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from booking_domain import Event
from booking_repository import BookingRepository, TransactionAborted

_OUTCOME_FIELDS = ("request_target", "request_hash", "status", "body", "completed_at")
_RECOGNIZED_ABORTS = (psycopg.errors.SerializationFailure,
                      psycopg.errors.DeadlockDetected)


class PostgresBookingCommands:
    """Open command and snapshot blocks against one PostgreSQL database."""

    def __init__(self, conninfo: str):
        """Keep the connection string that each block connects with."""
        self.conninfo = conninfo

    @contextmanager
    def unit_of_work(self) -> Iterator[BookingRepository]:
        """Run the block in one transaction that locks each event it loads.

        A serialization failure (SQLSTATE 40001) or detected deadlock (40P01),
        from a statement or from COMMIT, becomes TransactionAborted: PostgreSQL
        has rolled the whole transaction back. Every other error, including a
        connection lost during COMMIT, propagates unchanged, because the
        outcome of the transaction may be unknown.
        """
        try:
            with psycopg.connect(self.conninfo) as conn:
                yield _PostgresRepository(conn, lock_events=True)
        except _RECOGNIZED_ABORTS as error:
            raise TransactionAborted(error.sqlstate) from error

    @contextmanager
    def snapshot(self) -> Iterator[BookingRepository]:
        """Run the block in a read-only transaction with one consistent view."""
        with psycopg.connect(self.conninfo) as conn:
            conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
            conn.read_only = True
            yield _PostgresRepository(conn, lock_events=False)


class _PostgresRepository:
    """Repository calls translated to SQL on one open transaction."""

    def __init__(self, conn: psycopg.Connection, lock_events: bool):
        """Use conn's open transaction; lock_events makes get_event() lock rows."""
        self._conn = conn
        self._lock_events = lock_events

    def get_event(self, event_id: str) -> Event | None:
        """Load the Event, locking its row first in a unit of work.

        Occupancy is counted in a second statement, after the lock is held, so
        it includes bookings committed by the previous lock holder. A count
        inside the locking statement would use the state from before any lock
        wait and could allow an oversell.
        """
        lock = " FOR UPDATE" if self._lock_events else ""
        row = self._conn.execute(
            "SELECT title, capacity FROM events WHERE id = %s" + lock, (event_id,)
        ).fetchone()
        if row is None:
            return None
        title, capacity = row
        confirmed = self._scalar(
            "SELECT count(*) FROM reservations WHERE event_id = %s", (event_id,)
        )
        return Event(event_id, capacity, confirmed, title=title)

    def next_reservation_id(self) -> str:
        """Take the next res_N number; a rollback returns it to the counter."""
        number = self._scalar(
            "UPDATE id_counters SET next_value = next_value + 1"
            " WHERE name = 'reservation' RETURNING next_value - 1",
            (),
        )
        return f"res_{number}"

    def save_reservation(self, event: Event, reservation: dict[str, str]) -> None:
        """Insert the reservation row."""
        self._conn.execute(
            "INSERT INTO reservations (id, event_id, status, created_at)"
            " VALUES (%s, %s, %s, %s)",
            (reservation["id"], reservation["eventId"], reservation["status"],
             reservation["createdAt"]),
        )

    def get_reservation(self, reservation_id: str) -> dict[str, str] | None:
        """Return the stored reservation, or None."""
        row = self._conn.execute(
            "SELECT id, event_id, status, created_at FROM reservations WHERE id = %s",
            (reservation_id,),
        ).fetchone()
        if row is None:
            return None
        return dict(zip(("id", "eventId", "status", "createdAt"), row))

    def list_events(self) -> list[Event]:
        """Load every Event and its occupancy in one statement, ordered by ID."""
        rows = self._conn.execute(
            "SELECT e.id, e.title, e.capacity, count(r.id)"
            " FROM events e LEFT JOIN reservations r ON r.event_id = e.id"
            " GROUP BY e.id ORDER BY e.id"
        ).fetchall()
        return [Event(event_id, capacity, confirmed, title=title)
                for event_id, title, capacity, confirmed in rows]

    def waitlist_entries(self, event_id: str) -> list[dict[str, str]]:
        """Return the event's waitlist entries in join order."""
        rows = self._conn.execute(
            "SELECT id, event_id, user_id, created_at FROM waitlist_entries"
            " WHERE event_id = %s ORDER BY join_order",
            (event_id,),
        ).fetchall()
        return [dict(zip(("id", "eventId", "userId", "createdAt"), row))
                for row in rows]

    def add_waitlist_entry(self, entry: dict[str, str]) -> None:
        """Insert the entry; the database gives it the next join_order value."""
        self._conn.execute(
            "INSERT INTO waitlist_entries (id, event_id, user_id, created_at)"
            " VALUES (%s, %s, %s, %s)",
            (entry["id"], entry["eventId"], entry["userId"], entry["createdAt"]),
        )

    def remove_waitlist_entry(self, event_id: str, entry_id: str) -> bool:
        """Delete the entry and return whether a row was removed."""
        cursor = self._conn.execute(
            "DELETE FROM waitlist_entries WHERE event_id = %s AND id = %s",
            (event_id, entry_id),
        )
        return cursor.rowcount == 1

    def claim_key(self, key: str, request_target: str,
                  request_hash: str) -> dict[str, Any] | None:
        """Insert a placeholder row for a free key, or lock and return the stored one.

        The key's primary key decides ownership: a same-key INSERT in another
        transaction waits until this one ends, then finds the committed row.
        The placeholder has no outcome; save_outcome() fills it before commit.
        """
        # 1. Try to own the key. If another transaction holds it, this blocks
        #    until that transaction commits or rolls back.
        claimed = self._conn.execute(
            "INSERT INTO idempotency_outcomes (key, request_target, request_hash)"
            " VALUES (%s, %s, %s) ON CONFLICT (key) DO NOTHING RETURNING key",
            (key, request_target, request_hash),
        ).fetchone()
        # Row returned: we own the key, so the caller runs the command.
        if claimed is not None:
            return None
        # 2. Key already stored: lock its row and return it for replay.
        row = self._conn.execute(
            "SELECT request_target, request_hash, status, body, completed_at"
            " FROM idempotency_outcomes WHERE key = %s FOR UPDATE",
            (key,),
        ).fetchone()
        assert row is not None
        return dict(zip(_OUTCOME_FIELDS, row))

    def save_outcome(self, key: str, outcome: dict[str, Any]) -> None:
        """Write the outcome into the key's row, replacing the placeholder or an old one."""
        self._conn.execute(
            "UPDATE idempotency_outcomes SET request_target = %s, request_hash = %s,"
            " status = %s, body = %s, completed_at = %s WHERE key = %s",
            (outcome["request_target"], outcome["request_hash"], outcome["status"],
             Jsonb(outcome["body"]), outcome["completed_at"], key),
        )

    def _scalar(self, query: Any, params: tuple) -> Any:
        """Run a query that always returns one row and return its first column."""
        row = self._conn.execute(query, params).fetchone()
        assert row is not None
        return row[0]
