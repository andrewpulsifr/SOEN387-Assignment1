# SOEN 387 Assignment 1

A Flask implementation of the Encore event and waitlist API.

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

## Storage limits

Events, reservations, waitlist entries, and idempotency outcomes live in process
memory. Idempotency outcomes are lazily retained for 24 hours within the
running process. Restarting the server clears them, and separate server
processes do not share them, so this implementation does not provide durable
24-hour retention across restarts or multiple workers.
