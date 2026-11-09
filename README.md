# SOEN 387 Encore assignments

A Flask implementation of the Encore event and waitlist API.

## Current implementation and Assignment 2 status

Events, immediate reservations, and waitlist join/list/lookup/leave are
implemented and stored in PostgreSQL. The
[OpenAPI contract](contracts/encore-openapi.yaml) also specifies planned
Assignment 2 Hold operations; those handlers, durable replay of keyed requests,
and runtime TTL configuration are not implemented yet. Contract tests passing
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
functions (row locking, waitlist order, rollback and ID allocation), and
`app.py` started as a process, including a restart and a failed migration.
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

Idempotency outcomes live in process memory for 24 hours. A restart
clears them, and separate server processes do not share them. After a
restart, a retry with the same key runs again: a reservation retry books
another seat if one is free, and a waitlist join retry returns
`already_on_waitlist`. The `idempotency_outcomes` table exists for durable
replay but is not used yet. Holds are future work.
