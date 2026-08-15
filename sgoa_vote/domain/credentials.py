"""Voter credentials and voter sessions.

A four-character code is a usability credential, not a password. Its safety comes
from the surrounding controls: it only works after check-in, only on the meeting
network, only for one device at a time, only for the length of the meeting, and
every failed burst raises an audit event.

The plaintext code exists exactly once -- in the HTTP response that renders the
printable card. Only an HMAC of it is stored.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from pathlib import Path

from ..util import future_iso, is_past, new_id, now_iso
from . import audit
from .errors import Conflict, NotFound, RateLimited, Unauthorized, ValidationError, ASSISTANCE

# Unambiguous in print: no I or O, no 0 or 1.
LETTERS = "ABCDEFGHJKLMNPQRSTUVWXYZ"
DIGITS = "23456789"
CODE_LETTERS = 3


def load_or_create_key(path: Path) -> bytes:
    path = Path(path)
    if path.exists():
        return path.read_bytes()
    key = secrets.token_bytes(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(key)
    try:  # best effort; Windows ACLs are handled by the OS account, not chmod
        path.chmod(0o600)
    except OSError:
        pass
    return key


def generate_code() -> str:
    return "".join(secrets.choice(LETTERS) for _ in range(CODE_LETTERS)) + secrets.choice(DIGITS)


def normalise_code(code: str) -> str:
    return (code or "").strip().upper().replace(" ", "")


def hash_code(key: bytes, code: str) -> str:
    return hmac.new(key, normalise_code(code).encode("utf-8"), hashlib.sha256).hexdigest()


def _unique_code(conn, key: bytes) -> tuple[str, str]:
    for _ in range(500):
        code = generate_code()
        code_hash = hash_code(key, code)
        exists = conn.execute(
            "SELECT 1 FROM credentials WHERE code_hash = ?", (code_hash,)
        ).fetchone()
        if not exists:
            return code, code_hash
    raise Conflict("Unable to generate a unique voting code. Please contact the administrator.")


# --------------------------------------------------------------------------
# issue / reset
# --------------------------------------------------------------------------

def issue_credential(conn, key: bytes, attendee_id: str, entitlement_count: int,
                     operator_id: str | None = None) -> dict:
    attendee = conn.execute(
        "SELECT * FROM attendees WHERE attendee_id = ?", (attendee_id,)
    ).fetchone()
    if attendee is None:
        raise NotFound("That attendee record does not exist.")

    existing = conn.execute(
        "SELECT * FROM credentials WHERE attendee_id = ? AND status IN ('CREATED','ACTIVE')",
        (attendee_id,),
    ).fetchone()
    if existing is not None:
        raise Conflict(
            f"{attendee['display_name']} already holds an active voting code. "
            "Use Reset code if a replacement is needed."
        )

    code, code_hash = _unique_code(conn, key)
    credential_id = new_id("cred_")
    conn.execute(
        """INSERT INTO credentials
             (credential_id, code_hash, attendee_id, status, entitlement_count, issued_at)
           VALUES (?,?,?,'ACTIVE',?,?)""",
        (credential_id, code_hash, attendee_id, entitlement_count, now_iso()),
    )
    _registration_event(conn, "CREDENTIAL_ISSUED", attendee_id, operator_id,
                        f"{entitlement_count} entitlement(s)")
    audit.append(conn, "CREDENTIAL_ISSUED",
                 {"entitlement_count": entitlement_count},
                 actor_role="REGISTRATION", actor_id=operator_id)
    return {"credential_id": credential_id, "code": code,
            "entitlement_count": entitlement_count,
            "attendee_name": attendee["display_name"]}


def reset_credential(conn, key: bytes, credential_id: str, operator_id: str | None = None,
                     reason: str = "") -> dict:
    """Issue a replacement code bound to the same ledger.

    AC-13: consumed entitlements are never restored. The ledger rows are moved
    across to the new credential id with their consumed counts intact, so a lost
    card cannot become a second vote.
    """
    cred = conn.execute(
        "SELECT * FROM credentials WHERE credential_id = ?", (credential_id,)
    ).fetchone()
    if cred is None:
        raise NotFound("That voting code record does not exist.")
    if cred["status"] in ("REPLACED", "CLOSED"):
        raise Conflict("That voting code has already been replaced or closed.")

    code, code_hash = _unique_code(conn, key)
    new_credential_id = new_id("cred_")

    conn.execute(
        """INSERT INTO credentials
             (credential_id, code_hash, attendee_id, status, entitlement_count,
              replaces, issued_at)
           VALUES (?,?,?,'ACTIVE',?,?,?)""",
        (new_credential_id, code_hash, cred["attendee_id"], cred["entitlement_count"],
         credential_id, now_iso()),
    )
    conn.execute("UPDATE credentials SET status='REPLACED' WHERE credential_id = ?",
                 (credential_id,))
    # Carry the ledger across, consumed counts and all.
    conn.execute(
        "UPDATE credential_resolution_ledger SET credential_id = ? WHERE credential_id = ?",
        (new_credential_id, credential_id),
    )
    # Any device holding the old code is logged out immediately.
    conn.execute("DELETE FROM sessions WHERE credential_id = ?", (credential_id,))

    _registration_event(conn, "CREDENTIAL_RESET", cred["attendee_id"], operator_id, reason)
    audit.append(conn, "CREDENTIAL_RESET", {"reason": reason or "not stated"},
                 actor_role="REGISTRATION", actor_id=operator_id)
    return {"credential_id": new_credential_id, "code": code,
            "entitlement_count": cred["entitlement_count"]}


def set_entitlement_count(conn, credential_id: str, count: int) -> None:
    conn.execute("UPDATE credentials SET entitlement_count = ? WHERE credential_id = ?",
                 (count, credential_id))


def active_credential_for_attendee(conn, attendee_id: str):
    return conn.execute(
        "SELECT * FROM credentials WHERE attendee_id = ? AND status = 'ACTIVE'",
        (attendee_id,),
    ).fetchone()


def _registration_event(conn, event_type: str, subject_id: str | None,
                        operator_id: str | None, reason: str = "") -> None:
    conn.execute(
        """INSERT INTO registration_events
             (event_id, event_type, subject_id, operator_id, reason, timestamp)
           VALUES (?,?,?,?,?,?)""",
        (new_id("rev_"), event_type, subject_id, operator_id, reason, now_iso()),
    )


# --------------------------------------------------------------------------
# rate limiting
# --------------------------------------------------------------------------

def check_rate_limit(conn, client_key: str, cfg) -> None:
    window_start = future_iso(seconds=-cfg.join_rate_limit_window_seconds)
    failures = conn.execute(
        """SELECT COUNT(*) AS n FROM join_attempts
            WHERE client_key = ? AND ok = 0 AND attempt_at >= ?""",
        (client_key, window_start),
    ).fetchone()["n"]
    if failures >= cfg.join_rate_limit_attempts:
        raise RateLimited(
            "Too many incorrect codes were entered from this device. "
            f"Please wait {cfg.join_rate_limit_cooldown_seconds} seconds and try again.",
            next_action=ASSISTANCE,
        )


def record_attempt(conn, client_key: str, ok: bool, cfg) -> None:
    conn.execute(
        "INSERT INTO join_attempts (client_key, attempt_at, ok) VALUES (?,?,?)",
        (client_key, now_iso(), 1 if ok else 0),
    )
    if ok:
        return
    window_start = future_iso(seconds=-cfg.join_rate_limit_window_seconds)
    failures = conn.execute(
        """SELECT COUNT(*) AS n FROM join_attempts
            WHERE client_key = ? AND ok = 0 AND attempt_at >= ?""",
        (client_key, window_start),
    ).fetchone()["n"]
    if failures == cfg.join_rate_limit_attempts:
        # Aggregate only: the audit trail records that a burst happened, never
        # which code was being guessed or from which address.
        audit.append(conn, "INVALID_CREDENTIAL_THRESHOLD",
                     {"failed_attempts": failures,
                      "window_seconds": cfg.join_rate_limit_window_seconds})


# --------------------------------------------------------------------------
# authentication and sessions
# --------------------------------------------------------------------------

def hash_session_id(session_id: str) -> str:
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()


def authenticate(conn, key: bytes, code: str, cfg, client_key: str,
                 current_session_id: str | None = None) -> dict:
    """Validate a code and open a session.

    Attempts are *not* recorded here. A failure raises, which rolls back the
    surrounding transaction -- so recording the attempt inside it would erase the
    very evidence the rate limiter runs on. The caller records the outcome in its
    own committed transaction.
    """
    check_rate_limit(conn, client_key, cfg)

    code = normalise_code(code)
    if len(code) != CODE_LETTERS + 1:
        raise Unauthorized("That code is not the right length. Codes look like KTR7.",
                           next_action=ASSISTANCE)

    row = conn.execute(
        "SELECT * FROM credentials WHERE code_hash = ?", (hash_code(key, code),)
    ).fetchone()
    if row is None or row["status"] != "ACTIVE":
        raise Unauthorized("That code was not recognised.", next_action=ASSISTANCE)

    purge_expired_sessions(conn)
    live = conn.execute(
        "SELECT session_id_hash FROM sessions WHERE credential_id = ?",
        (row["credential_id"],),
    ).fetchall()

    # The same device re-joining with its cookie still set is not a second device.
    same_device = (
        current_session_id is not None
        and hash_session_id(current_session_id) in {r["session_id_hash"] for r in live}
    )
    if live and not same_device and len(live) >= cfg.sessions_per_credential:
        raise Conflict(
            "This code is already in use on another device.",
            next_action=ASSISTANCE,
        )

    if same_device:
        return {"credential_id": row["credential_id"], "session_id": current_session_id,
                "entitlement_count": row["entitlement_count"], "resumed": True}

    session_id = secrets.token_urlsafe(32)  # well above the 128-bit floor
    conn.execute(
        """INSERT INTO sessions (session_id_hash, credential_id, created_at, expires_at)
           VALUES (?,?,?,?)""",
        (hash_session_id(session_id), row["credential_id"], now_iso(),
         future_iso(hours=cfg.voter_session_hours)),
    )
    return {"credential_id": row["credential_id"], "session_id": session_id,
            "entitlement_count": row["entitlement_count"], "resumed": False}


def resolve_session(conn, session_id: str | None):
    """Return the credential row for a session cookie, or None."""
    if not session_id:
        return None
    row = conn.execute(
        """SELECT s.expires_at, c.*
             FROM sessions s JOIN credentials c ON c.credential_id = s.credential_id
            WHERE s.session_id_hash = ?""",
        (hash_session_id(session_id),),
    ).fetchone()
    if row is None or is_past(row["expires_at"]) or row["status"] != "ACTIVE":
        return None
    return row


def end_session(conn, session_id: str | None) -> None:
    """Ends the device session. Never touches the voting ledger."""
    if session_id:
        conn.execute("DELETE FROM sessions WHERE session_id_hash = ?",
                     (hash_session_id(session_id),))


def purge_expired_sessions(conn) -> None:
    conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now_iso(),))


def active_session_count(conn) -> int:
    purge_expired_sessions(conn)
    return conn.execute("SELECT COUNT(*) AS n FROM sessions").fetchone()["n"]
