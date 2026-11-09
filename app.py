"""Flask application and the start command for the Encore API.

Run python3 app.py to serve the API on PostgreSQL. Other launchers import the
app without running main(), so they skip migrations and use process memory.
"""

import argparse
import os
import sys
from pathlib import Path

import psycopg
from flask import Flask, send_from_directory

import migrate
import storage
from postgres_booking import PostgresBookingCommands
from routes import api_routes

DEFAULT_DATABASE_URL = "postgresql://encore:encore@localhost:5432/encore"

app = Flask(__name__)
HERE = Path(__file__).resolve().parent

app.register_blueprint(api_routes)


@app.get("/openapi.yaml")
def openapi_yaml():
    return send_from_directory(HERE / "contracts", "encore-openapi.yaml", mimetype="text/yaml")


@app.get("/apidocs")
@app.get("/apidocs/")
def apidocs():
    return """
<!doctype html>
<html lang="en">
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>Encore API Docs</title>
        <link rel="stylesheet" href="https://unpkg.com/swagger-ui-dist@5/swagger-ui.css">
    </head>
    <body>
        <div id="swagger-ui"></div>
        <script src="https://unpkg.com/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
        <script>
            window.ui = SwaggerUIBundle({
                url: '/openapi.yaml',
                dom_id: '#swagger-ui',
                deepLinking: true,
                presets: [SwaggerUIBundle.presets.apis],
                layout: 'BaseLayout'
            });
        </script>
    </body>
</html>
"""


def _parse_args(argv):
    """Read --port (default 3000) and --seed-fresh from the command line."""
    parser = argparse.ArgumentParser(description="Run the Encore API on PostgreSQL.")
    parser.add_argument("--port", type=int, default=3000)
    parser.add_argument(
        "--seed-fresh", action="store_true",
        help="load contracts/fixtures/fresh-events.json if the database has no events",
    )
    return parser.parse_args(argv)


def _prepare_database(database_url, seed_fresh):
    """Apply pending migrations, seed if asked, and print what happened.

    The fresh fixture loads only when seed_fresh is set and the database has
    no events. A psycopg.Error prints to stderr and exits with status 1.
    """
    try:
        applied = migrate.migrate(database_url)
        # seed_fresh() returns True if it inserted events, False if the
        # database already had data.
        seeded = migrate.seed_fresh(database_url) if seed_fresh else False
    except psycopg.Error as error:
        print(f"Startup stopped: could not prepare the database: {error}",
              file=sys.stderr)
        raise SystemExit(1) from error
    print(f"Migrations applied: {', '.join(applied) or 'none pending'}", flush=True)
    if seed_fresh:
        print("Fresh fixture loaded" if seeded
              else "Database already has events; seed skipped", flush=True)


def main(argv=None):
    """Migrate the database, seed it if asked, then serve the API on PostgreSQL.

    ENCORE_DATABASE_URL selects the database (default: the local `encore`
    database). A database error while migrating or seeding stops startup with
    exit status 1 before the server starts.
    """
    args = _parse_args(argv)
    database_url = os.environ.get("ENCORE_DATABASE_URL", DEFAULT_DATABASE_URL)
    _prepare_database(database_url, args.seed_fresh)

    # Replace the in-memory default in storage.py; services read this global on every call.
    storage.BOOKING_COMMANDS = PostgresBookingCommands(database_url)
    app.run(port=args.port, threaded=True)


if __name__ == "__main__":
    main()
