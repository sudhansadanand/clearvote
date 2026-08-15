"""Integration tests: the whole path from a code on a card to a counted ballot."""

from __future__ import annotations

import uuid

from helpers import (ballot_count, choice_counts, credential_id_for, ledger_totals,
                     representation_id_for)


def test_join_open_preview_confirm_records_ballots_and_consumes_entitlements(
        seeded, open_resolution, voter_factory):
    ident = open_resolution("R1")
    voter = voter_factory(seeded.demo_codes["Kavitha Iyer"])
    assert voter.joined

    state = voter.state()
    assert state["screen"] == "vote"
    assert state["remaining"] == 1

    preview = voter.preview(ident, {"FOR": 1})
    assert preview.status_code == 200
    assert preview.json()["total"] == 1

    # Preview changes nothing.
    assert ballot_count(seeded, ident) == 0

    response = voter.vote(ident, {"FOR": 1})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "RECORDED"
    assert body["entitlements_recorded"] == 1
    assert body["remaining_entitlements"] == 0
    assert body["receipt_id"].startswith("VOTE-")

    assert ballot_count(seeded, ident) == 1
    assert ledger_totals(seeded, ident)["consumed"] == 1
    assert voter.state()["screen"] == "recorded"


def test_three_entitlement_holder_casts_all_for(seeded, open_resolution, voter_factory):
    ident = open_resolution("R1")
    voter = voter_factory(seeded.demo_codes["Meera Raghavan"])
    assert voter.state()["remaining"] == 3

    response = voter.vote(ident, {"FOR": 3})
    assert response.status_code == 200
    assert response.json()["entitlements_recorded"] == 3

    assert choice_counts(seeded, ident) == {"FOR": 3, "AGAINST": 0, "ABSTAIN": 0}
    assert ballot_count(seeded, ident) == 3


def test_three_entitlement_holder_splits_two_for_one_against(
        seeded, open_resolution, voter_factory):
    ident = open_resolution("R1")
    voter = voter_factory(seeded.demo_codes["Meera Raghavan"])

    response = voter.vote(ident, {"FOR": 2, "AGAINST": 1})
    assert response.status_code == 200
    assert response.json()["entitlements_recorded"] == 3
    assert choice_counts(seeded, ident) == {"FOR": 2, "AGAINST": 1, "ABSTAIN": 0}


def test_partial_allocation_leaves_the_rest_available(
        seeded, open_resolution, voter_factory):
    ident = open_resolution("R1")
    voter = voter_factory(seeded.demo_codes["Meera Raghavan"])

    assert voter.vote(ident, {"FOR": 1}).json()["remaining_entitlements"] == 2
    assert voter.state()["screen"] == "vote"
    assert voter.vote(ident, {"ABSTAIN": 2}).json()["remaining_entitlements"] == 0
    assert choice_counts(seeded, ident) == {"FOR": 1, "AGAINST": 0, "ABSTAIN": 2}


def test_retrying_the_same_submission_after_a_dropped_connection_does_not_double_vote(
        seeded, open_resolution, voter_factory):
    ident = open_resolution("R1")
    voter = voter_factory(seeded.demo_codes["Meera Raghavan"])
    submission_id = str(uuid.uuid4())

    first = voter.vote(ident, {"FOR": 3}, submission_id=submission_id)
    assert first.status_code == 200
    receipt = first.json()["receipt_id"]

    for _ in range(4):
        retry = voter.vote(ident, {"FOR": 3}, submission_id=submission_id)
        assert retry.status_code == 200
        assert retry.json()["receipt_id"] == receipt      # the original outcome, replayed
        assert retry.json()["duplicate"] is True

    assert ballot_count(seeded, ident) == 3
    assert ledger_totals(seeded, ident)["consumed"] == 3


def test_voting_again_after_full_consumption_is_refused(
        seeded, open_resolution, voter_factory):
    ident = open_resolution("R1")
    voter = voter_factory(seeded.demo_codes["Kavitha Iyer"])
    assert voter.vote(ident, {"FOR": 1}).status_code == 200

    second = voter.vote(ident, {"AGAINST": 1})
    assert second.status_code == 422
    assert "0 vote(s) left" in second.json()["message"]
    assert ballot_count(seeded, ident) == 1


def test_allocating_more_than_the_entitlement_is_refused(
        seeded, open_resolution, voter_factory):
    ident = open_resolution("R1")
    voter = voter_factory(seeded.demo_codes["Sunil Menon"])       # holds 2

    response = voter.vote(ident, {"FOR": 3})
    assert response.status_code == 422
    assert ballot_count(seeded, ident) == 0


def test_an_unconfirmed_submission_records_nothing(
        seeded, open_resolution, voter_factory):
    ident = open_resolution("R1")
    voter = voter_factory(seeded.demo_codes["Kavitha Iyer"])

    response = voter.vote(ident, {"FOR": 1}, confirmed=False)
    assert response.status_code == 422
    assert ballot_count(seeded, ident) == 0


def test_a_stale_page_cannot_vote_on_superseded_wording(
        seeded, mc, find_resolution, voter_factory):
    ident = find_resolution("R2")
    mc.post(f"/api/v1/admin/resolutions/{ident}/finalize")
    mc.post(f"/api/v1/admin/resolutions/{ident}/open")

    voter = voter_factory(seeded.demo_codes["Kavitha Iyer"])
    response = voter.vote(ident, {"FOR": 1}, version=1, text_hash="sha256:notthecurrentone")
    assert response.status_code == 409
    assert response.json()["error"] == "stale_resolution"


