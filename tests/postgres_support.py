"""Disposable PostgreSQL databases for tests that need a real server.

Set ENCORE_TEST_DATABASE_URL to a server URL whose user may create
databases, for example the Compose server's postgres database. Each test
gets a new empty database, which is dropped afterwards. Without the variable,
these tests are skipped.
"""

import os
import unittest
import uuid

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

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
