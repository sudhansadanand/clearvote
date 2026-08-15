"""Ballot authorisation and submission -- the heart of the privacy design.

The submission runs as one indivisible transaction across all three databases.
Inside it, N one-time authorisation tokens are minted in memory, each is spent
to insert exactly one anonymous ballot, and the credential's consumed counter
goes up by N. The mapping from token to credential exists only in local
variables and is gone the moment the function returns.

What the system can prove afterwards: N valid entitlements were consumed and
exactly N ballots were accepted. What it cannot prove, by construction: which
ballot came from which code.
"""

from __future__ import annotations

import secrets

from ..util import new_id, now_iso
from . import audit, entitlements, resolutions
from .errors import Conflict, ValidationError, ASSISTANCE

CHOICES = ("FOR", "AGAINST", "ABSTAIN")


def parse_allocation(allocation: dict | None) -> dict:
    if not isinstance(allocation, dict):
        raise ValidationError("Please choose how to cast your votes.",
                              next_action=ASSISTANCE)
    parsed = {}
    for choice in CHOICES:
        raw = allocation.get(choice, 0)
        if isinstance(raw, bool) or not isinstance(raw, int):
            if isinstance(raw, str) and raw.strip().isdigit():
                raw = int(raw.strip())
            else:
                raise ValidationError("Vote numbers must be whole numbers.",
                                      next_action=ASSISTANCE)
        if raw < 0:
            raise ValidationError("Vote numbers cannot be negative.",
                                  next_action=ASSISTANCE)
        parsed[choice] = raw
    unknown = set(allocation) - set(CHOICES)
    if unknown:
        raise ValidationError(f"Unrecognised vote option: {', '.join(sorted(unknown))}.",
                              next_action=ASSISTANCE)
    return parsed


def _check_open_and_current(conn, ident: str, resolution_version, resolution_hash):
    row = resolutions.require(conn, ident)
    if row["status"] != "VOTING_OPEN":
        raise Conflict(
            "Voting on this resolution is not open.",
            next_action="Please wait for the next resolution to be opened.",
            code="not_open",
        )
    # A phone left on an old screen must not be able to vote on stale wording.
    if resolution_version is not None and int(resolution_version) != row["version"]:
        raise Conflict("The wording of this resolution has been updated.",
                       next_action="Please wait a moment; your screen will refresh.",
                       code="stale_resolution")
    if resolution_hash and resolution_hash != row["text_hash"]:
        raise Conflict("The wording of this resolution has been updated.",
                       next_action="Please wait a moment; your screen will refresh.",
                       code="stale_resolution")
    return row


def preview(conn, credential_row, ident: str, allocation: dict,
            resolution_version=None, resolution_hash=None) -> dict:
    """Validate a proposed allocation. Changes nothing at all."""
    row = _check_open_and_current(conn, ident, resolution_version, resolution_hash)
    parsed = parse_allocation(allocation)
    total = sum(parsed.values())

    ledger = entitlements.ledger_row(conn, credential_row["credential_id"],
                                     row["resolution_id"])
    if ledger is None:
        raise Conflict("You are not on the voting list for this resolution.",
                       next_action=ASSISTANCE, code="not_eligible")
    remaining = ledger["eligible_count"] - ledger["consumed_count"]

    if total == 0:
        raise ValidationError("Please allocate at least one vote before continuing.",
                              next_action=ASSISTANCE)
    if total > remaining:
        raise ValidationError(
            f"You have {remaining} vote(s) left for this resolution, "
            f"but {total} were allocated.",
            next_action=ASSISTANCE,
        )

    parts = [f"{count} {choice}" for choice, count in parsed.items() if count]
    return {
        "resolution": row["number"],
        "resolution_title": row["title"],
        "allocation": parsed,
        "total": total,
        "remaining_after": remaining - total,
        "summary": " and ".join(parts),
        "confirm_label": (f"CONFIRM {total} VOTE{'S' if total != 1 else ''} "
                          f"{parts[0].split(' ', 1)[1]}" if len(parts) == 1
                          else "CONFIRM VOTES"),
    }


