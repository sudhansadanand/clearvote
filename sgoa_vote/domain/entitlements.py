"""Apartments, representations (own and proxy), and the consumption ledger.

One apartment resolves to exactly one active voting entitlement holder at any
instant -- enforced by a partial unique index in the schema, not by a check here
that a future refactor could drop.

Ledger rows are created when a resolution opens, snapshotting each credential's
entitlement count at that moment. That single choice makes several requirements
fall out naturally: revocation cannot rewrite history, a closed resolution's
eligible total can never drift, and reconciliation always has a fixed
denominator to check against.
"""

from __future__ import annotations

from ..util import new_id, now_iso
from . import audit
from .credentials import _registration_event, active_credential_for_attendee
from .errors import Conflict, NotFound, ValidationError


# --------------------------------------------------------------------------
# apartments and attendees
# --------------------------------------------------------------------------

def add_apartment(conn, apartment_id: str, owner_display_name: str = "",
                  eligible: bool = True, notes: str | None = None) -> None:
    ts = now_iso()
    conn.execute(
        """INSERT INTO apartments
             (apartment_id, eligible, owner_display_name, eligibility_notes,
              eligibility_version, created_at, updated_at)
           VALUES (?,?,?,?,1,?,?)""",
        (apartment_id, 1 if eligible else 0, owner_display_name, notes, ts, ts),
    )


def set_apartment_eligibility(conn, apartment_id: str, eligible: bool,
                              operator_id: str | None = None, reason: str = "") -> None:
    row = conn.execute("SELECT * FROM apartments WHERE apartment_id = ?",
                       (apartment_id,)).fetchone()
    if row is None:
        raise NotFound(f"Apartment {apartment_id} is not in the register.")
    conn.execute(
        """UPDATE apartments
              SET eligible = ?, eligibility_version = eligibility_version + 1,
                  eligibility_notes = ?, updated_at = ?
            WHERE apartment_id = ?""",
        (1 if eligible else 0, reason or row["eligibility_notes"], now_iso(), apartment_id),
    )
    _registration_event(conn, "APARTMENT_ELIGIBILITY_CHANGED", apartment_id,
                        operator_id, reason)


def ensure_attendee(conn, display_name: str) -> str:
    display_name = (display_name or "").strip()
    if not display_name:
        raise ValidationError("Please enter the attendee's name.")
    row = conn.execute("SELECT * FROM attendees WHERE display_name = ?",
                       (display_name,)).fetchone()
    if row is not None:
        return row["attendee_id"]
    attendee_id = new_id("att_")
    conn.execute(
        "INSERT INTO attendees (attendee_id, display_name, created_at) VALUES (?,?,?)",
        (attendee_id, display_name, now_iso()),
    )
    return attendee_id


# --------------------------------------------------------------------------
# representations
# --------------------------------------------------------------------------

def count_active_representations(conn, attendee_id: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) AS n FROM representations WHERE attendee_id = ? AND status = 'ACTIVE'",
        (attendee_id,),
    ).fetchone()["n"]


def count_active_proxies(conn, attendee_id: str) -> int:
    return conn.execute(
        """SELECT COUNT(*) AS n FROM representations
            WHERE attendee_id = ? AND status = 'ACTIVE' AND rep_type = 'PROXY'""",
        (attendee_id,),
    ).fetchone()["n"]


