"""The hall screen. What it shows matters as much as what the console shows.

Regression: closing R2 used to make the projector fall back to R1's result,
because it anchored on "the last result shown" rather than "the resolution we
are on". A room glancing at the screen would have read the previous
resolution's numbers as the current outcome.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def projector(mc):
    """The projector is driven from the MC's own signed-in session."""
    def state():
        response = mc.get("/api/v1/projector/state")
        assert response.status_code == 200, response.text
        return response.json()
    return state


def run_resolution(mc, find_resolution, voter_factory, seeded, number, allocation, code):
    ident = find_resolution(number)
    mc.post(f"/api/v1/admin/resolutions/{ident}/finalize")
    mc.post(f"/api/v1/admin/resolutions/{ident}/open")
    voter_factory(seeded.demo_codes[code]).vote(ident, allocation)
    return ident


def test_idle_shows_the_upcoming_item_and_flags_unfinalized_wording(projector, mc,
                                                                    find_resolution):
    state = projector()
    assert state["mode"] == "idle"
    assert state["next"]["number"] == "R1"
    # Step 1 of the MC sequence is discussing the draft, so it may be displayed --
    # but it must be labelled as not yet final.
    assert state["next"]["finalized"] is False

    mc.post(f"/api/v1/admin/resolutions/{find_resolution('R1')}/finalize")
    assert projector()["next"]["finalized"] is True


def test_open_voting_shows_wording_and_participation_but_no_counts(
        seeded, open_resolution, voter_factory, projector):
    ident = open_resolution("R1")
    voter_factory(seeded.demo_codes["Meera Raghavan"]).vote(ident, {"FOR": 3})

    state = projector()
    assert state["mode"] == "voting_open"
    assert state["resolution"]["number"] == "R1"
    assert state["participation"]["votes_received"] == 3

    # Nothing anywhere in the payload reveals how those three were cast.
    assert "for" not in state["participation"]
    assert "FOR" not in str(state["participation"])


def test_closing_publishes_the_result_to_the_hall_immediately(
        seeded, open_resolution, close_resolution, voter_factory, projector):
    """No window in which the MC alone knows the outcome."""
    ident = open_resolution("R1")
    voter_factory(seeded.demo_codes["Meera Raghavan"]).vote(ident, {"FOR": 3})
    outcome = close_resolution(ident)
    assert outcome["published"] is True

    state = projector()
    assert state["mode"] == "result"
    assert state["result"]["resolution"] == "R1"
    assert state["result"]["outcome"] == "PASSED"
    assert state["result"]["for_count"] == 3


def test_publishing_can_be_switched_to_manual_where_the_chair_must_declare(
        seeded, open_resolution, close_resolution, voter_factory, projector, mc):
    """Some governing documents require the Chair to declare the result."""
    seeded.config.auto_publish_results = False

    ident = open_resolution("R1")
    voter_factory(seeded.demo_codes["Meera Raghavan"]).vote(ident, {"FOR": 3})
    outcome = close_resolution(ident)
    assert outcome["published"] is False

    state = projector()
    assert state["mode"] == "closed"
    assert state["resolution"]["number"] == "R1"
    assert "result" not in state          # counted, but not yet declared

    mc.post(f"/api/v1/admin/resolutions/{ident}/show-result")
    state = projector()
    assert state["mode"] == "result"
    assert state["result"]["outcome"] == "PASSED"


def test_explicitly_showing_an_already_published_result_changes_nothing(
        seeded, open_resolution, close_resolution, voter_factory, projector, mc):
    ident = open_resolution("R1")
    voter_factory(seeded.demo_codes["Meera Raghavan"]).vote(ident, {"FOR": 3})
    close_resolution(ident)

    assert mc.post(f"/api/v1/admin/resolutions/{ident}/show-result").status_code == 200
    state = projector()
    assert state["mode"] == "result"
    assert state["result"]["resolution"] == "R1"


def test_closing_the_second_resolution_never_falls_back_to_the_first(
        seeded, mc, find_resolution, voter_factory, projector):
    """The reported bug, in the order it actually happened."""
    r1 = run_resolution(mc, find_resolution, voter_factory, seeded,
                        "R1", {"FOR": 3}, "Meera Raghavan")
    mc.post(f"/api/v1/admin/resolutions/{r1}/close")
    assert projector()["result"]["resolution"] == "R1"

    r2 = run_resolution(mc, find_resolution, voter_factory, seeded,
                        "R2", {"AGAINST": 2}, "Sunil Menon")
    assert projector()["resolution"]["number"] == "R2"      # while open

    mc.post(f"/api/v1/admin/resolutions/{r2}/close")
    state = projector()
    assert state["mode"] == "result"
    assert state["result"]["resolution"] == "R2", \
        "R1's result must not reappear once R2 closes"
    assert state["result"]["outcome"] == "FAILED"


def test_manual_publishing_still_holds_on_the_current_resolution_not_the_previous(
        seeded, mc, find_resolution, voter_factory, projector):
    """The same regression, on the manual-publishing path."""
    seeded.config.auto_publish_results = False

    r1 = run_resolution(mc, find_resolution, voter_factory, seeded,
                        "R1", {"FOR": 3}, "Meera Raghavan")
    mc.post(f"/api/v1/admin/resolutions/{r1}/close")
    mc.post(f"/api/v1/admin/resolutions/{r1}/show-result")
    assert projector()["result"]["resolution"] == "R1"

    r2 = run_resolution(mc, find_resolution, voter_factory, seeded,
                        "R2", {"AGAINST": 2}, "Sunil Menon")
    mc.post(f"/api/v1/admin/resolutions/{r2}/close")

    state = projector()
    assert state["mode"] == "closed"
    assert state["resolution"]["number"] == "R2"
    assert "result" not in state, "R1's result must not reappear once R2 closes"


def test_opening_the_next_resolution_clears_the_previous_result(
        seeded, mc, find_resolution, voter_factory, projector):
    r1 = run_resolution(mc, find_resolution, voter_factory, seeded,
                        "R1", {"FOR": 3}, "Meera Raghavan")
    mc.post(f"/api/v1/admin/resolutions/{r1}/close")
    mc.post(f"/api/v1/admin/resolutions/{r1}/show-result")

    r2 = find_resolution("R2")
    mc.post(f"/api/v1/admin/resolutions/{r2}/finalize")
    mc.post(f"/api/v1/admin/resolutions/{r2}/open")

    state = projector()
    assert state["mode"] == "voting_open"
    assert state["resolution"]["number"] == "R2"


def test_a_resolution_that_failed_reconciliation_is_never_projected_as_a_result(
        seeded, open_resolution, voter_factory, mc, projector):
    import sqlite3

    ident = open_resolution("R1")
    voter_factory(seeded.demo_codes["Meera Raghavan"]).vote(ident, {"FOR": 3})

    conn = sqlite3.connect(seeded.db.paths["eligibility"])
    try:
        conn.execute(
            """UPDATE credential_resolution_ledger
                  SET eligible_count = eligible_count + 1, consumed_count = consumed_count + 1
                WHERE resolution_id = ? AND consumed_count > 0""", (ident,))
        conn.commit()
    finally:
        conn.close()

    assert mc.post(f"/api/v1/admin/resolutions/{ident}/close"
                   ).json()["status"] == "RECONCILIATION_ERROR"

    response = mc.post(f"/api/v1/admin/resolutions/{ident}/show-result")
    assert response.status_code == 409
    assert "did not reconcile" in response.json()["message"]

    state = projector()
    assert state["mode"] == "closed"
    assert state["status"] == "RECONCILIATION_ERROR"
    assert "result" not in state
