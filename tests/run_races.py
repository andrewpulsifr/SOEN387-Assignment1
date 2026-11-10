"""Run the Assignment 2 race checks on PostgreSQL and print what each one saw.

Runs the three tests in tests/test_postgres_races.py, each against app.py on
a new database loaded from the fresh fixture, and prints every response and
the final state of ev_103 under each test. The header, shared with
tests/run_conformance.py, records the command, revision, versions and
settings. Databases are dropped afterwards, so there is nothing to reset.

Run from the project root with the Compose PostgreSQL server up:
    ENCORE_TEST_DATABASE_URL=postgresql://encore:encore@localhost:5432/postgres \\
        python3 tests/run_races.py
It exits with status 0 only if all three races pass.
"""

import logging
import sys
import unittest
from pathlib import Path

# The suites import the application modules from the project root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from run_conformance import counts, passed, print_header, run_tests, server_url_missing
from test_postgres_races import HOLD_TTL_SECONDS


def main():
    """Print the header, run the races with their logged responses, and return the exit status."""
    if server_url_missing():
        return 2
    print_header("Encore race run on PostgreSQL",
                 f"{HOLD_TTL_SECONDS} seconds in every race, longer than a whole run;"
                 " a race fails if its requests have not all been answered within"
                 " 10 seconds")
    show_race_log()

    print("\n== Races: tests/test_postgres_races.py, one new database per test ==",
          flush=True)
    result = run_tests(["test_postgres_races"], resultclass=ResultWithRaceLog)

    print("\n== Summary ==")
    print(f"Race tests: {counts(result)}")
    return 0 if passed(result) else 1


class ResultWithRaceLog(unittest.TextTestResult):
    """Verbose test result that ends each test's description line before its race log."""

    def startTest(self, test):
        """Print the test's description as usual, then end the line."""
        super().startTest(test)
        self.stream.writeln()


def show_race_log():
    """Print the races' logged responses and final states, indented, on standard output."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("    %(message)s"))
    races = logging.getLogger("encore.races")
    races.addHandler(handler)
    races.setLevel(logging.INFO)


if __name__ == "__main__":
    sys.exit(main())
