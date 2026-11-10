"""PostgreSQL implementation of the booking repository protocols.

Each block opens its own connection; there is no connection pool.
unit_of_work() is one transaction that commits when the block ends normally
and rolls back if it raises. Inside it, get_event() locks the event row and
claim_key() locks the key's idempotency_outcomes row until the transaction
ends, so commands on the same event or key run one at a time.
snapshot() is a read-only REPEATABLE READ transaction, so every read in it
sees the same committed state. The time comes from PostgreSQL's
clock_timestamp(), one clock for every app process. SQL never decides whether
a Hold is live; the adapter loads pending Holds and the domain Event judges
them against that time.
"""

from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from booking_domain import Event, Hold
from booking_repository import BookingRepository, TransactionAborted

_OUTCOME_FIELDS = ("request_target", "request_hash", "status", "body", "completed_at")
# Holds not ended by confirmation or release. Whether each is still live
# depends on the time, which only the domain judges.
_PENDING_HOLDS = ("SELECT id, event_id, created_at, expires_at FROM holds"
                  " WHERE reservation_id IS NULL AND released_at IS NULL")
_RECOGNIZED_ABORTS = (psycopg.errors.SerializationFailure,
                      psycopg.errors.DeadlockDetected)
_INSERT_RESERVATION = ("INSERT INTO reservations (id, event_id, status, created_at)"
                       " VALUES (%(id)s, %(eventId)s, %(status)s, %(createdAt)s)")


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
        """Run the block in a read-only transaction with one consistent view.

        Its first statement reads the clock. In REPEATABLE READ that statement
        also fixes the view, so every read and now() describe one moment.
        """
        with psycopg.connect(self.conninfo) as conn:
            conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
            conn.read_only = True
            observed_at = _clock(conn)
            yield _PostgresRepository(conn, lock_events=False, observed_at=observed_at)


def _clock(conn: psycopg.Connection) -> datetime:
    """Read the database's wall-clock time, which every worker shares."""
    row = conn.execute("SELECT clock_timestamp()").fetchone()
    assert row is not None
    return row[0]


def _pending_hold(row: tuple) -> Hold:
    """Build a pending Hold from an id, event_id, created_at, expires_at row."""
    hold_id, event_id, created_at, expires_at = row
    return Hold(hold_id, event_id, created_at, expires_at)


class _PostgresRepository:
    """Repository calls translated to SQL on one open transaction."""

    def __init__(self, conn: psycopg.Connection, lock_events: bool,
                 observed_at: datetime | None = None):
        """Use conn's open transaction; lock_events makes get_event() lock rows.

        observed_at fixes now() for a snapshot; without it now() reads the clock.
        """
        self._conn = conn
        self._lock_events = lock_events
        self._observed_at = observed_at

    def now(self) -> datetime:
        """Return the snapshot's time, or a fresh clock reading in a unit of work."""
        if self._observed_at is not None:
            return self._observed_at
        return _clock(self._conn)

    def get_event(self, event_id: str) -> Event | None:
        """Load the Event, locking its row first in a unit of work.

        Occupancy and pending Holds are read in later statements, after the
        lock is held, so they include what the previous lock holder committed.
        A count inside the locking statement would use the state from before
        any lock wait and could allow an oversell. The clock is read last, in
        its own query, so a command that waited judges expiry at the time it
        decides, not when its transaction began.
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
        holds = [_pending_hold(hold_row) for hold_row in self._conn.execute(
            _PENDING_HOLDS + " AND event_id = %s", (event_id,)
        )]
        return Event(event_id, capacity, confirmed, title=title, holds=holds,
                     now=self.now())

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
        self._conn.execute(_INSERT_RESERVATION, reservation)

    def save_confirmation(self, hold: Hold, reservation: dict[str, str]) -> None:
        """Insert the reservation row, then link it from the Hold's row.

        The reservation goes first because holds.reservation_id references it.
        """
        self._conn.execute(_INSERT_RESERVATION, reservation)
        self._conn.execute("UPDATE holds SET reservation_id = %s WHERE id = %s",
                           (hold.reservation_id, hold.id))

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
        """Load every Event, its occupancy and pending Holds, ordered by ID."""
        rows = self._conn.execute(
            "SELECT e.id, e.title, e.capacity, count(r.id)"
            " FROM events e LEFT JOIN reservations r ON r.event_id = e.id"
            " GROUP BY e.id ORDER BY e.id"
        ).fetchall()
        holds_by_event = defaultdict(list)
        for hold_row in self._conn.execute(_PENDING_HOLDS):
            hold = _pending_hold(hold_row)
            holds_by_event[hold.event_id].append(hold)
        now = self.now()
        return [Event(event_id, capacity, confirmed, title=title,
                      holds=holds_by_event[event_id], now=now)
                for event_id, title, capacity, confirmed in rows]

    def save_hold(self, hold: Hold) -> None:
        """Insert the Hold row with its fixed deadline."""
        self._conn.execute(
            "INSERT INTO holds (id, event_id, created_at, expires_at)"
            " VALUES (%s, %s, %s, %s)",
            (hold.id, hold.event_id, hold.created_at, hold.expires_at),
        )

    def get_hold(self, hold_id: str) -> Hold | None:
        """Return the stored Hold with its confirmation and release state, or None."""
        row = self._conn.execute(
            "SELECT id, event_id, created_at, expires_at, reservation_id,"
            " released_at IS NOT NULL FROM holds WHERE id = %s",
            (hold_id,),
        ).fetchone()
        if row is None:
            return None
        hold_id, event_id, created_at, expires_at, reservation_id, released = row
        return Hold(hold_id, event_id, created_at, expires_at,
                    reservation_id=reservation_id, released=released)

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
