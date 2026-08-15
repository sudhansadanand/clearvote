"""The AGM instance itself: creation, configuration freeze, and finalization."""

from __future__ import annotations

from ..util import new_id, now_iso
from . import audit, resolutions
from .errors import Conflict, NotFound

STATUSES = ("SETUP", "REGISTRATION_OPEN", "VOTING_IN_PROGRESS", "FINALIZED", "ARCHIVED")


def get(conn):
    return conn.execute("SELECT * FROM agms ORDER BY created_at ASC LIMIT 1").fetchone()


def require(conn):
    row = get(conn)
    if row is None:
        raise NotFound("No AGM has been created yet. Run `python -m sgoa_vote seed` "
                       "or create one from the admin console.")
    return row


def create(conn, title: str, agm_date: str, location: str, cfg, is_demo: bool = False) -> dict:
    from ..config import canonical_json

    if get(conn) is not None:
        raise Conflict("An AGM already exists in this database. Use a fresh data directory "
                       "for a separate meeting.")
    agm_id = new_id("agm_")
    config_json = canonical_json(cfg.governance())
    conn.execute(
        """INSERT INTO agms
             (agm_id, title, agm_date, location, status, config_json, config_hash,
              is_demo, created_at)
           VALUES (?,?,?,?,'SETUP',?,?,?,?)""",
        (agm_id, title, agm_date, location, config_json, cfg.config_hash(),
         1 if is_demo else 0, now_iso()),
    )
    audit.append(conn, "AGM_CREATED",
                 {"title": title, "date": agm_date, "location": location})
    audit.append(conn, "AGM_CONFIG_FROZEN",
                 {"config_hash": cfg.config_hash(), "governance": cfg.governance()})
    audit.create_checkpoint(conn, "AGM configuration frozen")
    return {"agm_id": agm_id, "config_hash": cfg.config_hash()}


def open_registration(conn, operator_id: str | None = None) -> dict:
    row = require(conn)
    if row["status"] not in ("SETUP", "REGISTRATION_OPEN"):
        raise Conflict(f"The AGM is {row['status'].replace('_', ' ').lower()}; "
                       "registration cannot be reopened from here.")
    conn.execute("UPDATE agms SET status='REGISTRATION_OPEN' WHERE agm_id = ?",
                 (row["agm_id"],))
    audit.append(conn, "REGISTRATION_OPENED", {}, actor_role="ADMIN", actor_id=operator_id)
    audit.create_checkpoint(conn, "registration opened")
    return {"status": "REGISTRATION_OPEN"}


def assert_ready_to_finalize(conn) -> None:
    open_row = resolutions.active(conn)
    if open_row is not None:
        raise Conflict(
            f"Resolution {open_row['number']} is still open for voting. "
            "Close it before finalizing the AGM."
        )
    broken = conn.execute(
        "SELECT number FROM ballot.resolutions WHERE status = 'RECONCILIATION_ERROR'"
    ).fetchall()
    if broken:
        numbers = ", ".join(r["number"] for r in broken)
        raise Conflict(
            f"Resolution(s) {numbers} are in reconciliation error. "
            "These must be investigated by the scrutineers before certification."
        )


def finalize(conn, operator_id: str | None = None) -> dict:
    row = require(conn)
    assert_ready_to_finalize(conn)
    if row["status"] == "FINALIZED":
        return {"status": "FINALIZED", "already": True}
    conn.execute("UPDATE agms SET status='FINALIZED' WHERE agm_id = ?", (row["agm_id"],))
    audit.append(conn, "AGM_FINALIZED", {"title": row["title"]},
                 actor_role="ADMIN", actor_id=operator_id)
    return {"status": "FINALIZED", "already": False}


def is_demo(conn) -> bool:
    row = get(conn)
    return bool(row["is_demo"]) if row is not None else False
