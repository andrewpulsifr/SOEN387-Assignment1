"""Migration runner checks against isolated PostgreSQL databases.

Checks that a fresh database gets migration one and its ledger row, that a
rerun changes nothing, that a failed step leaves neither its tables nor its
ledger row, and that two concurrent migrators apply each step once.

Run from the project root with ENCORE_TEST_DATABASE_URL set:
    python3 -m unittest discover -s tests -p 'test_postgres_migrations.py' -v
"""

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import psycopg
from postgres_support import PostgresTestCase, requires_postgres

import migrate


@requires_postgres
class MigrationRunnerTests(PostgresTestCase):
    def ledger(self):
        """Return the recorded (version, name) rows in version order."""
        return self.query("SELECT version, name FROM schema_migrations ORDER BY version")

    def test_fresh_database_applies_first_migration_and_records_it(self):
        applied = migrate.migrate(self.database_url)

        self.assertEqual(applied, ["001_initial.sql"])
        self.assertEqual(self.ledger(), [(1, "001_initial.sql")])
        tables = {name for (name,) in self.query(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
        )}
        self.assertLessEqual(
            {"events", "reservations", "waitlist_entries", "id_counters",
             "idempotency_outcomes"},
            tables,
        )

    def test_rerun_applies_nothing_and_keeps_existing_rows(self):
        migrate.migrate(self.database_url)
        with psycopg.connect(self.database_url) as conn:
            conn.execute("INSERT INTO events VALUES ('ev_500', 'Kept', 3)")

        self.assertEqual(migrate.migrate(self.database_url), [])
        self.assertEqual(self.ledger(), [(1, "001_initial.sql")])
        self.assertEqual(self.query("SELECT id, title, capacity FROM events"),
                         [("ev_500", "Kept", 3)])

    def test_failed_step_leaves_neither_its_changes_nor_its_ledger_row(self):
        with tempfile.TemporaryDirectory() as directory:
            steps = Path(directory)
            (steps / "001_first.sql").write_text("CREATE TABLE first_step (id INT);")
            (steps / "002_broken.sql").write_text(
                "CREATE TABLE broken_step (id INT); SELECT 1 / 0;"
            )
            with self.assertRaises(psycopg.errors.DivisionByZero):
                migrate.migrate(self.database_url, steps)

        self.assertEqual(self.ledger(), [(1, "001_first.sql")])
        self.assertEqual(self.query("SELECT to_regclass('first_step') IS NOT NULL, "
                                    "to_regclass('broken_step') IS NULL"),
                         [(True, True)])

    def test_concurrent_migrators_apply_each_step_once(self):
        start = Barrier(2)

        def run(_):
            start.wait(timeout=5)
            return migrate.migrate(self.database_url)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = sorted(pool.map(run, range(2)))

        self.assertEqual(results, [[], ["001_initial.sql"]])
        self.assertEqual(self.ledger(), [(1, "001_initial.sql")])


if __name__ == "__main__":
    unittest.main()
