-- ballot.db : resolutions, anonymous ballots, frozen results.
-- This database knows choices. It must never know identities.

CREATE TABLE IF NOT EXISTS resolutions (
    resolution_id    TEXT PRIMARY KEY,
    number           TEXT NOT NULL,
    version          INTEGER NOT NULL DEFAULT 1,
    title            TEXT NOT NULL,
    full_text        TEXT NOT NULL,
    text_hash        TEXT,
    status           TEXT NOT NULL CHECK (status IN
                       ('DRAFT','FINALIZED','VOTING_OPEN','VOTING_CLOSED','PASSED','FAILED',
                        'TIED','WITHDRAWN','NOT_PUT_TO_VOTE','RECONCILIATION_ERROR')),
    voting_rule      TEXT NOT NULL DEFAULT 'FOR_GT_AGAINST',
    eligible_pool_id TEXT NOT NULL DEFAULT 'default',
    seq              INTEGER NOT NULL DEFAULT 0,
    finalized_at     TEXT,
    finalized_by     TEXT,
    opened_at        TEXT,
    closed_at        TEXT,
    disposition_note TEXT,
    superseded_by    TEXT,
    created_at       TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_resolutions_number_version ON resolutions(number, version);
CREATE INDEX IF NOT EXISTS ix_resolutions_status ON resolutions(status);

-- Revision history for DRAFT edits. Contains no voter data.
CREATE TABLE IF NOT EXISTS resolution_revisions (
    revision_id   TEXT PRIMARY KEY,
    resolution_id TEXT NOT NULL REFERENCES resolutions(resolution_id),
    title         TEXT NOT NULL,
    full_text     TEXT NOT NULL,
    operator_id   TEXT,
    timestamp     TEXT NOT NULL
);

-- ============================================================================
-- THE BALLOT TABLE. Four columns. Nothing else, now or later.
-- Adding any column that could identify a voter breaks invariant I2 and the
-- privacy test in tests/privacy/ will fail the build.
-- ============================================================================
CREATE TABLE IF NOT EXISTS ballots (
    ballot_id     TEXT PRIMARY KEY,
    resolution_id TEXT NOT NULL,
    choice        TEXT NOT NULL CHECK (choice IN ('FOR','AGAINST','ABSTAIN')),
    accepted_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_ballots_resolution ON ballots(resolution_id);

-- I4: immutability enforced by the database, so it holds even against a
-- direct sqlite3 prompt, not merely against application code.
CREATE TRIGGER IF NOT EXISTS ballots_no_update
BEFORE UPDATE ON ballots
BEGIN SELECT RAISE(ABORT, 'ballots are immutable'); END;

CREATE TRIGGER IF NOT EXISTS ballots_no_delete
BEFORE DELETE ON ballots
BEGIN SELECT RAISE(ABORT, 'ballots are immutable'); END;

CREATE TABLE IF NOT EXISTS result_snapshots (
    resolution_id  TEXT PRIMARY KEY,
    for_count      INTEGER NOT NULL,
    against_count  INTEGER NOT NULL,
    abstain_count  INTEGER NOT NULL,
    cast_count     INTEGER NOT NULL,
    not_cast_count INTEGER NOT NULL,
    eligible_count INTEGER NOT NULL,
    outcome        TEXT NOT NULL,
    rule_applied   TEXT NOT NULL,
    snapshot_hash  TEXT NOT NULL,
    created_at     TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS result_snapshots_no_update
BEFORE UPDATE ON result_snapshots
BEGIN SELECT RAISE(ABORT, 'result snapshots are immutable'); END;

CREATE TRIGGER IF NOT EXISTS result_snapshots_no_delete
BEFORE DELETE ON result_snapshots
BEGIN SELECT RAISE(ABORT, 'result snapshots are immutable'); END;

CREATE TABLE IF NOT EXISTS resolution_events (
    event_id      TEXT PRIMARY KEY,
    resolution_id TEXT NOT NULL,
    event_type    TEXT NOT NULL,
    operator_id   TEXT,
    timestamp     TEXT NOT NULL,
    previous_hash TEXT
);

CREATE TRIGGER IF NOT EXISTS resolution_events_no_update
BEFORE UPDATE ON resolution_events
BEGIN SELECT RAISE(ABORT, 'resolution_events are append-only'); END;

CREATE TRIGGER IF NOT EXISTS resolution_events_no_delete
BEFORE DELETE ON resolution_events
BEGIN SELECT RAISE(ABORT, 'resolution_events are append-only'); END;
