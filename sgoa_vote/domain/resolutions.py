"""Resolution lifecycle: draft, finalize, amend, open, withdraw.

    DRAFT --finalize--> FINALIZED --open--> VOTING_OPEN --close--> VOTING_CLOSED
                           |                                   --> PASSED|FAILED|TIED
                           +--> WITHDRAWN                      --> RECONCILIATION_ERROR
                           +--> NOT_PUT_TO_VOTE

Finalizing freezes the wording and hashes it. An amendment creates a new version
and leaves the old one visible. Once a ballot has been accepted the text can
never change by any route the application offers.
"""

from __future__ import annotations

import hashlib
import unicodedata

from ..util import new_id, now_iso
from . import audit, entitlements
from .errors import Conflict, NotFound, ValidationError

VOTING_RULES = {
    "FOR_GT_AGAINST": "FOR > AGAINST",
    "TWO_THIRDS_OF_CAST": "FOR >= two-thirds of FOR+AGAINST",
    "MAJORITY_OF_ALL_ELIGIBLE": "FOR > half of all eligible entitlements",
}

TERMINAL_STATUSES = {"PASSED", "FAILED", "TIED", "WITHDRAWN", "NOT_PUT_TO_VOTE"}


def canonical_text(text: str) -> str:
    """Normalise wording so an invisible edit cannot change the hash silently.

    NFC-normalise, unify line endings, strip trailing whitespace on each line,
    and drop leading and trailing blank lines.
    """
    text = unicodedata.normalize("NFC", text or "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in text.split("\n")]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


def compute_text_hash(text: str, version: int) -> str:
    material = f"{canonical_text(text)}|{version}"
    return "sha256:" + hashlib.sha256(material.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# lookups
# --------------------------------------------------------------------------

def get(conn, ident: str):
    """Resolve by resolution_id or by number (returning its current version)."""
    row = conn.execute("SELECT * FROM ballot.resolutions WHERE resolution_id = ?",
                       (ident,)).fetchone()
    if row is not None:
        return row
    return conn.execute(
        """SELECT * FROM ballot.resolutions
            WHERE number = ? AND superseded_by IS NULL
            ORDER BY version DESC LIMIT 1""",
        (ident,),
    ).fetchone()


def require(conn, ident: str):
    row = get(conn, ident)
    if row is None:
        raise NotFound(f"Resolution {ident} was not found.")
    return row


def active(conn):
    return conn.execute(
        "SELECT * FROM ballot.resolutions WHERE status = 'VOTING_OPEN' LIMIT 1"
    ).fetchone()


def list_all(conn, include_superseded: bool = False) -> list[dict]:
    sql = "SELECT * FROM ballot.resolutions"
    if not include_superseded:
        sql += " WHERE superseded_by IS NULL"
    sql += " ORDER BY seq ASC, version ASC"
    return [dict(r) for r in conn.execute(sql).fetchall()]


def versions_of(conn, number: str) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM ballot.resolutions WHERE number = ? ORDER BY version ASC", (number,)
    ).fetchall()]


def next_number(conn) -> str:
    rows = conn.execute("SELECT DISTINCT number FROM ballot.resolutions").fetchall()
    highest = 0
    for row in rows:
        digits = "".join(ch for ch in row["number"] if ch.isdigit())
        if digits:
            highest = max(highest, int(digits))
    return f"R{highest + 1}"


def has_ballots(conn, resolution_id: str) -> bool:
    row = conn.execute("SELECT 1 FROM ballot.ballots WHERE resolution_id = ? LIMIT 1",
                       (resolution_id,)).fetchone()
    return row is not None


def _event(conn, resolution_id: str, event_type: str, operator_id: str | None) -> None:
    conn.execute(
        """INSERT INTO ballot.resolution_events
             (event_id, resolution_id, event_type, operator_id, timestamp, previous_hash)
           VALUES (?,?,?,?,?,?)""",
        (new_id("rse_"), resolution_id, event_type, operator_id, now_iso(), None),
    )


# --------------------------------------------------------------------------
# lifecycle
# --------------------------------------------------------------------------

