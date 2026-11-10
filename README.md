# SOEN 387 Encore assignments

A Flask implementation of the Encore event and waitlist API.

## Current implementation and Assignment 2 status

Events, immediate reservations, waitlist join/list/lookup/leave, and Hold
creation (`POST /events/{eventId}/holds`), inspection (`GET /holds/{holdId}`),
confirmation (`POST /holds/{holdId}/confirm`) and release
(`DELETE /holds/{holdId}`) are implemented and stored in PostgreSQL, together
with the replay outcomes of keyed requests. The
[OpenAPI contract](contracts/encore-openapi.yaml) specifies these operations.

## Assignment deliverables

Final Assignment 2 documents and selected captures belong in
`deliverables/assignment2/`. The old/new conformance output is there as
`conformance-run.txt`. The two-page design note, AI log, race script/output
and restart demonstration remain pending. No Assignment 2 submission ZIP has
been prepared.

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
and pass `--port` to use another port. Set `ENCORE_HOLD_TTL_SECONDS` to a
positive whole number to change how long a new Hold lasts (the default is 90);
any other value stops startup before the database is touched. The startup
output prints the TTL in use.

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

Run the domain checks (Event availability, the Hold lifecycle, its deadline
rounding and confirmation) and the in-memory booking block checks with:

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
They cover the migration runner (including the upgrade from migration one to
two), the PostgreSQL adapter through the service functions (row locking,
waitlist order, rollback, ID allocation, and expiry judged after a lock wait
for Hold creation, confirmation and release), durable replay (stored errors,
key conflicts, same-key contention, the 24-hour boundary on an injected clock,
and abort retries), and `app.py` started as a process: replay after a restart,
a failed migration, an invalid Hold TTL, the Hold checks in
`tests/test_postgres_holds.py`, `tests/test_postgres_confirm.py` and
`tests/test_postgres_release.py`, and the cross-workflow checks in
`tests/test_postgres_cross_workflow.py`. Those send HTTP requests through the
conformance helpers and check receipts, the current view, coded errors, live
Holds filling an event for both booking paths and the waitlist, expiry with a
2-second TTL, first and repeated confirmation at full allocation, release
of live, released, expired and confirmed Holds, and key binding and replay
across all three keyed POSTs. Without
`ENCORE_TEST_DATABASE_URL` these tests are skipped.

To check booking against a running server, including recovery when a reply is
lost, run `python3 tests/direct_booking_smoke.py` once on freshly seeded data,
after the original conformance suite. It uses both of ev_103's seats and one
of ev_101's.

Check the Hold contract and fresh-setup fixture without calling Hold handlers:

```bash
python3 -m unittest discover -s tests -p 'test_hold*.py' -v
```

Keep `tests/fixtures/` and `contracts/fixtures/` with the tests. The frozen
v1.1.0 contract, original event-check bodies, and captured event responses
are compatibility/test inputs; they are not additional active contracts.

## Conformance run

One command runs the API's conformance checks on PostgreSQL. With the Compose
server up, run:

```bash
ENCORE_TEST_DATABASE_URL=postgresql://encore:encore@localhost:5432/postgres \
  python3 tests/run_conformance.py
```

It reports the original checks apart from the Assignment 2 checks. The
original group runs the 15 checks in `tests/conformance.py`, whose check code
is unchanged, against `app.py` on a new database loaded from the fresh
fixture. The Assignment 2 group runs `tests/direct_booking_smoke.py` on that same server,
then the Hold creation, confirmation and release suites and
`tests/test_postgres_cross_workflow.py`, where each test starts `app.py` on its
own new database. The cross-workflow suite checks that one key stays bound to
its first route, event and exact body bytes across all three keyed POSTs. It
also checks that a replayed waitlist join keeps its original position while
GET reports the current one, and that stored booking and join outcomes replay
unchanged while live Holds fill an event and give seats back. A last section,
reported on its own because it does not use HTTP, compares the four original
event check bodies with their recorded copy, checks the parsed contract, and
checks the 24-hour key retention boundary on an injected clock. Restart,
migration and lock-wait checks are not part of this run; the PostgreSQL
checks above cover them.

The header lists the command and expected result, the Git revision and any
uncommitted files, the contract version and checksum, the Python, Flask,
psycopg, jsonschema and PostgreSQL versions, and the settings: Holds last 90
seconds by default and 2 seconds in the expiry checks, and a command that hits
a serialization failure or deadlock runs up to 3 times in all. Every database
is dropped at the end, so there is nothing to reset. The command exits with
status 0 only when every section passes. A captured run is in
[deliverables/assignment2/conformance-run.txt](deliverables/assignment2/conformance-run.txt).

`tests/conformance.py` and `tests/direct_booking_smoke.py` check
`http://localhost:3000` unless `ENCORE_BASE_URL` names another address.

These passes have limits as compatibility evidence. The four supplied event
checks cover events only, and the other eleven original checks cover the
waitlist. An Assignment 1 comparison with the course
starter (revision `8893a46`) found two kinds of change. Reusing a key for
another event, which the starter answered with the first event's 201, now
returns 422 `idempotency_key_reuse`. Malformed IDs, invalid query values and
extra reservation fields that the starter accepted are now rejected. That
comparison ran against the in-memory Assignment 1 version and has not been
repeated on PostgreSQL. Nothing shows that the starter is the released
v1.0.0, so these runs do not establish equivalence with v1.0.0.

