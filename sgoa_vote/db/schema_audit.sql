-- audit.db : hash-chained, append-only record of everything that happened.
-- Payloads about voting are aggregate-only. No payload may name a credential.

CREATE TABLE IF NOT EXISTS audit_log (
    sequence     INTEGER PRIMARY KEY,
    timestamp    TEXT NOT NULL,
    actor_role   TEXT NOT NULL,
    actor_id     TEXT,
    event_type   TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    prev_hash    TEXT NOT NULL,
    event_hash   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_audit_event_type ON audit_log(event_type);

-- Append-only. Tampering is still detectable via the hash chain even if these
-- triggers are dropped, which is the point of chaining rather than just locking.
CREATE TRIGGER IF NOT EXISTS audit_log_no_update
BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;

CREATE TRIGGER IF NOT EXISTS audit_log_no_delete
BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;

CREATE TABLE IF NOT EXISTS audit_checkpoints (
    checkpoint_id TEXT PRIMARY KEY,
    head_sequence INTEGER NOT NULL,
    head_hash     TEXT NOT NULL,
    label         TEXT NOT NULL,
    created_at    TEXT NOT NULL
);