def assign_representation(conn, apartment_id: str, attendee_id: str, rep_type: str,
                          cfg, proxy_ref: str | None = None,
                          operator_id: str | None = None,
                          override_reason: str | None = None) -> dict:
    apartment = conn.execute("SELECT * FROM apartments WHERE apartment_id = ?",
                             (apartment_id,)).fetchone()
    if apartment is None:
        raise NotFound(f"Apartment {apartment_id} is not in the register.")
    if not apartment["eligible"]:
        raise Conflict(f"Apartment {apartment_id} is not eligible to vote at this AGM.")
    if rep_type not in ("OWN", "PROXY"):
        raise ValidationError("Representation type must be OWN or PROXY.")

    if rep_type == "PROXY":
        held = count_active_proxies(conn, attendee_id)
        if held >= cfg.max_proxies_per_attendee:
            raise Conflict(
                f"This attendee already holds {held} proxies, which is the configured "
                f"maximum of {cfg.max_proxies_per_attendee}."
            )

    existing = conn.execute(
        "SELECT * FROM representations WHERE apartment_id = ? AND status = 'ACTIVE'",
        (apartment_id,),
    ).fetchone()

    if existing is not None:
        # The owner-arrives-after-the-proxy case. Never silent: an authorised
        # operator has to say so and say why, and both events are logged.
        if not override_reason:
            holder = conn.execute("SELECT display_name FROM attendees WHERE attendee_id = ?",
                                  (existing["attendee_id"],)).fetchone()
            raise Conflict(
                f"Apartment {apartment_id} is already represented by "
                f"{holder['display_name'] if holder else 'another attendee'} "
                f"({existing['rep_type'].lower()}). An operator override with a reason is "
                "required to change the holder.",
                existing_representation_id=existing["representation_id"],
            )
        conn.execute(
            "UPDATE representations SET status = 'SUPERSEDED' WHERE representation_id = ?",
            (existing["representation_id"],),
        )
        _registration_event(conn, "REPRESENTATION_SUPERSEDED", apartment_id,
                            operator_id, override_reason)
        audit.append(conn, "REPRESENTATION_REVOKED",
                     {"apartment": apartment_id, "reason": override_reason,
                      "cause": "superseded by operator override"},
                     actor_role="REGISTRATION", actor_id=operator_id)
        _recompute_after_change(conn, existing["attendee_id"])

    representation_id = new_id("rep_")
    conn.execute(
        """INSERT INTO representations
             (representation_id, apartment_id, attendee_id, rep_type, proxy_ref,
              status, created_at)
           VALUES (?,?,?,?,?,'ACTIVE',?)""",
        (representation_id, apartment_id, attendee_id, rep_type, proxy_ref, now_iso()),
    )
    _registration_event(conn, "REPRESENTATION_ASSIGNED", apartment_id, operator_id,
                        f"{rep_type} / {proxy_ref or 'no proxy reference'}")
    audit.append(conn, "REPRESENTATION_ASSIGNED",
                 {"apartment": apartment_id, "type": rep_type},
                 actor_role="REGISTRATION", actor_id=operator_id)

    total = _recompute_after_change(conn, attendee_id)
    return {"representation_id": representation_id, "entitlement_count": total}


def revoke_representation(conn, representation_id: str, operator_id: str | None,
                          reason: str) -> dict:
    """Revoke a representation, removing only *unused future* entitlement.

    Ballots already accepted are anonymous and are never deleted or reassigned.
    Closed resolutions keep the eligible total they were decided on. For a
    resolution still open, the credential's allowance is reduced to what it has
    already consumed, so the revoked entitlement cannot still be cast.
    """
    if not reason or not reason.strip():
        raise ValidationError("A reason is required to revoke a representation.")
    rep = conn.execute("SELECT * FROM representations WHERE representation_id = ?",
                       (representation_id,)).fetchone()
    if rep is None:
        raise NotFound("That representation does not exist.")
    if rep["status"] != "ACTIVE":
        raise Conflict("That representation is not active.")

    conn.execute("UPDATE representations SET status = 'REVOKED' WHERE representation_id = ?",
                 (representation_id,))
    _registration_event(conn, "REPRESENTATION_REVOKED", rep["apartment_id"],
                        operator_id, reason)
    audit.append(conn, "REPRESENTATION_REVOKED",
                 {"apartment": rep["apartment_id"], "reason": reason},
                 actor_role="REGISTRATION", actor_id=operator_id)
    total = _recompute_after_change(conn, rep["attendee_id"])
    return {"entitlement_count": total}


def _recompute_after_change(conn, attendee_id: str) -> int:
    """Resync the attendee's credential and any still-open ledger rows."""
    total = count_active_representations(conn, attendee_id)
    cred = active_credential_for_attendee(conn, attendee_id)
    if cred is None:
        return total
    conn.execute("UPDATE credentials SET entitlement_count = ? WHERE credential_id = ?",
                 (total, cred["credential_id"]))

    # Only resolutions that are still open can be adjusted, and never below what
    # has already been consumed -- that would break eligible >= consumed.
    conn.execute(
        """UPDATE credential_resolution_ledger
              SET eligible_count = MAX(consumed_count, ?)
            WHERE credential_id = ?
              AND resolution_id IN (
                    SELECT resolution_id FROM ballot.resolutions WHERE status = 'VOTING_OPEN')""",
        (total, cred["credential_id"]),
    )
    return total


# --------------------------------------------------------------------------
# ledger
# --------------------------------------------------------------------------

