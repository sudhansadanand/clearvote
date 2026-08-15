-- eligibility.db : who may vote, and how much of their entitlement is used up.
-- This database knows identities. It never knows choices.

CREATE TABLE IF NOT EXISTS agms (
    agm_id       TEXT PRIMARY KEY,
    title        TEXT NOT NULL,
    agm_date     TEXT NOT NULL,
    location     TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL CHECK (status IN
                   ('SETUP','REGISTRATION_OPEN','VOTING_IN_PROGRESS','FINALIZED','ARCHIVED')),
    config_json  TEXT NOT NULL,
    config_hash  TEXT NOT NULL,
    is_demo      INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS apartments (
    apartment_id        TEXT PRIMARY KEY,
    eligible            INTEGER NOT NULL DEFAULT 1,
    owner_display_name  TEXT NOT NULL DEFAULT '',
    eligibility_notes   TEXT,
    eligibility_version INTEGER NOT NULL DEFAULT 1,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS attendees (
    attendee_id  TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS representations (
    representation_id TEXT PRIMARY KEY,
    apartment_id      TEXT NOT NULL REFERENCES apartments(apartment_id),
    attendee_id       TEXT NOT NULL REFERENCES attendees(attendee_id),
    rep_type          TEXT NOT NULL CHECK (rep_type IN ('OWN','PROXY')),
    proxy_ref         TEXT,
    status            TEXT NOT NULL CHECK (status IN ('ACTIVE','REVOKED','SUPERSEDED')),
    created_at        TEXT NOT NULL
);

-- AC-01 lives here, in the database, not in a code path that can be forgotten:
-- an apartment can have at most one ACTIVE representation at any instant.
CREATE UNIQUE INDEX IF NOT EXISTS ux_representations_one_active
    ON representations(apartment_id) WHERE status = 'ACTIVE';

CREATE INDEX IF NOT EXISTS ix_representations_attendee ON representations(attendee_id, status);

CREATE TABLE IF NOT EXISTS credentials (
    credential_id     TEXT PRIMARY KEY,
    code_hash         TEXT NOT NULL UNIQUE,
    attendee_id       TEXT NOT NULL REFERENCES attendees(attendee_id),
    status            TEXT NOT NULL CHECK (status IN
                        ('CREATED','ACTIVE','SUSPENDED','REPLACED','CLOSED')),
    entitlement_count INTEGER NOT NULL DEFAULT 0,
    replaces          TEXT,
    issued_at         TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_credentials_attendee ON credentials(attendee_id, status);

-- The anti-overvote and idempotency record. One row per (credential, resolution),
-- created when voting opens on that resolution.
CREATE TABLE IF NOT EXISTS credential_resolution_ledger (
    credential_id       TEXT NOT NULL,
    resolution_id       TEXT NOT NULL,
    eligible_count      INTEGER NOT NULL,
    consumed_count      INTEGER NOT NULL DEFAULT 0,
    last_submission_id  TEXT,
    last_receipt_id     TEXT,
    last_recorded_count INTEGER,
    PRIMARY KEY (credential_id, resolution_id),
    CHECK (consumed_count >= 0),
    CHECK (consumed_count <= eligible_count)   -- AC-02, enforced by the database
);

CREATE INDEX IF NOT EXISTS ix_ledger_resolution ON credential_resolution_ledger(resolution_id);

CREATE TABLE IF NOT EXISTS sessions (
    session_id_hash TEXT PRIMARY KEY,
    credential_id   TEXT NOT NULL REFERENCES credentials(credential_id),
    created_at      TEXT NOT NULL,
    expires_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_sessions_credential ON sessions(credential_id);

CREATE TABLE IF NOT EXISTS operators (
    operator_id   TEXT PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE,
    role          TEXT NOT NULL CHECK (role IN ('ADMIN','REGISTRATION','MC','SCRUTINEER')),
    password_hash TEXT NOT NULL,
    salt          TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS operator_sessions (
    session_id_hash TEXT PRIMARY KEY,
    operator_id     TEXT NOT NULL REFERENCES operators(operator_id),
    csrf_token      TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    last_seen_at    TEXT NOT NULL,
    expires_at      TEXT NOT NULL
);

-- Append-only. No UPDATE/DELETE path exists in the application.
CREATE TABLE IF NOT EXISTS registration_events (
    event_id    TEXT PRIMARY KEY,
    event_type  TEXT NOT NULL,
    subject_id  TEXT,
    operator_id TEXT,
    reason      TEXT,
    timestamp   TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS registration_events_no_update
BEFORE UPDATE ON registration_events
BEGIN SELECT RAISE(ABORT, 'registration_events are append-only'); END;

CREATE TRIGGER IF NOT EXISTS registration_events_no_delete
BEFORE DELETE ON registration_events
BEGIN SELECT RAISE(ABORT, 'registration_events are append-only'); END;

-- Join rate limiting (§2). Keyed on client IP only; never on credential.
CREATE TABLE IF NOT EXISTS join_attempts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    client_key TEXT NOT NULL,
    attempt_at TEXT NOT NULL,
    ok         INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_join_attempts ON join_attempts(client_key, attempt_at);