def submit(conn, credential_row, ident: str, allocation: dict, *,
           client_submission_id: str, resolution_version=None, resolution_hash=None,
           confirmed: bool = False) -> dict:
    """Consume N entitlements and insert N anonymous ballots, atomically."""
    if not confirmed:
        raise ValidationError("Your vote was not confirmed, so nothing was recorded.",
                              next_action="Please tap Confirm to record your vote.")
    if not client_submission_id:
        raise ValidationError("Missing submission reference.", next_action=ASSISTANCE)

    row = _check_open_and_current(conn, ident, resolution_version, resolution_hash)
    resolution_id = row["resolution_id"]
    credential_id = credential_row["credential_id"]

    ledger = entitlements.ledger_row(conn, credential_id, resolution_id)
    if ledger is None:
        raise Conflict("You are not on the voting list for this resolution.",
                       next_action=ASSISTANCE, code="not_eligible")

    # Step 4 of the algorithm: a retry after a dropped connection replays the
    # original outcome instead of casting a second time.
    if ledger["last_submission_id"] and ledger["last_submission_id"] == client_submission_id:
        return {
            "status": "RECORDED",
            "resolution": row["number"],
            "entitlements_recorded": ledger["last_recorded_count"] or 0,
            "remaining_entitlements": ledger["eligible_count"] - ledger["consumed_count"],
            "receipt_id": ledger["last_receipt_id"],
            "duplicate": True,
        }

    parsed = parse_allocation(allocation)
    total = sum(parsed.values())
    remaining = ledger["eligible_count"] - ledger["consumed_count"]
    if total == 0:
        raise ValidationError("Please allocate at least one vote before confirming.",
                              next_action=ASSISTANCE)
    if total > remaining:
        raise ValidationError(
            f"You have {remaining} vote(s) left for this resolution, "
            f"but {total} were submitted.",
            next_action=ASSISTANCE,
        )

    # Steps 7-8: one-time tokens, held only in this local list.
    tokens = [secrets.token_bytes(32) for _ in range(total)]
    spent: set[bytes] = set()
    accepted_at = now_iso()
    queue = [choice for choice in CHOICES for _ in range(parsed[choice])]

    for token, choice in zip(tokens, queue):
        if token in spent:  # a token may authorise exactly one ballot
            raise Conflict("Vote authorisation failed.", next_action=ASSISTANCE)
        spent.add(token)
        conn.execute(
            """INSERT INTO ballot.ballots (ballot_id, resolution_id, choice, accepted_at)
               VALUES (?,?,?,?)""",
            (new_id(), resolution_id, choice, accepted_at),
        )

    receipt_id = "VOTE-" + secrets.token_hex(3).upper()
    conn.execute(
        """UPDATE credential_resolution_ledger
              SET consumed_count = consumed_count + ?,
                  last_submission_id = ?, last_receipt_id = ?, last_recorded_count = ?
            WHERE credential_id = ? AND resolution_id = ?""",
        (total, client_submission_id, receipt_id, total, credential_id, resolution_id),
    )

    # Aggregate only. Nothing here ties the count to the credential that cast it.
    audit.append(conn, "ENTITLEMENTS_CONSUMED",
                 {"resolution_id": resolution_id, "resolution_number": row["number"],
                  "count": total})

    tokens.clear()
    spent.clear()

    return {
        "status": "RECORDED",
        "resolution": row["number"],
        "entitlements_recorded": total,
        "remaining_entitlements": remaining - total,
        "receipt_id": receipt_id,
        "duplicate": False,
    }


# --------------------------------------------------------------------------
# voter-facing state
# --------------------------------------------------------------------------

def voter_state(conn, credential_row, cfg) -> dict:
    agm = conn.execute("SELECT * FROM agms LIMIT 1").fetchone()
    row = resolutions.active(conn)
    state = {
        "entitlement_count": credential_row["entitlement_count"],
        "agm_title": agm["title"] if agm else "AGM",
        "agm_status": agm["status"] if agm else "SETUP",
        "poll_interval_ms": cfg.poll_interval_ms,
        "screen": "waiting",
        "resolution": None,
        "remaining": 0,
        "recorded": 0,
    }

    if row is None:
        latest = conn.execute(
            """SELECT number, status FROM ballot.resolutions
                WHERE superseded_by IS NULL AND status <> 'DRAFT'
                ORDER BY seq DESC LIMIT 1"""
        ).fetchone()
        state["current_item"] = (
            f"{latest['number']} - {latest['status'].replace('_', ' ').lower()}"
            if latest else "no resolution open yet"
        )
        return state

    ledger = entitlements.ledger_row(conn, credential_row["credential_id"],
                                     row["resolution_id"])
    eligible = ledger["eligible_count"] if ledger else 0
    consumed = ledger["consumed_count"] if ledger else 0
    remaining = eligible - consumed

    state.update({
        "current_item": f"{row['number']} - voting open",
        "resolution": {
            "resolution_id": row["resolution_id"],
            "number": row["number"],
            "version": row["version"],
            "title": row["title"],
            "full_text": row["full_text"],
            "text_hash": row["text_hash"],
            "voting_rule": row["voting_rule"],
        },
        "remaining": remaining,
        "recorded": consumed,
        "eligible_here": eligible,
    })
    if ledger is None:
        state["screen"] = "not_eligible"
    elif remaining > 0:
        state["screen"] = "vote"
    else:
        state["screen"] = "recorded"
    return state