def test_voting_is_refused_once_the_resolution_closes(
        seeded, open_resolution, close_resolution, voter_factory):
    ident = open_resolution("R1")
    voter = voter_factory(seeded.demo_codes["Kavitha Iyer"])
    state_before = voter.state()
    assert state_before["screen"] == "vote"

    close_resolution(ident)

    response = voter.vote(ident, {"FOR": 1},
                          version=state_before["resolution"]["version"],
                          text_hash=state_before["resolution"]["text_hash"])
    assert response.status_code == 409
    assert response.json().get("error") == "not_open"
    # The client's own polling moves it off the voting screen.
    assert voter.state()["screen"] == "waiting"


def test_state_survives_a_server_restart_mid_resolution(
        seeded, open_resolution, voter_factory):
    from fastapi.testclient import TestClient

    from sgoa_vote.app import create_app
    from sgoa_vote.config import Config
    from sgoa_vote.services import Services

    ident = open_resolution("R1")
    voter = voter_factory(seeded.demo_codes["Meera Raghavan"])
    assert voter.vote(ident, {"FOR": 2}).status_code == 200

    data_dir = seeded.config.data_dir
    seeded.close()                       # the laptop loses power here

    cfg = Config()
    cfg.data_dir = data_dir
    restarted = Services(cfg)
    try:
        app = create_app(restarted)
        with restarted.db.reader() as conn:
            row = conn.execute(
                "SELECT status FROM ballot.resolutions WHERE resolution_id = ?",
                (ident,)).fetchone()
        assert row["status"] == "VOTING_OPEN"
        assert ballot_count(restarted, ident) == 2
        assert ledger_totals(restarted, ident)["consumed"] == 2

        # The voter's session and remaining entitlement are intact.
        resumed = TestClient(app)
        resumed.cookies.update(voter.client.cookies)
        state = resumed.get("/api/v1/voter/state").json()
        assert state["remaining"] == 1
    finally:
        restarted.close()


# -- registration-side behaviour --------------------------------------------

def test_credential_reset_keeps_consumed_votes_consumed(
        seeded, open_resolution, registrar, voter_factory):
    ident = open_resolution("R1")
    voter = voter_factory(seeded.demo_codes["Meera Raghavan"])
    assert voter.vote(ident, {"FOR": 2}).status_code == 200

    credential_id = credential_id_for(seeded, "Meera Raghavan")
    response = registrar.post(
        f"/api/v1/registration/credentials/{credential_id}/reset",
        {"reason": "card lost"})
    assert response.status_code == 200
    new_code = response.json()["code"]
    assert new_code != seeded.demo_codes["Meera Raghavan"]

    # The old code is dead.
    assert not voter_factory(seeded.demo_codes["Meera Raghavan"]).joined

    # The new one resumes with one vote left, not three.
    replacement = voter_factory(new_code)
    assert replacement.joined
    assert replacement.state()["remaining"] == 1
    assert ledger_totals(seeded, ident)["consumed"] == 2


def test_revoking_a_proxy_affects_later_resolutions_only(
        seeded, mc, find_resolution, registrar, voter_factory):
    first = find_resolution("R1")
    mc.post(f"/api/v1/admin/resolutions/{first}/finalize")
    mc.post(f"/api/v1/admin/resolutions/{first}/open")

    voter = voter_factory(seeded.demo_codes["Meera Raghavan"])
    assert voter.vote(first, {"FOR": 3}).status_code == 200
    mc.post(f"/api/v1/admin/resolutions/{first}/close")

    eligible_before = ledger_totals(seeded, first)["eligible"]

    # A2 was held by proxy; the owner turns up and the proxy is revoked.
    rep = representation_id_for(seeded, "A2")
    assert registrar.post(f"/api/v1/registration/representations/{rep}/revoke",
                          {"reason": "owner attended in person"}).status_code == 200

    # The closed resolution is untouched: history never changes.
    assert ledger_totals(seeded, first)["eligible"] == eligible_before
    assert ballot_count(seeded, first) == 3

    second = find_resolution("R2")
    mc.post(f"/api/v1/admin/resolutions/{second}/finalize")
    mc.post(f"/api/v1/admin/resolutions/{second}/open")
    assert voter_factory(seeded.demo_codes["Meera Raghavan"])  # code still valid

    with seeded.db.reader() as conn:
        row = conn.execute(
            """SELECT eligible_count FROM credential_resolution_ledger
                WHERE resolution_id = ? AND credential_id = ?""",
            (second, credential_id_for(seeded, "Meera Raghavan"))).fetchone()
    assert row["eligible_count"] == 2       # was 3, now 2 for the new resolution


def test_amending_a_finalized_resolution_before_voting_opens(
        seeded, mc, find_resolution):
    ident = find_resolution("R2")
    mc.post(f"/api/v1/admin/resolutions/{ident}/finalize")

    response = mc.post(f"/api/v1/admin/resolutions/{ident}/amend",
                       {"full_text": "That the Association award the contract, as amended "
                                     "on the floor of the meeting."})
    assert response.status_code == 200
    amended = response.json()
    assert amended["version"] == 2

    # The new version is what opens, and the old one is still on the record.
    assert mc.post(f"/api/v1/admin/resolutions/{amended['resolution_id']}/open"
                   ).status_code == 200
    listing = mc.get("/api/v1/admin/resolutions").json()["resolutions"]
    versions = [r for r in listing if r["number"] == "R2"]
    assert len(versions) == 2
