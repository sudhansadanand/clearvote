"""The dress rehearsal: a complete simulated AGM checked against an answer key.

Covers, in one run: a three-entitlement proxy holder, a mixed allocation, a
retry after a simulated network drop, a credential reset between resolutions,
an abstention, deliberate non-voters, an exact tie, an amended resolution, a
withdrawn resolution, a special two-thirds majority, and the certification
bundle at the end.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path

from helpers import ballot_count, choice_counts, credential_id_for

# What a scrutineer with a pen and paper would independently arrive at.
ANSWER_KEY = {
    "R1": {"for": 7, "against": 1, "abstain": 1, "cast": 9,
           "not_cast": 6, "eligible": 15, "outcome": "PASSED"},
    "R2": {"for": 3, "against": 0, "abstain": 0, "cast": 3,
           "not_cast": 12, "eligible": 15, "outcome": "PASSED"},
    "R3": {"for": 3, "against": 3, "abstain": 1, "cast": 7,
           "not_cast": 8, "eligible": 15, "outcome": "TIED"},
    "R5": {"for": 4, "against": 2, "abstain": 0, "cast": 6,
           "not_cast": 9, "eligible": 15, "outcome": "PASSED"},
}

XSS_PAYLOAD = "<script>alert('resolution seven')</script>"


def test_full_mock_agm_reconciles_against_the_answer_key(
        seeded, mc, admin, registrar, scrutineer, find_resolution, voter_factory):
    codes = dict(seeded.demo_codes)

    # Each attendee has one phone and keeps it for the whole meeting, exactly as
    # in the hall. Joining twice with the same code is refused by design, so the
    # test must not re-join between resolutions either.
    phones = {name: voter_factory(code) for name, code in codes.items()}
    for name, phone in phones.items():
        assert phone.joined, f"{name} could not join with {codes[name]}"

    def cast(name, ident, allocation):
        response = phones[name].vote(ident, allocation)
        assert response.status_code == 200, f"{name}: {response.text}"
        return response.json()

    def run(number, votes, *, expect):
        """Open, collect the listed votes, close, and check the outcome."""
        ident = find_resolution(number)
        assert mc.post(f"/api/v1/admin/resolutions/{ident}/finalize").status_code in (200, 409)
        assert mc.post(f"/api/v1/admin/resolutions/{ident}/open").status_code == 200
        for name, allocation in votes:
            cast(name, ident, allocation)
        outcome = mc.post(f"/api/v1/admin/resolutions/{ident}/close").json()
        assert outcome["status"] == expect, f"{number}: {outcome}"
        return ident

    # -- R1: a proxy holder retries after the wi-fi drops, plus one abstention
    r1 = find_resolution("R1")
    mc.post(f"/api/v1/admin/resolutions/{r1}/finalize")
    mc.post(f"/api/v1/admin/resolutions/{r1}/open")

    retry_key = str(uuid.uuid4())
    first = phones["Meera Raghavan"].vote(r1, {"FOR": 3}, submission_id=retry_key)
    assert first.status_code == 200
    # The phone never saw the reply, so the browser sends it again.
    again = phones["Meera Raghavan"].vote(r1, {"FOR": 3}, submission_id=retry_key)
    assert again.json()["receipt_id"] == first.json()["receipt_id"]

    cast("Sunil Menon", r1, {"FOR": 2})
    cast("Kavitha Iyer", r1, {"AGAINST": 1})
    cast("Rajesh Nair", r1, {"ABSTAIN": 1})
    cast("Fatima Sheikh", r1, {"FOR": 1})
    cast("George Mathew", r1, {"FOR": 1})
    # Anjali, Prakash, Lakshmi, Imran, Deepa and Thomas do not vote at all.

    participation = mc.get(f"/api/v1/admin/participation/{r1}").json()
    assert participation["votes_received"] == 9
    assert participation["not_yet_cast"] == 6

    assert mc.post(f"/api/v1/admin/resolutions/{r1}/close").json()["status"] == "PASSED"
    assert ballot_count(seeded, r1) == 9

    # -- a lost card between resolutions: reset must not restore R1's votes
    credential = credential_id_for(seeded, "Meera Raghavan")
    replacement = registrar.post(f"/api/v1/registration/credentials/{credential}/reset",
                                 {"reason": "card left on a chair"}).json()
    codes["Meera Raghavan"] = replacement["code"]
    phones["Meera Raghavan"] = voter_factory(codes["Meera Raghavan"])
    assert phones["Meera Raghavan"].joined
    assert phones["Meera Raghavan"].state()["entitlement_count"] == 3

    # -- R2: amended on the floor before voting opens
    r2 = find_resolution("R2")
    mc.post(f"/api/v1/admin/resolutions/{r2}/finalize")
    amended = mc.post(f"/api/v1/admin/resolutions/{r2}/amend", {
        "full_text": "That the Association award the external painting contract to the "
                     "recommended contractor, subject to a five-year warranty as agreed "
                     "on the floor of this meeting."}).json()
    assert amended["version"] == 2
    r2 = amended["resolution_id"]
    mc.post(f"/api/v1/admin/resolutions/{r2}/open")
    cast("Sunil Menon", r2, {"FOR": 2})
    cast("Kavitha Iyer", r2, {"FOR": 1})
    assert mc.post(f"/api/v1/admin/resolutions/{r2}/close").json()["status"] == "PASSED"

    # -- R3: an exact tie, with the chair's decision recorded separately
    r3 = run("R3", [
        ("Meera Raghavan", {"FOR": 3}),
        ("Sunil Menon", {"AGAINST": 2}),
        ("Kavitha Iyer", {"AGAINST": 1}),
        ("Rajesh Nair", {"ABSTAIN": 1}),
    ], expect="TIED")
    assert mc.post(f"/api/v1/admin/resolutions/{r3}/disposition", {
        "note": "Chair declined a casting vote. Item carried forward to an EGM."
    }).status_code == 200

    # -- R4: withdrawn without being renumbered or deleted
    r4 = find_resolution("R4")
    mc.post(f"/api/v1/admin/resolutions/{r4}/finalize")
    assert mc.post(f"/api/v1/admin/resolutions/{r4}/withdraw",
                   {"note": "Auditor's consent letter not received in time."}
                   ).status_code == 200

    # -- R5: the special two-thirds rule, met exactly
    run("R5", [
        ("Meera Raghavan", {"FOR": 3}),
        ("Kavitha Iyer", {"FOR": 1}),
        ("Sunil Menon", {"AGAINST": 2}),
    ], expect="PASSED")

    # -- R6: never put to the vote, and carrying a hostile string
    created = mc.post("/api/v1/admin/resolutions", {
        "number": "R6", "title": f"Any other business {XSS_PAYLOAD}",
        "full_text": f"Item raised from the floor {XSS_PAYLOAD}"}).json()
    mc.post(f"/api/v1/admin/resolutions/{created['resolution_id']}/finalize")
    mc.post(f"/api/v1/admin/resolutions/{created['resolution_id']}/not-put-to-vote",
            {"note": "No time remained."})

    # ---------------------------------------------------------------- check --

    reconciliation = scrutineer.get("/api/v1/admin/reconciliation").json()["resolutions"]
    by_number = {r["resolution"]: r for r in reconciliation}

    for number, key in ANSWER_KEY.items():
        row = by_number[number]
        assert row["for"] == key["for"], f"{number} FOR"
        assert row["against"] == key["against"], f"{number} AGAINST"
        assert row["abstain"] == key["abstain"], f"{number} ABSTAIN"
        assert row["consumed"] == key["cast"], f"{number} cast"
        assert row["not_cast"] == key["not_cast"], f"{number} not cast"
        assert row["eligible"] == key["eligible"], f"{number} eligible"
        assert row["status"] == key["outcome"], f"{number} outcome"
        assert row["ok"] is True, f"{number} failed reconciliation"

    assert by_number["R4"]["status"] == "WITHDRAWN"
    assert by_number["R6"]["status"] == "NOT_PUT_TO_VOTE"

    # The ballot database agrees with the reported counts, independently.
    assert choice_counts(seeded, r1) == {"FOR": 7, "AGAINST": 1, "ABSTAIN": 1}
    assert choice_counts(seeded, r3) == {"FOR": 3, "AGAINST": 3, "ABSTAIN": 1}

    # The audit chain is intact end to end.
    verification = scrutineer.get("/api/v1/admin/audit/verify").json()["verification"]
    assert verification["ok"] is True

    # ------------------------------------------------------- certification --

    response = admin.post("/api/v1/admin/reports/final", {"password": "sgoa-demo"})
    assert response.status_code == 200, response.text
    bundle = Path(response.json()["path"])

    for line in (bundle / "checksums.sha256").read_text().strip().splitlines():
        digest, name = line.split("  ", 1)
        assert hashlib.sha256((bundle / name).read_bytes()).hexdigest() == digest

    manifest = json.loads((bundle / "manifest.json").read_text())
    assert manifest["audit_chain_ok"] is True
    manifest_by_number = {r["number"]: r for r in manifest["resolutions"]}
    for number, key in ANSWER_KEY.items():
        entry = manifest_by_number[number]
        assert (entry["for"], entry["against"], entry["abstain"]) == \
               (key["for"], key["against"], key["abstain"])
        assert entry["outcome"] == key["outcome"]

    html = (bundle / "final_report.html").read_text(encoding="utf-8")
    assert "Chair declined a casting vote" in html
    # A withdrawn item must not be reported as though nobody voted for it.
    assert "was not put to a vote" in html
    assert "Auditor's consent letter not received" in html or \
           "Auditor&#39;s consent letter not received" in html

    # The hostile string is rendered as text, never as markup.
    assert XSS_PAYLOAD not in html
    assert "&lt;script&gt;" in html

    assert (bundle / "final_report.pdf").stat().st_size > 2000

    # And the AGM is now finalized.
    with seeded.db.reader() as conn:
        assert conn.execute("SELECT status FROM agms").fetchone()["status"] == "FINALIZED"
