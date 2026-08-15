"""One asserting test per acceptance criterion, AC-01 to AC-14.

AC-04, AC-06 and AC-07 are asserted in tests/privacy, where the raw-database
inspection they need already lives. AC-15 is a human usability check and is
scripted in the README instead.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from pathlib import Path

import pytest
from helpers import (ballot_count, choice_counts, credential_id_for, ledger_totals,
                     representation_id_for)


# -- AC-01 ------------------------------------------------------------------

def test_ac01_only_one_active_representation_can_exist_per_apartment(seeded, registrar):
    """A2 is already held by proxy in the seed data."""
    response = registrar.post("/api/v1/registration/representations",
                              {"apartment_id": "A2", "attendee_name": "Someone Else",
                               "rep_type": "OWN"})
    assert response.status_code == 409
    assert "already represented" in response.json()["message"]

    # And the database refuses it too, not just the application.
    conn = sqlite3.connect(seeded.db.paths["eligibility"])
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """INSERT INTO representations
                     (representation_id, apartment_id, attendee_id, rep_type, status, created_at)
                   VALUES ('rep_x','A2','att_x','OWN','ACTIVE','now')""")
    finally:
        conn.close()


def test_ac01_an_override_supersedes_rather_than_duplicates(seeded, registrar):
    response = registrar.post("/api/v1/registration/representations",
                              {"apartment_id": "A2", "attendee_name": "Owner Of A2",
                               "rep_type": "OWN",
                               "override_reason": "owner attended; proxy withdrawn by chair"})
    assert response.status_code == 200

    with seeded.db.reader() as conn:
        rows = conn.execute(
            "SELECT status FROM representations WHERE apartment_id = 'A2'").fetchall()
    statuses = sorted(r["status"] for r in rows)
    assert statuses == ["ACTIVE", "SUPERSEDED"]


# -- AC-02 ------------------------------------------------------------------

def test_ac02_a_credential_cannot_consume_more_than_it_was_assigned(
        seeded, open_resolution, voter_factory):
    ident = open_resolution("R1")
    voter = voter_factory(seeded.demo_codes["Sunil Menon"])       # 2 entitlements

    assert voter.vote(ident, {"FOR": 5}).status_code == 422
    assert voter.vote(ident, {"FOR": 2}).status_code == 200
    assert voter.vote(ident, {"FOR": 1}).status_code == 422

    credential = credential_id_for(seeded, "Sunil Menon")
    with seeded.db.reader() as conn:
        row = conn.execute(
            """SELECT eligible_count, consumed_count FROM credential_resolution_ledger
                WHERE credential_id = ? AND resolution_id = ?""",
            (credential, ident)).fetchone()
    assert row["consumed_count"] == 2 == row["eligible_count"]


# -- AC-03 ------------------------------------------------------------------

def test_ac03_submission_creates_exactly_n_ballots_and_consumes_exactly_n(
        seeded, open_resolution, voter_factory):
    ident = open_resolution("R1")
    voter_factory(seeded.demo_codes["Meera Raghavan"]).vote(ident, {"FOR": 2, "ABSTAIN": 1})

    assert ballot_count(seeded, ident) == 3
    assert ledger_totals(seeded, ident)["consumed"] == 3
    assert choice_counts(seeded, ident) == {"FOR": 2, "AGAINST": 0, "ABSTAIN": 1}


def test_ac03_a_failure_part_way_through_records_nothing_at_all(
        seeded, open_resolution, voter_factory, monkeypatch):
    """The all-or-nothing guarantee, tested by breaking the last step."""
    ident = open_resolution("R1")
    voter = voter_factory(seeded.demo_codes["Meera Raghavan"])

    from sgoa_vote.domain import voting

    def explode(*args, **kwargs):
        raise RuntimeError("simulated crash after the ballots were inserted")

    monkeypatch.setattr(voting.audit, "append", explode)

    with pytest.raises(RuntimeError):
        voter.vote(ident, {"FOR": 3})

    # No half-written vote: no ballots, and no entitlement marked as used.
    assert ballot_count(seeded, ident) == 0
    assert ledger_totals(seeded, ident)["consumed"] == 0

    monkeypatch.undo()
    assert voter.vote(ident, {"FOR": 3}).status_code == 200
    assert ballot_count(seeded, ident) == 3


# -- AC-05 ------------------------------------------------------------------

def test_ac05_a_repeated_idempotency_key_creates_no_extra_ballots(
        seeded, open_resolution, voter_factory):
    ident = open_resolution("R1")
    voter = voter_factory(seeded.demo_codes["Meera Raghavan"])
    key = str(uuid.uuid4())

    first = voter.vote(ident, {"FOR": 3}, submission_id=key).json()
    for _ in range(5):
        again = voter.vote(ident, {"AGAINST": 3}, submission_id=key).json()
        assert again["receipt_id"] == first["receipt_id"]

    # Note the retries asked for AGAINST; the original FOR result stands.
    assert choice_counts(seeded, ident) == {"FOR": 3, "AGAINST": 0, "ABSTAIN": 0}


# -- AC-08 ------------------------------------------------------------------

def test_ac08_results_satisfy_both_reconciliation_equalities(
        seeded, open_resolution, close_resolution, voter_factory, mc):
    ident = open_resolution("R1")
    voter_factory(seeded.demo_codes["Meera Raghavan"]).vote(ident, {"FOR": 2, "AGAINST": 1})
    voter_factory(seeded.demo_codes["Kavitha Iyer"]).vote(ident, {"ABSTAIN": 1})
    # everyone else abstains from voting entirely

    outcome = close_resolution(ident)
    rec = outcome["reconciliation"]

    assert rec["consumed"] + rec["not_cast"] == rec["eligible"]
    assert rec["for"] + rec["against"] + rec["abstain"] == rec["ballot_rows"]
    assert rec["ballot_rows"] == rec["consumed"]
    assert rec["ok"] is True

    result = mc.get(f"/api/v1/admin/results/{ident}").json()["result"]
    assert result["cast_count"] + result["not_cast_count"] == result["eligible_count"]
    assert (result["for_count"] + result["against_count"] + result["abstain_count"]
            == result["cast_count"])


def test_ac08_a_broken_reconciliation_produces_no_outcome(
        seeded, open_resolution, voter_factory, mc):
    """Corrupt the ledger behind the application's back, then close."""
    ident = open_resolution("R1")
    voter_factory(seeded.demo_codes["Meera Raghavan"]).vote(ident, {"FOR": 3})

    conn = sqlite3.connect(seeded.db.paths["eligibility"])
    try:  # claim one more entitlement was consumed than there are ballots
        conn.execute(
            """UPDATE credential_resolution_ledger
                  SET eligible_count = eligible_count + 1, consumed_count = consumed_count + 1
                WHERE resolution_id = ? AND consumed_count > 0""", (ident,))
        conn.commit()
    finally:
        conn.close()

    outcome = mc.post(f"/api/v1/admin/resolutions/{ident}/close").json()
    assert outcome["status"] == "RECONCILIATION_ERROR"
    assert outcome["reconciliation"]["check_ballots"] is False

    shown = mc.get(f"/api/v1/admin/results/{ident}").json()
    assert shown["status"] == "RECONCILIATION_ERROR"
    assert shown["result"] is None          # no PASSED or FAILED is ever published


