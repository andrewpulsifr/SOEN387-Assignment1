"""Run the original conformance checks and the Assignment 2 checks on PostgreSQL, reported apart.

The original group runs the checks in tests/conformance.py against app.py on
a new database loaded from the fresh fixture. The Assignment 2 group runs
tests/direct_booking_smoke.py on that server, then the Hold and cross-workflow
HTTP suites, which start app.py on a new database per test. A last section
runs checks without HTTP: the original check bodies against their recorded
copy, the parsed contract, and 24-hour key retention on an injected clock.
Databases are dropped afterwards, so there is nothing to reset. The header
records the command, revision, versions and settings. tests/run_races.py
reuses the header and test-running helpers.

Run from the project root with the Compose PostgreSQL server up:
    ENCORE_TEST_DATABASE_URL=postgresql://encore:encore@localhost:5432/postgres \\
        python3 tests/run_conformance.py
It exits with status 0 only if every section passes.
"""

import hashlib
import os
import platform
import subprocess
import sys
import unittest
from importlib.metadata import version
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# The suites import the application modules from the project root.
sys.path.insert(0, str(ROOT))

import psycopg  # noqa: E402
import yaml  # noqa: E402
from postgres_support import ADMIN_URL, create_database, drop_database, serving_app  # noqa: E402

import idempotency  # noqa: E402

TESTS = ROOT / "tests"
CONTRACT = ROOT / "contracts" / "encore-openapi.yaml"
HTTP_SUITES = ["test_postgres_holds", "test_postgres_confirm", "test_postgres_release",
               "test_postgres_cross_workflow"]
NON_HTTP_CHECKS = ["test_hold_fixtures", "test_hold_contract",
                   "test_postgres_replay.PostgresReplayTests"
                   ".test_binding_expires_24_hours_after_completion"]


def main():
    """Print the header, run every section, print a summary; return the exit status."""
    if server_url_missing():
        return 2
    print_header("Encore conformance run on PostgreSQL",
                 "90 seconds (the app.py default); expiry checks start app.py"
                 " with ENCORE_HOLD_TTL_SECONDS=2")

    print("\n== Original checks: tests/conformance.py, check code unchanged ==",
          flush=True)
    database_url = create_database()
    try:
        with serving_app(database_url, "--seed-fresh") as base:
            original_passed = run_script("conformance.py", base)
            print("\n== Assignment 2 checks ==")
            print("-- Direct booking smoke check, on the same server --", flush=True)
            smoke_passed = run_script("direct_booking_smoke.py", base)
    finally:
        drop_database(database_url)

    print("\n-- Hold and cross-workflow HTTP checks, one new database per test --",
          flush=True)
    http = run_tests(HTTP_SUITES)
    print("\n== Not HTTP: original check bodies, the parsed contract, and 24-hour"
          " key retention on an injected clock ==", flush=True)
    non_http = run_tests(NON_HTTP_CHECKS)

    print("\n== Summary ==")
    print(f"Original checks: {'passed' if original_passed else 'FAILED'}")
    print(f"Assignment 2 direct booking smoke check:"
          f" {'passed' if smoke_passed else 'FAILED'}")
    print(f"Assignment 2 HTTP tests: {counts(http)}")
    print(f"Checks without HTTP: {counts(non_http)}")
    tests_passed = passed(http) and passed(non_http)
    return 0 if original_passed and smoke_passed and tests_passed else 1


def server_url_missing():
    """Return True, after saying how to set it, when ENCORE_TEST_DATABASE_URL is unset."""
    if ADMIN_URL:
        return False
    print("Set ENCORE_TEST_DATABASE_URL to a PostgreSQL server URL whose user may"
          " create databases; see the README.", file=sys.stderr)
    return True


def passed(result):
    """Return True if every test in a unittest result ran and passed."""
    return result.wasSuccessful() and not result.skipped


def print_header(title, hold_ttl):
    """Print the title, then the source revision, contract, versions and settings of this run.

    hold_ttl describes the Hold lifetimes the run uses.
    """
    contract = CONTRACT.read_bytes()
    with psycopg.connect(ADMIN_URL) as conn:
        server_version = conn.execute("SHOW server_version").fetchone()[0]
    print(title)
    print(f"Command: python3 {' '.join(sys.argv)}, with ENCORE_TEST_DATABASE_URL set")
    print("Expected: every check passes and the command exits with status 0")
    print(f"Source: {git('rev-parse', '--short', 'HEAD') or 'not a Git checkout'}")
    for line in git("status", "--short", "--untracked-files=all").splitlines():
        print(f"  uncommitted: {line}")
    print(f"Contract: contracts/encore-openapi.yaml version"
          f" {yaml.safe_load(contract)['info']['version']},"
          f" SHA-256 {hashlib.sha256(contract).hexdigest()}")
    print(f"Python {platform.python_version()}, Flask {version('flask')},"
          f" psycopg {version('psycopg')}, jsonschema {version('jsonschema')},"
          f" PostgreSQL {server_version}")
    print(f"Hold TTL: {hold_ttl}")
    print(f"Retries: up to {idempotency.MAX_COMMAND_ATTEMPTS} attempts per command"
          " after SQLSTATE 40001 or 40P01, with no delay")
    print("Fixtures: every server runs on a new database loaded from"
          " contracts/fixtures/fresh-events.json, dropped afterwards")


def git(*args):
    """Return git's output for args in the project root, or "" if git fails."""
    result = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True,
                            check=False)
    # rstrip only: a status line's leading space marks an unstaged change.
    return result.stdout.rstrip("\n") if result.returncode == 0 else ""


def run_tests(names, resultclass=None):
    """Run the named unittest modules or tests verbosely; return the result.

    resultclass, if given, replaces unittest's TextTestResult.
    """
    tests = unittest.defaultTestLoader.loadTestsFromNames(names)
    return unittest.TextTestRunner(stream=sys.stdout, verbosity=2,
                                   resultclass=resultclass).run(tests)


def counts(result):
    """Describe a unittest result as run, failed, error and skipped counts."""
    return (f"{result.testsRun} run, {len(result.failures)} failed,"
            f" {len(result.errors)} errors, {len(result.skipped)} skipped")


def run_script(name, base):
    """Run tests/<name> against the server at base, showing its output.

    Returns True if the script exited with status 0.
    """
    result = subprocess.run([sys.executable, str(TESTS / name)], check=False,
                            env={**os.environ, "ENCORE_BASE_URL": base})
    return result.returncode == 0


if __name__ == "__main__":
    sys.exit(main())
