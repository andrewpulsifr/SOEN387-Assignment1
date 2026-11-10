# SOEN 387 Encore assignments

A Flask implementation of the Encore event and waitlist API.

## Current implementation and Assignment 2 status

Events, immediate reservations, and waitlist join/list/lookup/leave are
implemented and stored in PostgreSQL, together with the replay outcomes of
keyed requests. The [OpenAPI contract](contracts/encore-openapi.yaml) also
specifies planned Assignment 2 Hold operations; those handlers and runtime TTL
configuration are not implemented yet. Contract tests passing
does not demonstrate Hold HTTP behavior.

## Assignment deliverables

Final Assignment 2 documents and selected captures belong in
`deliverables/assignment2/` once produced. They remain pending, including the
two-page design note, AI log, old/new conformance output, race script/output,
and restart demonstration. No Assignment 2 submission ZIP has been prepared.

## Review map

Review the root Python modules and `requirements.txt` for the application,
`contracts/` for the active API contract and setup fixtures, `tests/` for
executable checks and their inputs, and `deliverables/assignment2/` for final
Assignment 2 documents and evidence once produced.

`_internal/` holds local plans, policies, journals, tools, scratch work, and
historical Assignment 1 deliverables. Exclude it from the Assignment 2 ZIP.
Root agent instructions, `CONTEXT.md`, editor/agent configuration, `.gitignore`,
and local environments remain at locations required by their tools. They are
outside the grading package. The internal guide explains the archived paths.

## Run

The API needs PostgreSQL. `compose.yaml` runs PostgreSQL 17 in Docker with a
local `encore` user and database, reachable only from this machine. Its login
is a fixed development value for that throwaway container.

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
docker compose up -d --wait
python3 app.py --seed-fresh
```

`app.py` applies pending migrations from `migrations/` before it serves
requests on port 3000, and exits with an error if a migration fails.
`--seed-fresh` loads [fresh-events.json](contracts/fixtures/fresh-events.json)
only into a database that has no events, so repeating it changes nothing.
Later starts can use `python3 app.py`. Set `ENCORE_DATABASE_URL` to use another
database (the default is `postgresql://encore:encore@localhost:5432/encore`)
and pass `--port` to use another port.

With the server running, use a second terminal to run:

```bash
python3 tests/conformance.py
```

The conformance and smoke checks expect freshly seeded data, such as an empty
ev_103 waitlist. To start over, remove the database volume and seed again:

```bash
docker compose down -v
docker compose up -d --wait
python3 app.py --seed-fresh
```

Run focused idempotency checks with:

```bash
python3 -m unittest discover -s tests -p 'test_idempotency.py' -v
```

Run the direct-booking domain and in-memory booking block checks with:

```bash
python3 -m unittest discover -s tests -p 'test_booking*.py' -v
```

Booking and waitlist commands each run in one atomic block. Adapters provide
`unit_of_work()`, which opens the block and yields the repository used to load
the event, allocate an ID and save changes; if the block raises, it leaves no
trace. `snapshot()` opens a read-only block for event, reservation and waitlist
reads. These checks use the in-memory adapter, so they say nothing about
database persistence or locking.

Run the PostgreSQL checks against the Compose server with:

```bash
ENCORE_TEST_DATABASE_URL=postgresql://encore:encore@localhost:5432/postgres \
  python3 -m unittest discover -s tests -p 'test_postgres*.py' -v
```

Each test creates its own database on that server and drops it afterwards.
They cover the migration runner, the PostgreSQL adapter through the service
functions (row locking, waitlist order, rollback and ID allocation), durable
replay (stored errors, key conflicts, same-key contention, the 24-hour
boundary on an injected clock, and abort retries), and `app.py` started as a
process, including replay after a restart and a failed migration.
Without `ENCORE_TEST_DATABASE_URL` these tests are skipped.