def open_ledger_for_resolution(conn, resolution_id: str) -> int:
    """Snapshot every active credential's entitlement as this resolution opens."""
    creds = conn.execute(
        "SELECT credential_id, entitlement_count FROM credentials WHERE status = 'ACTIVE'"
    ).fetchall()
    total = 0
    for cred in creds:
        if cred["entitlement_count"] <= 0:
            continue
        conn.execute(
            """INSERT OR IGNORE INTO credential_resolution_ledger
                 (credential_id, resolution_id, eligible_count, consumed_count)
               VALUES (?,?,?,0)""",
            (cred["credential_id"], resolution_id, cred["entitlement_count"]),
        )
        total += cred["entitlement_count"]
    return total


def add_credential_to_open_resolutions(conn, credential_id: str, entitlement_count: int) -> None:
    """A voter who checks in mid-meeting can still vote on what is open now."""
    open_rows = conn.execute(
        "SELECT resolution_id FROM ballot.resolutions WHERE status = 'VOTING_OPEN'"
    ).fetchall()
    for row in open_rows:
        conn.execute(
            """INSERT OR IGNORE INTO credential_resolution_ledger
                 (credential_id, resolution_id, eligible_count, consumed_count)
               VALUES (?,?,?,0)""",
            (credential_id, row["resolution_id"], entitlement_count),
        )


def ledger_totals(conn, resolution_id: str) -> dict:
    row = conn.execute(
        """SELECT COALESCE(SUM(eligible_count),0) AS eligible,
                  COALESCE(SUM(consumed_count),0) AS consumed
             FROM credential_resolution_ledger WHERE resolution_id = ?""",
        (resolution_id,),
    ).fetchone()
    eligible = row["eligible"]
    consumed = row["consumed"]
    return {"eligible": eligible, "consumed": consumed, "not_cast": eligible - consumed}


def ledger_row(conn, credential_id: str, resolution_id: str):
    return conn.execute(
        """SELECT * FROM credential_resolution_ledger
            WHERE credential_id = ? AND resolution_id = ?""",
        (credential_id, resolution_id),
    ).fetchone()


# --------------------------------------------------------------------------
# registration views
# --------------------------------------------------------------------------

def apartment_register(conn) -> list[dict]:
    rows = conn.execute(
        """SELECT a.apartment_id, a.eligible, a.owner_display_name, a.eligibility_notes,
                  r.representation_id, r.rep_type, r.proxy_ref, r.attendee_id,
                  t.display_name AS holder_name,
                  c.credential_id, c.entitlement_count
             FROM apartments a
             LEFT JOIN representations r
                    ON r.apartment_id = a.apartment_id AND r.status = 'ACTIVE'
             LEFT JOIN attendees t ON t.attendee_id = r.attendee_id
             LEFT JOIN credentials c
                    ON c.attendee_id = r.attendee_id AND c.status = 'ACTIVE'
            ORDER BY LENGTH(a.apartment_id), a.apartment_id"""
    ).fetchall()
    return [dict(r) for r in rows]


def attendee_register(conn) -> list[dict]:
    rows = conn.execute(
        """SELECT t.attendee_id, t.display_name,
                  c.credential_id, c.status AS credential_status, c.entitlement_count,
                  (SELECT GROUP_CONCAT(r.apartment_id, ', ')
                     FROM representations r
                    WHERE r.attendee_id = t.attendee_id AND r.status = 'ACTIVE') AS apartments,
                  (SELECT COUNT(*) FROM representations r
                    WHERE r.attendee_id = t.attendee_id AND r.status = 'ACTIVE') AS entitlements
             FROM attendees t
             LEFT JOIN credentials c
                    ON c.attendee_id = t.attendee_id AND c.status = 'ACTIVE'
            ORDER BY t.display_name"""
    ).fetchall()
    return [dict(r) for r in rows]


def representation_summary(conn) -> dict:
    row = conn.execute(
        """SELECT
             (SELECT COUNT(*) FROM apartments WHERE eligible = 1) AS eligible_apartments,
             (SELECT COUNT(*) FROM representations WHERE status='ACTIVE') AS represented,
             (SELECT COUNT(*) FROM representations WHERE status='ACTIVE' AND rep_type='OWN') AS own,
             (SELECT COUNT(*) FROM representations WHERE status='ACTIVE' AND rep_type='PROXY') AS proxy,
             (SELECT COALESCE(SUM(entitlement_count),0) FROM credentials WHERE status='ACTIVE')
               AS active_entitlements,
             (SELECT COUNT(*) FROM credentials WHERE status='ACTIVE') AS active_credentials"""
    ).fetchone()
    return dict(row)