## Database setup

`migrations/001_initial.sql` creates the tables for events with their
Capacity, reservations, waitlist entries, the reservation ID counter and
stored replay outcomes. `migrations/002_holds_and_reservation_link.sql` adds
the `holds` table: each Hold's fixed `created_at` and `expires_at`, a
`reservation_id` link for a confirmed Hold and a `released_at` mark. No column
records expiry. The runner in `migrate.py` applies each numbered step
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

Events, reservations, Holds, waitlist entries and the reservation ID counter
live in PostgreSQL, through `postgres_booking.py`. `seatsLeft` is never
stored: the domain Event computes it as Capacity minus stored reservations
minus live Holds, for event listing, the bookable filter, the waitlist's
`event_not_full` check, direct booking and Hold creation alike.

A Hold is live while it is unconfirmed, unreleased and the database time is
strictly before its `expiresAt`; at that instant it has expired. Expiry frees
the seat at once, without a cleanup job, because every reader judges the Hold
against the current time. `GET /holds/{holdId}` reports the current status and
`secondsRemaining`, rounded up while the Hold is live and 0 afterwards. Reads,
retries and restarts never move the deadline. Time comes from PostgreSQL's
`clock_timestamp()`. A read takes it in the first statement of its snapshot,
so the state and the time match. A command reads it after locking the event,
so a request that waited for the lock judges expiry when it runs.

`POST /holds/{holdId}/confirm` turns a live Hold into a reservation. In one
transaction it finds the Hold's event, locks it, reads the Hold again, then
stores one reservation and the Hold's `reservationId` link together. The
Hold's seat passes to the reservation, so `seatsLeft` does not change and a
full event can still confirm its Holds. The first confirmation returns 201
with the reservation; a repeat returns 200 with the same one, even after the
old deadline. An expired Hold, including one that expired while its request
waited for the lock, gives 409 `hold_expired` and changes nothing. A released
Hold gives 409 `hold_released`. Confirmation needs no `Idempotency-Key` and
ignores one if sent.

`DELETE /holds/{holdId}` releases a live Hold early. It takes the same steps
under the event's lock, then sets the Hold's `released_at` mark, which frees
its seat at once. It returns 204 with no body. Releasing a Hold that is
already released or has expired also returns 204 and writes nothing, so a
repeat cannot free a second seat and an expired Hold stays expired. A
confirmed Hold gives 409 `already_confirmed` with its `reservationId`, even
after the old deadline; release never cancels a reservation. The Hold stays
retrievable with `GET /holds/{holdId}` in every case. Release needs no
`Idempotency-Key` and ignores one if sent.

Booking, Hold creation, confirmation and release, waitlist join and waitlist
leave each lock their event's row with `SELECT ... FOR UPDATE` before reading
the state they decide on, so commands on the same event run one at a time. The
lock ends when the transaction commits or rolls back. Reads run in one read-only
`REPEATABLE READ` transaction, so a response never mixes two states. Waitlist
positions follow a join order the database assigns, independent of the random
entry IDs, and a user can appear on an event's waitlist only once. A booking
that rolls back returns its reservation number to the counter.

`memory_booking.py` implements the same repository over the dictionaries in
`storage.py`. The focused unit tests use it. Start the API with
`python3 app.py`: that entry point migrates the database and switches the
services to PostgreSQL, while other launchers such as `flask run` skip both
steps and serve from memory.
Every reservation has the fixed `createdAt` value `2026-09-12T19:00:00Z`.

## Replay of keyed requests

`POST /events/{eventId}/reservations`, `POST /events/{eventId}/waitlist` and
`POST /events/{eventId}/holds` store their outcome, including expected errors
such as `sold_out` and `invalid_request`, in the `idempotency_outcomes` table.
Each request runs in one transaction. It first claims its key with
`INSERT ... ON CONFLICT (key) DO NOTHING`, or locks the key's existing row,
before it locks any event. Then it either replays the stored outcome, rejects
a different endpoint, event or body with `idempotency_key_reuse`, or does the
work and saves the outcome. The booking, join or Hold and its outcome commit
together, so a crash cannot keep one without the other. A replayed
Hold creation returns the original receipt, with status `held`, even after the
Hold has expired; `GET /holds/{holdId}` gives its current state. A second
request with the same key waits on the key's row and then replays the first
outcome.

An outcome stays replayable for 24 hours after its request completed, measured
on the API process's UTC clock, and survives a restart. At 24 hours or later
the key starts a new attempt. Missing-key and key-reuse errors are never
stored.

A transaction that PostgreSQL aborts with a serialization failure (SQLSTATE
`40001`) or a detected deadlock (`40P01`) has been rolled back completely, so
the whole request runs again, up to 3 attempts in all with no delay. After the
third abort the API returns 503 `service_unavailable` and stores nothing;
the client should retry with the same key, endpoint and body bytes. Hold
confirmation follows the same retry rule; after its 503 the client can GET the
Hold or confirm again. Every other
database error, including a connection lost during commit, is not retried and
returns a server error, because the work may or may not have committed. The
client recovers the same way: the same key replays the outcome if the work
committed, or runs it once if it did not. No statement, lock or connection
timeout is set. The v1.1 operation entries in the contract do not list the 503
response yet; the Hold operations do.