def create_draft(conn, title: str, full_text: str, *, number: str | None = None,
                 voting_rule: str = "FOR_GT_AGAINST", eligible_pool_id: str = "default",
                 operator_id: str | None = None) -> dict:
    if not (title or "").strip():
        raise ValidationError("Please give the resolution a short title.")
    if not canonical_text(full_text):
        raise ValidationError("Please enter the exact wording to be put to the vote.")
    if voting_rule not in VOTING_RULES:
        raise ValidationError(f"Unknown voting rule '{voting_rule}'.")

    number = (number or next_number(conn)).strip().upper()
    clash = conn.execute("SELECT 1 FROM ballot.resolutions WHERE number = ?",
                         (number,)).fetchone()
    if clash:
        raise Conflict(f"Resolution number {number} already exists. Numbers are never reused.")

    seq = conn.execute(
        "SELECT COALESCE(MAX(seq),0) + 1 AS n FROM ballot.resolutions"
    ).fetchone()["n"]
    resolution_id = new_id("res_")
    conn.execute(
        """INSERT INTO ballot.resolutions
             (resolution_id, number, version, title, full_text, status, voting_rule,
              eligible_pool_id, seq, created_at)
           VALUES (?,?,1,?,?,'DRAFT',?,?,?,?)""",
        (resolution_id, number, title.strip(), canonical_text(full_text),
         voting_rule, eligible_pool_id, seq, now_iso()),
    )
    _event(conn, resolution_id, "RESOLUTION_CREATED", operator_id)
    audit.append(conn, "RESOLUTION_CREATED",
                 {"resolution_number": number, "title": title.strip(),
                  "voting_rule": voting_rule},
                 actor_role="MC", actor_id=operator_id)
    return dict(conn.execute("SELECT * FROM ballot.resolutions WHERE resolution_id = ?",
                             (resolution_id,)).fetchone())


def edit_draft(conn, ident: str, *, title: str | None = None, full_text: str | None = None,
               voting_rule: str | None = None, operator_id: str | None = None) -> dict:
    row = require(conn, ident)
    if row["status"] != "DRAFT":
        raise Conflict(
            f"Resolution {row['number']} is {row['status'].replace('_', ' ').lower()} and can "
            "no longer be edited directly. Use Create amendment instead."
        )
    if voting_rule is not None and voting_rule not in VOTING_RULES:
        raise ValidationError(f"Unknown voting rule '{voting_rule}'.")

    # Keep the pre-edit wording so the drafting history survives.
    conn.execute(
        """INSERT INTO ballot.resolution_revisions
             (revision_id, resolution_id, title, full_text, operator_id, timestamp)
           VALUES (?,?,?,?,?,?)""",
        (new_id("rev_"), row["resolution_id"], row["title"], row["full_text"],
         operator_id, now_iso()),
    )
    conn.execute(
        """UPDATE ballot.resolutions
              SET title = ?, full_text = ?, voting_rule = ?
            WHERE resolution_id = ?""",
        (title.strip() if title is not None else row["title"],
         canonical_text(full_text) if full_text is not None else row["full_text"],
         voting_rule or row["voting_rule"], row["resolution_id"]),
    )
    _event(conn, row["resolution_id"], "RESOLUTION_EDITED", operator_id)
    audit.append(conn, "RESOLUTION_EDITED", {"resolution_number": row["number"]},
                 actor_role="MC", actor_id=operator_id)
    return dict(conn.execute("SELECT * FROM ballot.resolutions WHERE resolution_id = ?",
                             (row["resolution_id"],)).fetchone())


def finalize(conn, ident: str, operator_id: str | None = None) -> dict:
    row = require(conn, ident)
    if row["status"] != "DRAFT":
        raise Conflict(f"Only a draft can be finalized; {row['number']} is "
                       f"{row['status'].replace('_', ' ').lower()}.")
    text_hash = compute_text_hash(row["full_text"], row["version"])
    conn.execute(
        """UPDATE ballot.resolutions
              SET status='FINALIZED', text_hash = ?, finalized_at = ?, finalized_by = ?
            WHERE resolution_id = ?""",
        (text_hash, now_iso(), operator_id, row["resolution_id"]),
    )
    _event(conn, row["resolution_id"], "RESOLUTION_FINALIZED", operator_id)
    audit.append(conn, "RESOLUTION_FINALIZED",
                 {"resolution_number": row["number"], "version": row["version"],
                  "text_hash": text_hash, "voting_rule": row["voting_rule"]},
                 actor_role="MC", actor_id=operator_id)
    return dict(conn.execute("SELECT * FROM ballot.resolutions WHERE resolution_id = ?",
                             (row["resolution_id"],)).fetchone())


def amend(conn, ident: str, *, title: str | None = None, full_text: str | None = None,
          voting_rule: str | None = None, operator_id: str | None = None) -> dict:
    """New version supersedes the old one. The old row stays visible forever."""
    row = require(conn, ident)
    if row["status"] != "FINALIZED":
        raise Conflict("Only a finalized resolution that has not yet opened can be amended.")
    if has_ballots(conn, row["resolution_id"]):
        raise Conflict("Voting has already accepted ballots on this resolution; "
                       "its wording can never change.")

    new_version = row["version"] + 1
    new_text = canonical_text(full_text) if full_text is not None else row["full_text"]
    new_title = title.strip() if title is not None else row["title"]
    new_rule = voting_rule or row["voting_rule"]
    if voting_rule is not None and voting_rule not in VOTING_RULES:
        raise ValidationError(f"Unknown voting rule '{voting_rule}'.")

    new_resolution_id = new_id("res_")
    conn.execute(
        """INSERT INTO ballot.resolutions
             (resolution_id, number, version, title, full_text, text_hash, status,
              voting_rule, eligible_pool_id, seq, finalized_at, finalized_by, created_at)
           VALUES (?,?,?,?,?,?, 'FINALIZED', ?,?,?,?,?,?)""",
        (new_resolution_id, row["number"], new_version, new_title, new_text,
         compute_text_hash(new_text, new_version), new_rule, row["eligible_pool_id"],
         row["seq"], now_iso(), operator_id, now_iso()),
    )
    conn.execute("UPDATE ballot.resolutions SET superseded_by = ? WHERE resolution_id = ?",
                 (new_resolution_id, row["resolution_id"]))
    _event(conn, new_resolution_id, "RESOLUTION_AMENDED", operator_id)
    audit.append(conn, "RESOLUTION_AMENDED",
                 {"resolution_number": row["number"], "from_version": row["version"],
                  "to_version": new_version,
                  "text_hash": compute_text_hash(new_text, new_version)},
                 actor_role="MC", actor_id=operator_id)
    return dict(conn.execute("SELECT * FROM ballot.resolutions WHERE resolution_id = ?",
                             (new_resolution_id,)).fetchone())