To check booking against a running server, including recovery when a reply is
lost, run `python3 tests/direct_booking_smoke.py` once on freshly seeded data,
after the original conformance suite. It uses both of ev_103's seats and one
of ev_101's.

Check the planned Hold contract and fresh-setup fixture without calling Hold
handlers:

```bash
python3 -m unittest discover -s tests -p 'test_hold*.py' -v
```

Keep `tests/fixtures/` and `contracts/fixtures/` with the tests. The frozen
v1.1.0 contract, original event-check bodies, and captured event responses
are compatibility/test inputs; they are not additional active contracts.

## Database setup

`migrations/001_initial.sql` creates the tables for events with their
Capacity, reservations, waitlist entries, the reservation ID counter and
stored replay outcomes. The runner in `migrate.py` applies each numbered step
once, in order, and records it in `schema_migrations` in the same transaction,
so a failed step leaves neither its changes nor its ledger row. A PostgreSQL
advisory lock lets only one migrator run at a time.

[fresh-events.json](contracts/fixtures/fresh-events.json) defines capacities
42, 0, and 2 for ev_101, ev_102, and ev_103, empty
Reservations/Holds/waitlists/replay outcomes, and Reservation numbering from 1.
Capacity zero represents the full event without inventing past bookings from
remaining-seat counts. Normal starts never seed, so a restarted server keeps
its data and IDs.

## Storage

Events, reservations, waitlist entries and the reservation ID counter live in
PostgreSQL, through `postgres_booking.py`. `seatsLeft` is never stored: the
domain Event computes it from Capacity and the number of stored reservations
for event listing, the bookable filter and the waitlist's `event_not_full`
check alike.

Booking, waitlist join and waitlist leave each lock their event's row with
`SELECT ... FOR UPDATE` at the start of their transaction, so commands on the
same event run one at a time. The lock ends when the transaction commits or
rolls back. Reads run in one read-only `REPEATABLE READ` transaction, so a
response never mixes two states. Waitlist positions follow a join order the
database assigns, independent of the random entry IDs, and a user can appear on
an event's waitlist only once. A booking that rolls back returns its
reservation number to the counter.

`memory_booking.py` implements the same repository over the dictionaries in
`storage.py`. The focused unit tests use it. Start the API with
`python3 app.py`: that entry point migrates the database and switches the
services to PostgreSQL, while other launchers such as `flask run` skip both
steps and serve from memory.
Every reservation has the fixed `createdAt` value `2026-09-12T19:00:00Z`.

## Replay of keyed requests

`POST /events/{eventId}/reservations` and `POST /events/{eventId}/waitlist`
store their outcome, including expected errors such as `sold_out` and
`invalid_request`, in the `idempotency_outcomes` table. Each request runs in
one transaction. It first claims its key with
`INSERT ... ON CONFLICT (key) DO NOTHING`, or locks the key's existing row,
before it locks any event. Then it either replays the stored outcome, rejects
a different endpoint, event or body with `idempotency_key_reuse`, or does the
work and saves the outcome. The booking or join and its outcome commit
together, so a crash cannot keep one without the other. A second request with
the same key waits on the key's row and then replays the first outcome.

An outcome stays replayable for 24 hours after its request completed, measured
on the API process's UTC clock, and survives a restart. At 24 hours or later
the key starts a new attempt. Missing-key and key-reuse errors are never
stored.

A transaction that PostgreSQL aborts with a serialization failure (SQLSTATE
`40001`) or a detected deadlock (`40P01`) has been rolled back completely, so
the whole request runs again, up to 3 attempts in all with no delay. After the
third abort the API returns 503 `service_unavailable` and stores nothing;
the client should retry with the same key, endpoint and body bytes. Every other
database error, including a connection lost during commit, is not retried and
returns a server error, because the work may or may not have committed. The
client recovers the same way: the same key replays the outcome if the work
committed, or runs it once if it did not. No statement, lock or connection
timeout is set. The v1.1 operation entries in the contract do not list the 503
response yet. Holds are future work.