# -- AC-09 ------------------------------------------------------------------

def test_ac09_default_rule_is_for_greater_than_against_and_equality_is_tied(
        seeded, open_resolution, close_resolution, voter_factory):
    ident = open_resolution("R1")
    # 3 FOR against 3 AGAINST, plus an abstention that must not break the tie.
    voter_factory(seeded.demo_codes["Meera Raghavan"]).vote(ident, {"FOR": 3})
    voter_factory(seeded.demo_codes["Sunil Menon"]).vote(ident, {"AGAINST": 2})
    voter_factory(seeded.demo_codes["Kavitha Iyer"]).vote(ident, {"AGAINST": 1})
    voter_factory(seeded.demo_codes["Rajesh Nair"]).vote(ident, {"ABSTAIN": 1})

    outcome = close_resolution(ident)
    assert outcome["status"] == "TIED"

    with seeded.db.reader() as conn:
        row = conn.execute("SELECT status FROM ballot.resolutions WHERE resolution_id = ?",
                           (ident,)).fetchone()
    assert row["status"] == "TIED"          # never silently resolved


def test_ac09_a_tie_can_carry_a_disposition_note_recorded_separately(
        seeded, open_resolution, close_resolution, voter_factory, mc):
    ident = open_resolution("R1")
    voter_factory(seeded.demo_codes["Meera Raghavan"]).vote(ident, {"FOR": 3})
    voter_factory(seeded.demo_codes["Sunil Menon"]).vote(ident, {"AGAINST": 2})
    voter_factory(seeded.demo_codes["Kavitha Iyer"]).vote(ident, {"AGAINST": 1})
    assert close_resolution(ident)["status"] == "TIED"

    note = "Chair declined to exercise a casting vote; item deferred to an EGM."
    assert mc.post(f"/api/v1/admin/resolutions/{ident}/disposition",
                   {"note": note}).status_code == 200

    with seeded.db.reader() as conn:
        row = conn.execute(
            "SELECT status, disposition_note FROM ballot.resolutions WHERE resolution_id = ?",
            (ident,)).fetchone()
    assert row["status"] == "TIED"          # the note does not change the outcome
    assert note in row["disposition_note"]


