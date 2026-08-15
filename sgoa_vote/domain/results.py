"""Closing a resolution: recount, reconcile, then decide.

The order matters. Counts are always recomputed from the ballot rows themselves,
never from a running total, so a corrupted counter cannot quietly decide an AGM.
Reconciliation runs before the rule is applied: if the books do not balance,
no outcome is produced at all.
"""

from __future__ import annotations

import hashlib

from ..config import canonical_json
from ..util import new_id, now_iso
from . import audit, entitlements, resolutions
from .errors import Conflict

RULE_LABELS = resolutions.VOTING_RULES


def counts(conn, resolution_id: str) -> dict:
    rows = conn.execute(
        "SELECT choice, COUNT(*) AS n FROM ballot.ballots WHERE resolution_id = ? GROUP BY choice",
        (resolution_id,),
    ).fetchall()
    tally = {"FOR": 0, "AGAINST": 0, "ABSTAIN": 0}
    for row in rows:
        tally[row["choice"]] = row["n"]
    tally["CAST"] = tally["FOR"] + tally["AGAINST"] + tally["ABSTAIN"]
    return tally


def apply_rule(rule: str, tally: dict, eligible: int) -> str:
    for_count, against = tally["FOR"], tally["AGAINST"]

    if rule == "FOR_GT_AGAINST":
        if for_count > against:
            return "PASSED"
        if for_count < against:
            return "FAILED"
        return "TIED"

    if rule == "TWO_THIRDS_OF_CAST":
        decisive = for_count + against
        if decisive == 0:
            return "FAILED"
        # FOR >= ceil(2/3 * decisive), expressed without floating point.
        return "PASSED" if for_count * 3 >= decisive * 2 else "FAILED"

    if rule == "MAJORITY_OF_ALL_ELIGIBLE":
        return "PASSED" if for_count * 2 > eligible else "FAILED"

    raise Conflict(f"Unknown voting rule '{rule}'.")


def reconciliation(conn, resolution_id: str) -> dict:
    """The two equalities of invariant I3, reported explicitly."""
    tally = counts(conn, resolution_id)
    ledger = entitlements.ledger_totals(conn, resolution_id)

    check_entitlements = ledger["consumed"] + ledger["not_cast"] == ledger["eligible"]
    check_ballots = tally["CAST"] == ledger["consumed"]
    check_choices = tally["FOR"] + tally["AGAINST"] + tally["ABSTAIN"] == tally["CAST"]

    return {
        "eligible": ledger["eligible"],
        "consumed": ledger["consumed"],
        "not_cast": ledger["not_cast"],
        "ballot_rows": tally["CAST"],
        "for": tally["FOR"],
        "against": tally["AGAINST"],
        "abstain": tally["ABSTAIN"],
        "check_entitlements": check_entitlements,
        "check_ballots": check_ballots,
        "check_choices": check_choices,
        "ok": check_entitlements and check_ballots and check_choices,
        "labels": {
            "check_entitlements":
                f"{ledger['consumed']} cast + {ledger['not_cast']} not cast = {ledger['eligible']} eligible",
            "check_ballots":
                f"{tally['CAST']} ballot rows = {ledger['consumed']} entitlements consumed",
            "check_choices":
                f"{tally['FOR']} FOR + {tally['AGAINST']} AGAINST + {tally['ABSTAIN']} ABSTAIN "
                f"= {tally['CAST']} cast",
        },
    }


def participation(conn, ident: str) -> dict:
    """Eligible / cast / not-cast only. Never a breakdown by choice."""
    row = resolutions.require(conn, ident)
    ledger = entitlements.ledger_totals(conn, row["resolution_id"])
    eligible = ledger["eligible"]
    return {
        "resolution": row["number"],
        "resolution_id": row["resolution_id"],
        "title": row["title"],
        "status": row["status"],
        "eligible_entitlements": eligible,
        "votes_received": ledger["consumed"],
        "not_yet_cast": ledger["not_cast"],
        "percent": round(100 * ledger["consumed"] / eligible) if eligible else 0,
    }


