"""Unit tests: every legal transition works, every illegal one is refused."""

from __future__ import annotations

import pytest

from sgoa_vote.domain import agm, resolutions, results
from sgoa_vote.domain.errors import Conflict


@pytest.fixture
def store(svc):
    with svc.db.writer() as conn:
        agm.create(conn, "Test AGM", "2026-09-20", "Hall", svc.config)
    return svc


def draft(conn, number="R1"):
    return resolutions.create_draft(conn, "Title", "Wording of the resolution.",
                                    number=number)


# -- legal path -------------------------------------------------------------

def test_draft_finalize_open_close(store):
    with store.db.writer() as conn:
        row = draft(conn)
        assert row["status"] == "DRAFT"
        assert row["text_hash"] is None

        finalized = resolutions.finalize(conn, row["resolution_id"])
        assert finalized["status"] == "FINALIZED"
        assert finalized["text_hash"].startswith("sha256:")

        opened = resolutions.open_voting(conn, row["resolution_id"])
        assert opened["resolution"]["status"] == "VOTING_OPEN"

        closed = results.close_voting(conn, row["resolution_id"])
        # Nobody was eligible, so nothing was cast; the books still balance.
        assert closed["reconciliation"]["ok"] is True


# -- illegal transitions ----------------------------------------------------

def test_cannot_open_a_draft_that_was_never_finalized(store):
    with store.db.writer() as conn:
        row = draft(conn)
        with pytest.raises(Conflict):
            resolutions.open_voting(conn, row["resolution_id"])


def test_cannot_edit_after_finalizing(store):
    with store.db.writer() as conn:
        row = draft(conn)
        resolutions.finalize(conn, row["resolution_id"])
        with pytest.raises(Conflict):
            resolutions.edit_draft(conn, row["resolution_id"], full_text="Sneaky change")


def test_cannot_finalize_twice(store):
    with store.db.writer() as conn:
        row = draft(conn)
        resolutions.finalize(conn, row["resolution_id"])
        with pytest.raises(Conflict):
            resolutions.finalize(conn, row["resolution_id"])


def test_cannot_close_a_resolution_that_is_not_open(store):
    with store.db.writer() as conn:
        row = draft(conn)
        with pytest.raises(Conflict):
            results.close_voting(conn, row["resolution_id"])


def test_only_one_resolution_may_be_open_at_a_time(store):
    with store.db.writer() as conn:
        first = draft(conn, "R1")
        second = draft(conn, "R2")
        resolutions.finalize(conn, first["resolution_id"])
        resolutions.finalize(conn, second["resolution_id"])
        resolutions.open_voting(conn, first["resolution_id"])
        with pytest.raises(Conflict):
            resolutions.open_voting(conn, second["resolution_id"])


def test_resolution_numbers_are_never_reused(store):
    with store.db.writer() as conn:
        draft(conn, "R1")
        with pytest.raises(Conflict):
            draft(conn, "R1")


def test_withdrawn_resolution_keeps_its_number_and_history(store):
    with store.db.writer() as conn:
        row = draft(conn, "R3")
        resolutions.finalize(conn, row["resolution_id"])
        withdrawn = resolutions.withdraw(conn, row["resolution_id"], "Deferred to an EGM")
        assert withdrawn["status"] == "WITHDRAWN"
        assert withdrawn["number"] == "R3"
        # The number cannot be recycled for a different motion.
        with pytest.raises(Conflict):
            draft(conn, "R3")


# -- amendments -------------------------------------------------------------

def test_amendment_creates_a_new_version_and_keeps_the_old_one_visible(store):
    with store.db.writer() as conn:
        row = draft(conn, "R2")
        resolutions.finalize(conn, row["resolution_id"])
        amended = resolutions.amend(conn, row["resolution_id"],
                                    full_text="Wording of the resolution, as amended.")
        assert amended["version"] == 2
        assert amended["status"] == "FINALIZED"

        history = resolutions.versions_of(conn, "R2")
        assert [h["version"] for h in history] == [1, 2]
        assert history[0]["superseded_by"] == amended["resolution_id"]
        assert history[0]["text_hash"] != amended["text_hash"]


def test_cannot_open_a_superseded_version(store):
    with store.db.writer() as conn:
        row = draft(conn, "R2")
        resolutions.finalize(conn, row["resolution_id"])
        resolutions.amend(conn, row["resolution_id"], full_text="Amended wording.")
        with pytest.raises(Conflict):
            resolutions.open_voting(conn, row["resolution_id"])


def test_cannot_amend_a_draft(store):
    with store.db.writer() as conn:
        row = draft(conn)
        with pytest.raises(Conflict):
            resolutions.amend(conn, row["resolution_id"], full_text="x")
