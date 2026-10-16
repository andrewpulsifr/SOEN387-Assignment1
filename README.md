# SOEN 387 Assignment 1

A Flask implementation of the Encore event and waitlist API.

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
memory. Restarting the server clears them, and separate server processes do not
share them. The contract promises at least 24 hours of key retention, but this
implementation does not establish restart-safe retention.
