"""Apply the numbered SQL migrations in migrations/ to PostgreSQL, and seed.

Each file named NNN_description.sql is one step, applied once in version
order and recorded in the schema_migrations ledger. A step and its ledger row
commit in one transaction. A PostgreSQL advisory lock lets one migrator run at
a time; a second one waits, then sees the steps the first recorded.
seed_fresh() loads the fresh fixture into an empty database for first setup
and tests; normal starts do not call it.
"""

import json
import re
from pathlib import Path

import psycopg

MIGRATIONS_DIR = Path(__file__).with_name("migrations")
FRESH_FIXTURE = Path(__file__).with_name("contracts") / "fixtures" / "fresh-events.json"
_STEP_NAME = re.compile(r"^(\d+)_\w+\.sql$")
_SETUP_LOCK_KEY = 387_001  # advisory lock shared by migrate() and seed_fresh()
_EMPTY_FIXTURE_LISTS = ("reservations", "holds", "waitlist", "idempotencyOutcomes")


def migrate(conninfo: str, directory: Path = MIGRATIONS_DIR) -> list[str]:
    """Apply every pending step and return the file names applied, in order.

    A failing step is rolled back with its ledger row and its database error
    propagates; steps before it stay applied.
    """
    steps = _steps(directory)
    applied_now: list[str] = []
    with psycopg.connect(conninfo, autocommit=True) as conn:
        conn.execute("SELECT pg_advisory_lock(%s)", (_SETUP_LOCK_KEY,))
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " version INTEGER PRIMARY KEY,"
            " name TEXT NOT NULL,"
            " applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
        applied = {
            version for (version,) in
            conn.execute("SELECT version FROM schema_migrations")
        }
        for version, path in steps:
            if version in applied:
                continue
            with conn.transaction():
                conn.execute(path.read_bytes())
                conn.execute(
                    "INSERT INTO schema_migrations (version, name) VALUES (%s, %s)",
                    (version, path.name),
                )
            applied_now.append(path.name)
    return applied_now


def seed_fresh(conninfo: str, fixture_path: Path = FRESH_FIXTURE) -> bool:
    """Load fixture events into a database that has none; return whether it did.

    A database that already has events or stored outcomes is left unchanged.
    Raises ValueError for a fixture that lists reservations, Holds, waitlist
    entries or outcomes, since seeding never invents booking history.
    """
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    if any(fixture[name] for name in _EMPTY_FIXTURE_LISTS):
        raise ValueError("Seed fixtures must not contain bookings, waitlists or outcomes")
    with psycopg.connect(conninfo) as conn:
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (_SETUP_LOCK_KEY,))
        populated = conn.execute(
            "SELECT EXISTS (SELECT 1 FROM events)"
            " OR EXISTS (SELECT 1 FROM idempotency_outcomes)"
        ).fetchone()
        if populated != (False,):
            return False
        with conn.cursor() as cursor:
            cursor.executemany(
                "INSERT INTO events (id, title, capacity) VALUES (%s, %s, %s)",
                [(event["id"], event["title"], event["capacity"])
                 for event in fixture["events"]],
            )
        conn.execute(
            "UPDATE id_counters SET next_value = %s WHERE name = 'reservation'",
            (fixture["nextReservationNumber"],),
        )
    return True


def _steps(directory: Path) -> list[tuple[int, Path]]:
    """List migration files by version, rejecting duplicate version numbers."""
    steps: dict[int, Path] = {}
    for path in directory.glob("*.sql"):
        match = _STEP_NAME.match(path.name)
        if match is None:
            raise ValueError(f"Migration file name must be NNN_name.sql: {path.name}")
        version = int(match.group(1))
        if version in steps:
            raise ValueError(f"Duplicate migration version {version}")
        steps[version] = path
    return sorted(steps.items())
