"""Run the Assignment 2 race and lock-wait checks on PostgreSQL and print what each saw.

Runs the four races in tests/test_postgres_races.py and the lock-wait check
in tests/test_postgres_lock_wait.py, each against app.py on a new database
loaded from the fresh fixture. Under each test it prints every response and
the final state of ev_103; the lock-wait check also prints the stored
deadline and when it saw the confirmation waiting. The header, shared with
tests/run_conformance.py, records the command, revision, versions and
settings. Databases are dropped afterwards, so there is nothing to reset.

Run from the project root with the Compose PostgreSQL server up:
    ENCORE_TEST_DATABASE_URL=postgresql://encore:encore@localhost:5432/postgres \\
        python3 tests/run_races.py
It exits with status 0 only if every check passes.
"""

import sys
from pathlib import Path

# The suites import the application modules from the project root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from run_conformance import (ResultWithLog, counts, passed, print_header, run_tests,
                             server_url_missing, show_log)
from test_postgres_lock_wait import HOLD_TTL_SECONDS as LOCK_WAIT_TTL_SECONDS
from test_postgres_races import HOLD_TTL_SECONDS


def main():
    """Print the header, run the checks with their logged observations; return the exit status."""
    if server_url_missing():
        return 2
    print_header("Encore race run on PostgreSQL",
                 f"{HOLD_TTL_SECONDS} seconds in every race, longer than a whole run;"
                 " a race fails if its requests have not all been answered within"
                 f" 10 seconds. {LOCK_WAIT_TTL_SECONDS} seconds in the lock-wait check,"
                 " so its Hold expires while the confirmation waits")
    show_log("encore.races")

    print("\n== Races: tests/test_postgres_races.py, one new database per test ==",
          flush=True)
    races = run_tests(["test_postgres_races"], resultclass=ResultWithLog)
    print("\n== Lock wait: tests/test_postgres_lock_wait.py, on its own new database ==",
          flush=True)
    lock_wait = run_tests(["test_postgres_lock_wait"], resultclass=ResultWithLog)

    print("\n== Summary ==")
    print(f"Race tests: {counts(races)}")
    print(f"Lock-wait test: {counts(lock_wait)}")
    return 0 if passed(races) and passed(lock_wait) else 1


if __name__ == "__main__":
    sys.exit(main())
