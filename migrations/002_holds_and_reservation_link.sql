-- Holds: one provisional seat each, with a deadline fixed at creation.
-- No column records expiry. The application decides whether a Hold is live
-- from expires_at and the database clock, so nothing has to clean up.
-- reservation_id links a confirmed Hold to the Reservation that replaced it;
-- released_at marks an early release. A Hold cannot be both.

CREATE TABLE holds (
    id             TEXT PRIMARY KEY,
    event_id       TEXT NOT NULL REFERENCES events (id),
    created_at     TIMESTAMPTZ NOT NULL,
    expires_at     TIMESTAMPTZ NOT NULL,
    reservation_id TEXT UNIQUE REFERENCES reservations (id),
    released_at    TIMESTAMPTZ,
    CHECK (created_at < expires_at),
    CHECK (reservation_id IS NULL OR released_at IS NULL)
);
CREATE INDEX holds_event_id_idx ON holds (event_id);