def open_voting(conn, ident: str, operator_id: str | None = None) -> dict:
    row = require(conn, ident)
    if row["status"] != "FINALIZED":
        raise Conflict(f"Resolution {row['number']} must be finalized before voting opens.")
    if row["superseded_by"]:
        raise Conflict(f"Resolution {row['number']} has been amended. Open the latest version.")

    already = active(conn)
    if already is not None:
        raise Conflict(
            f"Resolution {already['number']} is still open. Close it before opening another."
        )

    conn.execute(
        "UPDATE ballot.resolutions SET status='VOTING_OPEN', opened_at = ? WHERE resolution_id = ?",
        (now_iso(), row["resolution_id"]),
    )
    eligible = entitlements.open_ledger_for_resolution(conn, row["resolution_id"])
    conn.execute("UPDATE agms SET status='VOTING_IN_PROGRESS' "
                 "WHERE status IN ('SETUP','REGISTRATION_OPEN')")

    _event(conn, row["resolution_id"], "VOTING_OPENED", operator_id)
    audit.append(conn, "VOTING_OPENED",
                 {"resolution_number": row["number"], "version": row["version"],
                  "text_hash": row["text_hash"], "eligible_entitlements": eligible},
                 actor_role="MC", actor_id=operator_id)
    return {"resolution": dict(conn.execute(
        "SELECT * FROM ballot.resolutions WHERE resolution_id = ?",
        (row["resolution_id"],)).fetchone()), "eligible_entitlements": eligible}


def withdraw(conn, ident: str, note: str = "", operator_id: str | None = None) -> dict:
    return _dispose(conn, ident, "WITHDRAWN", "RESOLUTION_WITHDRAWN", note, operator_id)


def not_put_to_vote(conn, ident: str, note: str = "", operator_id: str | None = None) -> dict:
    return _dispose(conn, ident, "NOT_PUT_TO_VOTE", "RESOLUTION_NOT_PUT_TO_VOTE", note,
                    operator_id)


def _dispose(conn, ident: str, status: str, event_type: str, note: str,
             operator_id: str | None) -> dict:
    row = require(conn, ident)
    if row["status"] not in ("DRAFT", "FINALIZED"):
        raise Conflict(
            f"Resolution {row['number']} is {row['status'].replace('_', ' ').lower()} and "
            "cannot be marked at this point."
        )
    conn.execute(
        "UPDATE ballot.resolutions SET status = ?, disposition_note = ? WHERE resolution_id = ?",
        (status, note or None, row["resolution_id"]),
    )
    _event(conn, row["resolution_id"], event_type, operator_id)
    audit.append(conn, event_type,
                 {"resolution_number": row["number"], "note": note or "none"},
                 actor_role="MC", actor_id=operator_id)
    return dict(conn.execute("SELECT * FROM ballot.resolutions WHERE resolution_id = ?",
                             (row["resolution_id"],)).fetchone())


def record_disposition(conn, ident: str, note: str, operator_id: str | None = None) -> dict:
    """Chair or general-body action, recorded separately from the anonymous ballots."""
    row = require(conn, ident)
    if not (note or "").strip():
        raise ValidationError("Please enter the disposition note.")
    existing = row["disposition_note"]
    combined = f"{existing}\n{note.strip()}" if existing else note.strip()
    conn.execute("UPDATE ballot.resolutions SET disposition_note = ? WHERE resolution_id = ?",
                 (combined, row["resolution_id"]))
    _event(conn, row["resolution_id"], "DISPOSITION_NOTE_RECORDED", operator_id)
    audit.append(conn, "DISPOSITION_NOTE_RECORDED",
                 {"resolution_number": row["number"], "note": note.strip()},
                 actor_role="MC", actor_id=operator_id)
    return dict(conn.execute("SELECT * FROM ballot.resolutions WHERE resolution_id = ?",
                             (row["resolution_id"],)).fetchone())
