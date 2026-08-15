"""Hash-chained, append-only audit log.

    event_hash[n] = SHA256(prev_hash || sequence || timestamp || event_type || canonical_payload)

Altering or removing any earlier event invalidates every later hash, which is
what makes tampering detectable after the fact even by someone who has the
database file.

Payload discipline: nothing written here may link a ballot to a person. Voting
events are aggregate-only -- {"resolution_id": "R4", "count": 3} and no more.
`assert_payload_clean` is the guard, and tests/privacy re-checks every stored row.
"""

from __future__ import annotations

import hashlib

from ..config import canonical_json
from ..util import new_id, now_iso

GENESIS_HASH = "0" * 64

# Keys that would create the linkage the whole design exists to prevent.
FORBIDDEN_PAYLOAD_KEYS = {
    "credential_id",
    "credential_code",
    "code",
    "attendee_id",
    "attendee_name",
    "apartment_id",
    "session_id",
    "session_id_hash",
    "device_fingerprint",
    "ip",
    "ip_address",
    "client_ip",
    "mac",
    "mac_address",
    "proxy_source_apartment",
    "choice",
    "allocation",
    "ballot_id",
}

# Voting events carry no subject at all; these are the aggregate-only types.
AGGREGATE_ONLY_EVENTS = {"ENTITLEMENTS_CONSUMED"}


class AuditPayloadError(ValueError):
    """Raised when an event payload would leak identity into the audit trail."""


def assert_payload_clean(event_type: str, payload: dict) -> None:
    for key in payload:
        if key in FORBIDDEN_PAYLOAD_KEYS:
            raise AuditPayloadError(
                f"audit payload for {event_type} may not contain '{key}'"
            )
    if event_type in AGGREGATE_ONLY_EVENTS:
        allowed = {"resolution_id", "resolution_number", "count"}
        extra = set(payload) - allowed
        if extra:
            raise AuditPayloadError(
                f"{event_type} is aggregate-only; unexpected keys {sorted(extra)}"
            )


def compute_hash(prev_hash: str, sequence: int, timestamp: str, event_type: str,
                 payload: dict) -> str:
    material = f"{prev_hash}{sequence}{timestamp}{event_type}{canonical_json(payload)}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def head(conn) -> tuple[int, str]:
    """(sequence, hash) of the newest event, or (0, GENESIS_HASH) if empty."""
    row = conn.execute(
        "SELECT sequence, event_hash FROM audit.audit_log ORDER BY sequence DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return 0, GENESIS_HASH
    return row["sequence"], row["event_hash"]


def append(conn, event_type: str, payload: dict | None = None, *,
           actor_role: str = "SYSTEM", actor_id: str | None = None) -> dict:
    """Append one event inside the caller's transaction."""
    payload = payload or {}
    assert_payload_clean(event_type, payload)

    prev_seq, prev_hash = head(conn)
    sequence = prev_seq + 1
    timestamp = now_iso()
    event_hash = compute_hash(prev_hash, sequence, timestamp, event_type, payload)

    conn.execute(
        """INSERT INTO audit.audit_log
             (sequence, timestamp, actor_role, actor_id, event_type,
              payload_json, prev_hash, event_hash)
           VALUES (?,?,?,?,?,?,?,?)""",
        (sequence, timestamp, actor_role, actor_id, event_type,
         canonical_json(payload), prev_hash, event_hash),
    )
    return {"sequence": sequence, "event_hash": event_hash, "timestamp": timestamp}


def verify_chain(conn) -> dict:
    """Recompute the whole chain. Reports the first sequence that fails."""
    rows = conn.execute(
        """SELECT sequence, timestamp, event_type, payload_json, prev_hash, event_hash
             FROM audit.audit_log ORDER BY sequence ASC"""
    ).fetchall()

    expected_prev = GENESIS_HASH
    expected_seq = 1
    for row in rows:
        if row["sequence"] != expected_seq:
            return {"ok": False, "events": len(rows), "first_bad_sequence": row["sequence"],
                    "reason": f"sequence gap: expected {expected_seq}, found {row['sequence']}"}
        if row["prev_hash"] != expected_prev:
            return {"ok": False, "events": len(rows), "first_bad_sequence": row["sequence"],
                    "reason": "prev_hash does not match the previous event's hash"}

        import json as _json
        payload = _json.loads(row["payload_json"])
        recomputed = compute_hash(row["prev_hash"], row["sequence"], row["timestamp"],
                                  row["event_type"], payload)
        if recomputed != row["event_hash"]:
            return {"ok": False, "events": len(rows), "first_bad_sequence": row["sequence"],
                    "reason": "event content does not match its stored hash"}

        expected_prev = row["event_hash"]
        expected_seq += 1

    return {"ok": True, "events": len(rows), "first_bad_sequence": None,
            "reason": "chain intact", "head_hash": expected_prev,
            "head_sequence": len(rows)}


def create_checkpoint(conn, label: str) -> dict:
    """Anchor the current chain head so later alteration is detectable."""
    append(conn, "AUDIT_CHECKPOINT_CREATED", {"label": label})
    seq, head_hash = head(conn)
    checkpoint_id = new_id("chk_")
    conn.execute(
        """INSERT INTO audit.audit_checkpoints
             (checkpoint_id, head_sequence, head_hash, label, created_at)
           VALUES (?,?,?,?,?)""",
        (checkpoint_id, seq, head_hash, label, now_iso()),
    )
    return {"checkpoint_id": checkpoint_id, "head_sequence": seq,
            "head_hash": head_hash, "label": label}


def checkpoints(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM audit.audit_checkpoints ORDER BY head_sequence ASC"
    ).fetchall()
    return [dict(r) for r in rows]


def recent(conn, limit: int = 200) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM audit.audit_log ORDER BY sequence DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]
