-- Durable state for events, reservations, the waitlist, reservation ID
-- allocation and stored replay outcomes.

CREATE TABLE events (
    id       TEXT PRIMARY KEY,
    title    TEXT NOT NULL,
    capacity INTEGER NOT NULL CHECK (capacity >= 0)
);

CREATE TABLE reservations (
    id         TEXT PRIMARY KEY,
    event_id   TEXT NOT NULL REFERENCES events (id),
    status     TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX reservations_event_id_idx ON reservations (event_id);

-- join_order keeps first-come, first-served order independent of the random
-- entry IDs. Positions are counted from it when entries are read.
CREATE TABLE waitlist_entries (
    id         TEXT PRIMARY KEY,
    event_id   TEXT NOT NULL REFERENCES events (id),
    user_id    TEXT NOT NULL,
    created_at TEXT NOT NULL,
    join_order BIGINT GENERATED ALWAYS AS IDENTITY,
    UNIQUE (event_id, user_id)
);
CREATE INDEX waitlist_entries_event_order_idx
    ON waitlist_entries (event_id, join_order);

-- next_value is the number the next reservation ID uses (res_<n>).
CREATE TABLE id_counters (
    name       TEXT PRIMARY KEY,
    next_value BIGINT NOT NULL CHECK (next_value >= 1)
);
INSERT INTO id_counters (name, next_value) VALUES ('reservation', 1);

-- Stored outcomes for keyed POST requests, for durable replay. The
-- application does not use this table yet. The outcome columns are nullable
-- so a request can claim its key before its outcome is known.
CREATE TABLE idempotency_outcomes (
    key            TEXT PRIMARY KEY,
    request_target TEXT NOT NULL,
    request_hash   TEXT NOT NULL,
    status         INTEGER,
    body           JSONB,
    completed_at   TIMESTAMPTZ
);