# -- AC-10 ------------------------------------------------------------------

def test_ac10_the_wording_voters_saw_is_the_wording_in_the_report(
        seeded, mc, find_resolution, close_resolution, voter_factory, admin):
    ident = find_resolution("R2")
    mc.post(f"/api/v1/admin/resolutions/{ident}/finalize")
    amended = mc.post(f"/api/v1/admin/resolutions/{ident}/amend",
                      {"full_text": "The wording actually put to the vote."}).json()
    mc.post(f"/api/v1/admin/resolutions/{amended['resolution_id']}/open")

    voter = voter_factory(seeded.demo_codes["Kavitha Iyer"])
    seen = voter.state()["resolution"]
    assert seen["version"] == 2
    assert seen["full_text"] == "The wording actually put to the vote."
    voter.vote(amended["resolution_id"], {"FOR": 1})
    close_resolution(amended["resolution_id"])

    from sgoa_vote.domain import reports
    with seeded.db.reader() as conn:
        report = reports.gather_report(conn, seeded.config)

    section = next(s for s in report["resolutions"] if s["number"] == "R2")
    assert section["version"] == seen["version"]
    assert section["full_text"] == seen["full_text"]
    assert section["text_hash"] == seen["text_hash"]


# -- AC-11 ------------------------------------------------------------------

def test_ac11_withdrawn_and_not_put_to_vote_stay_visible_with_their_numbering(
        seeded, mc, find_resolution):
    withdrawn = find_resolution("R4")
    skipped = find_resolution("R5")
    mc.post(f"/api/v1/admin/resolutions/{withdrawn}/finalize")
    assert mc.post(f"/api/v1/admin/resolutions/{withdrawn}/withdraw",
                   {"note": "superseded by a committee decision"}).status_code == 200
    assert mc.post(f"/api/v1/admin/resolutions/{skipped}/not-put-to-vote",
                   {"note": "ran out of time"}).status_code == 200

    listing = mc.get("/api/v1/admin/resolutions").json()["resolutions"]
    numbers = [r["number"] for r in listing]
    assert numbers == ["R1", "R2", "R3", "R4", "R5"]      # nothing renumbered

    by_number = {r["number"]: r for r in listing}
    assert by_number["R4"]["status"] == "WITHDRAWN"
    assert by_number["R5"]["status"] == "NOT_PUT_TO_VOTE"
    assert by_number["R4"]["disposition_note"] == "superseded by a committee decision"


# -- AC-12 ------------------------------------------------------------------

def test_ac12_no_page_or_asset_references_the_internet():
    root = Path(__file__).resolve().parents[2] / "sgoa_vote" / "web"
    offenders = []
    for path in list(root.rglob("*.html")) + list(root.rglob("*.css")) + list(root.rglob("*.js")):
        text = path.read_text(encoding="utf-8")
        for marker in ("http://", "https://", "//cdn.", "fonts.googleapis"):
            if marker in text:
                offenders.append(f"{path.name}: {marker}")
    assert offenders == [], f"external references found: {offenders}"


