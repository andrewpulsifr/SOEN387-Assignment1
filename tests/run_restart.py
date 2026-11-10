"""Run the restart and migration checks on PostgreSQL and print what the restart kept.

Runs tests/test_postgres_restart.py and prints the values it saw before and
after one restart. Then runs tests/test_postgres_startup.py, for bookings and
the waitlist across a killed process and for startup stopped by a failed
migration or an invalid Hold TTL, and tests/test_postgres_migrations.py, for
the migration runner. The header is shared with tests/run_conformance.py.
Each test owns a new database, dropped afterwards.

Run from the project root with the Compose PostgreSQL server up:
    ENCORE_TEST_DATABASE_URL=postgresql://encore:encore@localhost:5432/postgres \\
        python3 tests/run_restart.py
It exits with status 0 only if every check passes.
"""

import sys
from pathlib import Path

# The suites import the application modules from the project root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from run_conformance import (ResultWithLog, counts, passed, print_header, run_tests,
                             server_url_missing, show_log)


def main():
    """Print the header, run the three sections with the restart log; return the exit status."""
    if server_url_missing():
        return 2
    print_header("Encore restart and migration run on PostgreSQL",
                 "90 seconds (the app.py default); the restart test waits one"
                 " second between kill and restart")
    print("Restart: app.py is killed, then started again on the same database without"
          " --seed-fresh; nothing is reset or reseeded in between")
    show_log("encore.restart")

    print("\n== Restart: tests/test_postgres_restart.py, one database kept across"
          " one restart ==", flush=True)
    restart = run_tests(["test_postgres_restart"], resultclass=ResultWithLog)
    print("\n== Startup: tests/test_postgres_startup.py, one new database per test ==",
          flush=True)
    startup = run_tests(["test_postgres_startup"])
    print("\n== Migration runner: tests/test_postgres_migrations.py, one new empty"
          " database per test ==", flush=True)
    migrations = run_tests(["test_postgres_migrations"])

    print("\n== Summary ==")
    print(f"Restart test: {counts(restart)}")
    print(f"Startup tests: {counts(startup)}")
    print(f"Migration runner tests: {counts(migrations)}")
    return 0 if passed(restart) and passed(startup) and passed(migrations) else 1


if __name__ == "__main__":
    sys.exit(main())
