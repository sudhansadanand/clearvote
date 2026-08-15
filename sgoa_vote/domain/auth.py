"""Operator accounts, operator sessions, CSRF tokens and role checks.

Separate from voter credentials in every respect: different table, different
cookie, different lifetime, different failure behaviour. An operator session
also carries the CSRF token used by the console pages.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

from ..util import future_iso, is_past, new_id, now_iso, parse_iso
from .errors import Forbidden, Unauthorized, ValidationError

ROLES = ("ADMIN", "REGISTRATION", "MC", "SCRUTINEER")

_SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1, "dklen": 32}


def hash_password(password: str, salt: str) -> str:
    return hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt),
                          **_SCRYPT).hex()


def create_operator(conn, username: str, role: str, password: str) -> str:
    if role not in ROLES:
        raise ValidationError(f"Unknown role '{role}'.")
    if not password or len(password) < 6:
        raise ValidationError("Operator passwords must be at least 6 characters.")
    salt = secrets.token_bytes(16).hex()
    operator_id = new_id("op_")
    conn.execute(
        """INSERT INTO operators
             (operator_id, username, role, password_hash, salt, created_at)
           VALUES (?,?,?,?,?,?)""",
        (operator_id, username.strip().lower(), role, hash_password(password, salt),
         salt, now_iso()),
    )
    return operator_id


def set_password(conn, username: str, password: str) -> None:
    row = conn.execute("SELECT * FROM operators WHERE username = ?",
                       (username.strip().lower(),)).fetchone()
    if row is None:
        raise Unauthorized(f"No operator account named '{username}'.")
    if not password or len(password) < 6:
        raise ValidationError("Operator passwords must be at least 6 characters.")
    salt = secrets.token_bytes(16).hex()
    conn.execute("UPDATE operators SET password_hash = ?, salt = ? WHERE operator_id = ?",
                 (hash_password(password, salt), salt, row["operator_id"]))


def verify_operator(conn, username: str, password: str):
    row = None
    if username:
        row = conn.execute("SELECT * FROM operators WHERE username = ?",
                           ((username or "").strip().lower(),)).fetchone()
    if row is None:
        # Constant-ish work whether or not the account exists.
        hash_password(password or "x", secrets.token_bytes(16).hex())
        raise Unauthorized("That username or password was not recognised.")
    candidate = hash_password(password or "", row["salt"])
    if not hmac.compare_digest(candidate, row["password_hash"]):
        raise Unauthorized("That username or password was not recognised.")
    return row


def hash_session_id(session_id: str) -> str:
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()


def create_operator_session(conn, operator_id: str, cfg) -> dict:
    session_id = secrets.token_urlsafe(32)
    csrf_token = secrets.token_urlsafe(24)
    conn.execute(
        """INSERT INTO operator_sessions
             (session_id_hash, operator_id, csrf_token, created_at, last_seen_at, expires_at)
           VALUES (?,?,?,?,?,?)""",
        (hash_session_id(session_id), operator_id, csrf_token, now_iso(), now_iso(),
         future_iso(hours=12)),
    )
    return {"session_id": session_id, "csrf_token": csrf_token}


def resolve_operator_session(conn, session_id: str | None, cfg):
    """Return (operator_row, csrf_token) or None. Enforces the inactivity lock."""
    if not session_id:
        return None
    row = conn.execute(
        """SELECT s.session_id_hash, s.csrf_token, s.last_seen_at, s.expires_at, o.*
             FROM operator_sessions s
             JOIN operators o ON o.operator_id = s.operator_id
            WHERE s.session_id_hash = ?""",
        (hash_session_id(session_id),),
    ).fetchone()
    if row is None or is_past(row["expires_at"]):
        return None

    idle_minutes = (parse_iso(now_iso()) - parse_iso(row["last_seen_at"])).total_seconds() / 60
    if idle_minutes > cfg.operator_inactivity_minutes:
        conn.execute("DELETE FROM operator_sessions WHERE session_id_hash = ?",
                     (row["session_id_hash"],))
        return None
    return row


def touch_operator_session(conn, session_id: str) -> None:
    conn.execute("UPDATE operator_sessions SET last_seen_at = ? WHERE session_id_hash = ?",
                 (now_iso(), hash_session_id(session_id)))


def end_operator_session(conn, session_id: str | None) -> None:
    if session_id:
        conn.execute("DELETE FROM operator_sessions WHERE session_id_hash = ?",
                     (hash_session_id(session_id),))


def require_role(operator, *roles: str):
    if operator is None:
        raise Unauthorized("Please sign in to continue.")
    if operator["role"] == "ADMIN" and "ADMIN" not in roles:
        # The administrator can reach operational consoles, but note that the
        # results gate in the API is absolute and applies to ADMIN too.
        return operator
    if operator["role"] not in roles:
        raise Forbidden(
            f"This action requires the {' or '.join(roles).lower()} role; "
            f"you are signed in as {operator['role'].lower()}."
        )
    return operator


def check_csrf(session_row, supplied: str | None) -> None:
    if session_row is None:
        raise Unauthorized("Please sign in to continue.")
    if not supplied or not hmac.compare_digest(supplied, session_row["csrf_token"]):
        raise Forbidden("Your page has expired. Please reload and try again.")
