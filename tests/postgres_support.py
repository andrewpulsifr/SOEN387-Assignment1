"""Disposable PostgreSQL databases, and app.py running on them, for tests.

Set ENCORE_TEST_DATABASE_URL to a server URL whose user may create
databases, for example the Compose server's postgres database; without it
these tests are skipped. Each test gets a new empty database, dropped
afterwards, and either calls the service functions on it or starts app.py on
it and sends HTTP requests. tests/run_conformance.py starts app.py the same
way through serving_app(). A test can also make chosen row writes, such as
reservation INSERTs or Hold UPDATEs, fail with a chosen SQLSTATE to exercise
retries.
"""

import os
import socket
import subprocess
import sys
import time
import unittest
import urllib.request
import uuid
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import conformance
import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

import migrate
import storage
from postgres_booking import PostgresBookingCommands

ADMIN_URL = os.environ.get("ENCORE_TEST_DATABASE_URL", "")
APP = Path(__file__).resolve().parents[1] / "app.py"

requires_postgres = unittest.skipUnless(
    ADMIN_URL, "set ENCORE_TEST_DATABASE_URL to run PostgreSQL checks"
)


def create_database() -> str:
    """Create an empty uniquely named database and return its connection URL."""
    name = f"encore_test_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(ADMIN_URL, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    return make_conninfo(ADMIN_URL, dbname=name)


def drop_database(conninfo: str) -> None:
    """Drop a database made by create_database(), closing its open sessions."""
    name = conninfo_to_dict(conninfo)["dbname"]
    with psycopg.connect(ADMIN_URL, autocommit=True) as conn:
        conn.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                sql.Identifier(str(name))
            )
        )


class PostgresTestCase(unittest.TestCase):
    """Give each test its own empty database in self.database_url."""

    def setUp(self):
        """Create the database and register its removal."""
        self.database_url = create_database()
        self.addCleanup(drop_database, self.database_url)

    def query(self, statement, params=None):
        """Run one read query in its own connection and return all rows."""
        with psycopg.connect(self.database_url) as conn:
            return conn.execute(statement, params).fetchall()

    def execute(self, statement):
        """Run SQL without parameters in its own committed transaction."""
        with psycopg.connect(self.database_url) as conn:
            conn.execute(statement)

    def fail_writes(self, sqlstate, times, on="INSERT ON reservations"):
        """Make the next `times` row writes named by `on` raise `sqlstate` inside PostgreSQL.

        `on` is a trigger event and table, such as "UPDATE ON holds". A
        sequence counts the attempts, since nextval() survives a rollback.
        """
        self.execute(f"""
            CREATE SEQUENCE write_attempts;
            CREATE FUNCTION fail_write() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                IF nextval('write_attempts') <= {times} THEN
                    RAISE EXCEPTION 'injected' USING ERRCODE = '{sqlstate}';
                END IF;
                RETURN NEW;
            END $$;
            CREATE TRIGGER fail_write BEFORE {on}
                FOR EACH ROW EXECUTE FUNCTION fail_write();
        """)

    def write_attempts(self):
        """Return how many row writes the fail_writes() trigger has seen."""
        return self.query("SELECT last_value FROM write_attempts")[0][0]


def free_port():
    """Return a local TCP port that is free right now."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def app_command(port, *args):
    """Build the app.py command line for this port plus extra arguments."""
    return [sys.executable, str(APP), "--port", str(port), *args]


def app_env(database_url, env=None):
    """Return the environment that points app.py at database_url.

    env adds or overrides variables, such as ENCORE_HOLD_TTL_SECONDS.
    """
    return {**os.environ, "ENCORE_DATABASE_URL": database_url, **(env or {})}


@contextmanager
def serving_app(database_url, *args, env=None):
    """Start app.py on database_url and a free port, and kill it afterwards.

    Yields the app's base URL once it answers GET /events. Raises
    AssertionError if the app exits first or does not answer within 15 seconds.
    """
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    process = subprocess.Popen(app_command(port, *args), env=app_env(database_url, env),
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    assert process.stdout is not None
    output = process.stdout
    try:
        deadline = time.monotonic() + 15
        while True:
            if process.poll() is not None:
                raise AssertionError(f"app exited early:\n{output.read().decode()}")
            try:
                urllib.request.urlopen(base + "/events", timeout=1).close()
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise AssertionError("app did not start serving within 15 seconds")
                time.sleep(0.1)
        yield base
    finally:
        process.kill()
        process.wait(timeout=5)
        output.close()


class AppOnPostgresTestCase(PostgresTestCase):
    """Start app.py as a process on this test's database and talk to it over HTTP."""

    def app_command(self, *args):
        """Build the app.py command line for self.port plus extra arguments."""
        return app_command(self.port, *args)

    def app_env(self, env=None):
        """Return the environment that points app.py at the test database.

        env adds or overrides variables, such as ENCORE_HOLD_TTL_SECONDS.
        """
        return app_env(self.database_url, env)

    @contextmanager
    def running_app(self, *args, env=None):
        """Start app.py on the test database and kill it after the block.

        Inside the block the conformance helpers send requests to this app.
        """
        with (serving_app(self.database_url, *args, env=env) as base,
              patch.object(conformance, "BASE", base)):
            yield


class ServicesOnPostgresTestCase(PostgresTestCase):
    """Run the service functions on a migrated, freshly seeded database.

    Process-memory stores in storage.py start empty and are restored after
    each test, so only the database can carry state between calls.
    """

    def setUp(self):
        """Migrate and seed the database, then point the services at it."""
        super().setUp()
        migrate.migrate(self.database_url)
        migrate.seed_fresh(self.database_url, migrate.FRESH_FIXTURE)
        for store in (storage.IDEMPOTENCY, storage.RESERVATIONS, storage.WAITLIST,
                      storage.HOLDS):
            isolated = patch.dict(store, clear=True)
            isolated.start()
            self.addCleanup(isolated.stop)
        self.commands = PostgresBookingCommands(self.database_url)
        adapter = patch.object(storage, "BOOKING_COMMANDS", self.commands)
        adapter.start()
        self.addCleanup(adapter.stop)
