# SOEN 387 Encore assignments

A Flask implementation of the Encore event and waitlist API.

## Current implementation and Assignment 2 status

Events, immediate reservations, and waitlist join/list/lookup/leave are
implemented. The [OpenAPI contract](contracts/encore-openapi.yaml) also specifies
planned Assignment 2 Hold operations; those handlers, PostgreSQL persistence,
migrations, and runtime TTL configuration are not implemented yet. Contract
tests passing does not demonstrate Hold HTTP behavior, locking, or durability.

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

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
python3 app.py
```

With the server running, use a second terminal to run:

```bash
python3 tests/conformance.py
```

Run focused idempotency checks with:

```bash
python3 -m unittest discover -s tests -p 'test_idempotency.py' -v
```

Check the planned Hold contract and fresh-setup fixture without calling Hold
handlers:

```bash
python3 -m unittest discover -s tests -p 'test_hold*.py' -v
```

Keep `tests/fixtures/` and `contracts/fixtures/` with the tests. The frozen
v1.1.0 contract, original event-check bodies, and captured event responses
are compatibility/test inputs; they are not additional active contracts.

## Planned database setup

[fresh-events.json](contracts/fixtures/fresh-events.json) is an explicit future
seeding input, not a reset command or an existing PostgreSQL integration. It
defines capacities 42, 0, and 2 for ev_101, ev_102, and ev_103, empty
Reservations/Holds/waitlists/replay outcomes, and Reservation numbering from 1.
Capacity zero deliberately represents the full event without inventing past
bookings from remaining-seat counts.

Future setup must seed only an explicitly fresh, empty database after the
required schema exists. Normal startup must apply pending migrations, stop
on migration failure, and preserve populated data, IDs, receipts, and deadlines.
The test runner must own isolated disposable data. Restart demonstrations
must reuse the populated database with seeding disabled. Database dependencies
and executable setup/migration/start commands will be documented with their
implementation; no database command is available in this prerequisite slice.

## Storage limits

Events, reservations, waitlist entries, and idempotency outcomes live in process
memory. Idempotency outcomes are lazily retained for 24 hours within the
running process. Restarting the server clears them, and separate server
processes do not share them, so this implementation does not provide durable
24-hour retention across restarts or multiple workers.