def snapshot_hash(payload: dict) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def close_voting(conn, ident: str, operator_id: str | None = None, *,
                 auto_publish: bool = True) -> dict:
    """Close voting, recount, reconcile, decide -- and publish.

    `auto_publish` releases the result to the projector in the same transaction
    that produces it, so the hall sees the count at the moment it is made rather
    than when the MC chooses to reveal it. That removes the window in which one
    person alone knows the outcome, which is the window an observer would be
    entitled to be suspicious about.

    It never publishes a result that failed reconciliation: there is no outcome
    to publish in that case, and the projector says so.
    """
    row = resolutions.require(conn, ident)
    if row["status"] != "VOTING_OPEN":
        raise Conflict(f"Resolution {row['number']} is not open for voting.")
    resolution_id = row["resolution_id"]

    conn.execute(
        "UPDATE ballot.resolutions SET status='VOTING_CLOSED', closed_at = ? WHERE resolution_id = ?",
        (now_iso(), resolution_id),
    )
    resolutions._event(conn, resolution_id, "VOTING_CLOSED", operator_id)

    rec = reconciliation(conn, resolution_id)

    if not rec["ok"]:
        conn.execute("UPDATE ballot.resolutions SET status='RECONCILIATION_ERROR' "
                     "WHERE resolution_id = ?", (resolution_id,))
        audit.append(conn, "VOTING_CLOSED",
                     {"resolution_number": row["number"], "outcome": "RECONCILIATION_ERROR",
                      "eligible": rec["eligible"], "cast": rec["consumed"],
                      "ballot_rows": rec["ballot_rows"]},
                     actor_role="MC", actor_id=operator_id)
        audit.create_checkpoint(conn, f"{row['number']} closed with reconciliation error")
        # Nothing is published: there is no outcome, and the hall is told exactly
        # that rather than being shown a number the system will not stand behind.
        return {"status": "RECONCILIATION_ERROR", "reconciliation": rec,
                "resolution": row["number"], "published": False}

    outcome = apply_rule(row["voting_rule"], {"FOR": rec["for"], "AGAINST": rec["against"],
                                              "ABSTAIN": rec["abstain"]}, rec["eligible"])

    material = {
        "resolution_number": row["number"],
        "version": row["version"],
        "text_hash": row["text_hash"],
        "rule": row["voting_rule"],
        "for": rec["for"], "against": rec["against"], "abstain": rec["abstain"],
        "cast": rec["consumed"], "not_cast": rec["not_cast"], "eligible": rec["eligible"],
        "outcome": outcome,
    }
    digest = snapshot_hash(material)

    conn.execute(
        """INSERT INTO ballot.result_snapshots
             (resolution_id, for_count, against_count, abstain_count, cast_count,
              not_cast_count, eligible_count, outcome, rule_applied, snapshot_hash, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (resolution_id, rec["for"], rec["against"], rec["abstain"], rec["ballot_rows"],
         rec["not_cast"], rec["eligible"], outcome, row["voting_rule"], digest, now_iso()),
    )
    conn.execute("UPDATE ballot.resolutions SET status = ? WHERE resolution_id = ?",
                 (outcome, resolution_id))

    audit.append(conn, "VOTING_CLOSED",
                 {"resolution_number": row["number"], "eligible": rec["eligible"],
                  "cast": rec["consumed"], "not_cast": rec["not_cast"]},
                 actor_role="MC", actor_id=operator_id)
    audit.append(conn, "RESULT_SNAPSHOT_CREATED",
                 {"resolution_number": row["number"], "outcome": outcome,
                  "for": rec["for"], "against": rec["against"], "abstain": rec["abstain"],
                  "snapshot_hash": digest},
                 actor_role="MC", actor_id=operator_id)
    published = False
    if auto_publish:
        resolutions._event(conn, resolution_id, "RESULT_SHOWN", operator_id)
        published = True

    audit.create_checkpoint(conn, f"{row['number']} closed: {outcome}")

    return {"status": outcome, "reconciliation": rec, "resolution": row["number"],
            "snapshot_hash": digest, "published": published}


def result_for(conn, ident: str) -> dict | None:
    row = resolutions.require(conn, ident)
    snap = conn.execute("SELECT * FROM ballot.result_snapshots WHERE resolution_id = ?",
                        (row["resolution_id"],)).fetchone()
    if snap is None:
        return None
    data = dict(snap)
    data.update({
        "resolution": row["number"],
        "title": row["title"],
        "version": row["version"],
        "text_hash": row["text_hash"],
        "full_text": row["full_text"],
        "rule_label": RULE_LABELS.get(row["voting_rule"], row["voting_rule"]),
        "disposition_note": row["disposition_note"],
        "opened_at": row["opened_at"],
        "closed_at": row["closed_at"],
    })
    return data


def show_result(conn, ident: str, operator_id: str | None = None) -> dict:
    """Marks the point at which the MC put the result on the projector."""
    row = resolutions.require(conn, ident)
    if row["status"] in ("DRAFT", "FINALIZED", "VOTING_OPEN"):
        raise Conflict("Close the voting before showing the result.")
    if row["status"] == "RECONCILIATION_ERROR":
        raise Conflict(
            f"Resolution {row['number']} did not reconcile, so there is no result to "
            "show. The scrutineers must investigate before anything is announced.")
    if not is_result_shown(conn, row["resolution_id"]):
        resolutions._event(conn, row["resolution_id"], "RESULT_SHOWN", operator_id)
    return {"resolution": row["number"], "shown": True}


def is_result_shown(conn, resolution_id: str) -> bool:
    return conn.execute(
        """SELECT 1 FROM ballot.resolution_events
            WHERE resolution_id = ? AND event_type = 'RESULT_SHOWN' LIMIT 1""",
        (resolution_id,),
    ).fetchone() is not None