def test_ac12_a_full_voting_cycle_completes_with_outbound_networking_blocked(
        seeded, open_resolution, close_resolution, voter_factory, monkeypatch):
    """Any attempt to reach off-box during a vote fails the test outright."""
    import socket

    real_connect = socket.socket.connect

    def guarded(self, address, *args, **kwargs):
        host = address[0] if isinstance(address, tuple) else str(address)
        if host not in ("127.0.0.1", "::1", "localhost"):
            raise AssertionError(f"outbound connection attempted to {host}")
        return real_connect(self, address, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", guarded)

    ident = open_resolution("R1")
    voter_factory(seeded.demo_codes["Meera Raghavan"]).vote(ident, {"FOR": 3})
    voter_factory(seeded.demo_codes["Kavitha Iyer"]).vote(ident, {"AGAINST": 1})
    outcome = close_resolution(ident)

    assert outcome["status"] == "PASSED"
    assert ballot_count(seeded, ident) == 4


# -- AC-13 ------------------------------------------------------------------

def test_ac13_a_credential_reset_never_restores_consumed_votes(
        seeded, open_resolution, voter_factory, registrar):
    ident = open_resolution("R1")
    voter_factory(seeded.demo_codes["Meera Raghavan"]).vote(ident, {"FOR": 3})
    assert ballot_count(seeded, ident) == 3

    credential = credential_id_for(seeded, "Meera Raghavan")
    replacement = registrar.post(f"/api/v1/registration/credentials/{credential}/reset",
                                 {"reason": "phone battery died"}).json()

    resumed = voter_factory(replacement["code"])
    assert resumed.joined
    assert resumed.state()["remaining"] == 0
    assert resumed.state()["screen"] == "recorded"

    assert resumed.vote(ident, {"FOR": 1}).status_code == 422
    assert ballot_count(seeded, ident) == 3        # still three, not six


# -- AC-14 ------------------------------------------------------------------

def test_ac14_the_final_report_validates_against_the_database_and_chain_hashes(
        seeded, open_resolution, close_resolution, voter_factory, mc, admin):
    ident = open_resolution("R1")
    voter_factory(seeded.demo_codes["Meera Raghavan"]).vote(ident, {"FOR": 2, "AGAINST": 1})
    voter_factory(seeded.demo_codes["Kavitha Iyer"]).vote(ident, {"FOR": 1})
    close_resolution(ident)

    response = admin.post("/api/v1/admin/reports/final", {"password": "sgoa-demo"})
    assert response.status_code == 200, response.text
    bundle = Path(response.json()["path"])

    expected = {"final_report.pdf", "final_report.html", "eligibility.db", "ballot.db",
                "audit.db", "manifest.json", "checksums.sha256", "software_version.txt",
                "configuration_export.json", "audit_chain_verification.txt",
                "README_ARCHIVE.txt"}
    assert expected <= {p.name for p in bundle.iterdir()}

    # 1. Every checksum in the bundle matches the file it names.
    for line in (bundle / "checksums.sha256").read_text().strip().splitlines():
        digest, name = line.split("  ", 1)
        actual = hashlib.sha256((bundle / name).read_bytes()).hexdigest()
        assert actual == digest, f"{name} does not match its recorded checksum"

    # 2. The manifest's database hashes match the archived copies.
    manifest = json.loads((bundle / "manifest.json").read_text())
    for name, digest in manifest["database_hashes"].items():
        assert hashlib.sha256((bundle / name).read_bytes()).hexdigest() == digest

    # 3. The audit chain in the archived copy verifies, and its head is the head
    #    the report quotes.
    archived = sqlite3.connect(bundle / "audit.db")
    archived.row_factory = sqlite3.Row
    try:
        rows = archived.execute(
            "SELECT * FROM audit_log ORDER BY sequence").fetchall()
        from sgoa_vote.domain.audit import GENESIS_HASH, compute_hash

        prev = GENESIS_HASH
        for row in rows:
            recomputed = compute_hash(prev, row["sequence"], row["timestamp"],
                                      row["event_type"], json.loads(row["payload_json"]))
            assert recomputed == row["event_hash"], f"broken at {row['sequence']}"
            prev = row["event_hash"]
        assert prev == manifest["audit_chain_head"]
    finally:
        archived.close()

    # 4. The counts in the manifest are the counts in the ballot database.
    section = next(r for r in manifest["resolutions"] if r["number"] == "R1")
    assert section == {**section, "for": 3, "against": 1, "abstain": 0}
    assert section["for"] + section["against"] + section["abstain"] \
        + section["not_cast"] == section["eligible"]


def test_ac14_the_report_refuses_to_run_while_a_resolution_is_open(
        seeded, open_resolution, admin):
    open_resolution("R1")
    response = admin.post("/api/v1/admin/reports/final", {"password": "sgoa-demo"})
    assert response.status_code == 409
    assert "still open" in response.json()["message"]


def test_ac14_the_report_requires_re_authentication(seeded, admin):
    response = admin.post("/api/v1/admin/reports/final", {"password": "wrong-password"})
    assert response.status_code == 401
