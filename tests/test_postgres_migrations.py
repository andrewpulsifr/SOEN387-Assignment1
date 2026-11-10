"""Migration runner checks against isolated PostgreSQL databases.

Checks that a fresh database gets both migrations and their ledger rows, that
a database at migration one upgrades to two and keeps its rows, that a rerun
changes nothing, that a failed step leaves neither its tables nor its ledger
row, and that two concurrent migrators apply each step once.

Run from the project root with ENCORE_TEST_DATABASE_URL set:
    python3 -m unittest discover -s tests -p 'test_postgres_migrations.py' -v
"""

import shutil
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import psycopg
from postgres_support import PostgresTestCase, requires_postgres

import migrate

ALL_STEPS = ["001_initial.sql", "002_holds_and_reservation_link.sql"]
LEDGER = [(1, ALL_STEPS[0]), (2, ALL_STEPS[1])]


@requires_postgres
class MigrationRunnerTests(PostgresTestCase):
    def ledger(self):
        """Return the recorded (version, name) rows in version order."""
        return self.query("SELECT version, name FROM schema_migrations ORDER BY version")

    def test_fresh_database_applies_both_migrations_in_order(self):
        applied = migrate.migrate(self.database_url)

        self.assertEqual(applied, ALL_STEPS)
        self.assertEqual(self.ledger(), LEDGER)
        tables = {name for (name,) in self.query(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
        )}
        self.assertLessEqual(
            {"events", "reservations", "waitlist_entries", "id_counters",
             "idempotency_outcomes", "holds"},
            tables,
        )

    def test_upgrade_from_migration_one_keeps_rows_and_links_holds(self):
        """Migration two adds Holds beside A1 state that migration one stored."""
        with tempfile.TemporaryDirectory() as directory:
            shutil.copy(migrate.MIGRATIONS_DIR / ALL_STEPS[0], directory)
            self.assertEqual(migrate.migrate(self.database_url, Path(directory)),
                             ALL_STEPS[:1])
        self.execute("""
            INSERT INTO events VALUES ('ev_7', 'Kept', 3);
            INSERT INTO reservations VALUES ('res_1', 'ev_7', 'confirmed', 'x');
            INSERT INTO waitlist_entries (id, event_id, user_id, created_at)
                VALUES ('wl_1', 'ev_7', 'ana', 'y');
            UPDATE id_counters SET next_value = 2;
            INSERT INTO idempotency_outcomes VALUES
                ('key-1', 'POST /events/ev_7/reservations', 'h', 201, '{}', now());
        """)

        self.assertEqual(migrate.migrate(self.database_url), ALL_STEPS[1:])
        self.assertEqual(self.ledger(), LEDGER)
        self.assertEqual(self.query(
            "SELECT (SELECT count(*) FROM events), (SELECT count(*) FROM reservations),"
            " (SELECT count(*) FROM waitlist_entries), (SELECT next_value FROM id_counters),"
            " (SELECT count(*) FROM idempotency_outcomes), (SELECT count(*) FROM holds)"
        ), [(1, 1, 1, 2, 1, 0)])
        self.execute("""
            INSERT INTO holds (id, event_id, created_at, expires_at, reservation_id)
                VALUES ('hold_1', 'ev_7', now(), now() + interval '90 seconds', 'res_1');
        """)
        self.assertEqual(self.query("SELECT reservation_id FROM holds"), [("res_1",)])

    def test_rerun_applies_nothing_and_keeps_existing_rows(self):
        migrate.migrate(self.database_url)
        with psycopg.connect(self.database_url) as conn:
            conn.execute("INSERT INTO events VALUES ('ev_500', 'Kept', 3)")

        self.assertEqual(migrate.migrate(self.database_url), [])
        self.assertEqual(self.ledger(), LEDGER)
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

        self.assertEqual(results, [[], ALL_STEPS])
        self.assertEqual(self.ledger(), LEDGER)


if __name__ == "__main__":
    unittest.main()
