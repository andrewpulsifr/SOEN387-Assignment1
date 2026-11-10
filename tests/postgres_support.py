"""Disposable PostgreSQL databases for tests that need a real server.

Set ENCORE_TEST_DATABASE_URL to a server URL whose user may create
databases, for example the Compose server's postgres database. Each test
gets a new empty database, which is dropped afterwards. Without the variable,
these tests are skipped.
"""

import os
import unittest
import uuid
from unittest.mock import patch

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

import migrate
import storage
from postgres_booking import PostgresBookingCommands

ADMIN_URL = os.environ.get("ENCORE_TEST_DATABASE_URL", "")

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
        for store in (storage.IDEMPOTENCY, storage.RESERVATIONS, storage.WAITLIST):
            isolated = patch.dict(store, clear=True)
            isolated.start()
            self.addCleanup(isolated.stop)
        self.commands = PostgresBookingCommands(self.database_url)
        adapter = patch.object(storage, "BOOKING_COMMANDS", self.commands)
        adapter.start()
        self.addCleanup(adapter.stop)
