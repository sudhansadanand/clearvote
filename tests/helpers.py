"""Direct database inspection used by the tests to check what was really stored."""

from __future__ import annotations


def ballot_rows(svc, resolution_id: str) -> list:
    with svc.db.reader() as conn:
        return conn.execute(
            "SELECT * FROM ballot.ballots WHERE resolution_id = ?", (resolution_id,)
        ).fetchall()


def ballot_count(svc, resolution_id: str) -> int:
    return len(ballot_rows(svc, resolution_id))


def choice_counts(svc, resolution_id: str) -> dict:
    tally = {"FOR": 0, "AGAINST": 0, "ABSTAIN": 0}
    for row in ballot_rows(svc, resolution_id):
        tally[row["choice"]] += 1
    return tally


def ledger_totals(svc, resolution_id: str) -> dict:
    with svc.db.reader() as conn:
        row = conn.execute(
            """SELECT COALESCE(SUM(eligible_count),0) AS eligible,
                      COALESCE(SUM(consumed_count),0) AS consumed
                 FROM credential_resolution_ledger WHERE resolution_id = ?""",
            (resolution_id,)).fetchone()
    return {"eligible": row["eligible"], "consumed": row["consumed"]}


def credential_id_for(svc, attendee_name: str) -> str:
    with svc.db.reader() as conn:
        row = conn.execute(
            """SELECT c.credential_id FROM credentials c
                 JOIN attendees a ON a.attendee_id = c.attendee_id
                WHERE a.display_name = ? AND c.status = 'ACTIVE'""",
            (attendee_name,)).fetchone()
    assert row is not None, f"no active credential for {attendee_name}"
    return row["credential_id"]


def representation_id_for(svc, apartment_id: str) -> str:
    with svc.db.reader() as conn:
        row = conn.execute(
            """SELECT representation_id FROM representations
                WHERE apartment_id = ? AND status = 'ACTIVE'""", (apartment_id,)).fetchone()
    assert row is not None, f"no active representation for {apartment_id}"
    return row["representation_id"]
