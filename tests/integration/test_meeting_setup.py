"""Setting up the real meeting, as opposed to the demo one."""

from __future__ import annotations

import pytest

from sgoa_vote.domain.errors import Conflict, ValidationError
from sgoa_vote.seed import (init_agm, load_apartments_csv, load_resolutions_csv,
                            parse_blocks, strong_password)


def test_blocks_expand_to_the_full_register():
    apartments = parse_blocks("A:17,B:17,C:17")
    assert len(apartments) == 51
    assert apartments[0]["apartment_id"] == "A1"
    assert apartments[-1]["apartment_id"] == "C17"
    assert all(a["eligible"] for a in apartments)


def test_a_malformed_block_specification_is_refused():
    with pytest.raises(ValidationError):
        parse_blocks("A17")


def test_apartments_load_from_csv_with_owners_and_eligibility(tmp_path):
    path = tmp_path / "apartments.csv"
    path.write_text(
        "apartment_id,owner_name,eligible\n"
        "A1,Meera Raghavan,yes\n"
        "A2,Sunil Menon,\n"
        "B4,Disputed Ownership,no\n",
        encoding="utf-8")

    apartments = load_apartments_csv(path)
    assert [a["apartment_id"] for a in apartments] == ["A1", "A2", "B4"]
    assert apartments[0]["owner_display_name"] == "Meera Raghavan"
    assert apartments[1]["eligible"] is True          # blank defaults to eligible
    assert apartments[2]["eligible"] is False


def test_a_duplicated_apartment_in_the_csv_is_refused(tmp_path):
    path = tmp_path / "apartments.csv"
    path.write_text("apartment_id\nA1\nA1\n", encoding="utf-8")
    with pytest.raises(ValidationError) as caught:
        load_apartments_csv(path)
    assert "more than once" in str(caught.value)


def test_a_csv_without_the_required_header_is_refused(tmp_path):
    path = tmp_path / "apartments.csv"
    path.write_text("flat,owner\nA1,Someone\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        load_apartments_csv(path)


def test_init_creates_a_real_meeting_not_a_demo_one(svc):
    result = init_agm(svc, title="SGOA AGM 2026", agm_date="2026-09-20",
                      location="Community Hall", apartments=parse_blocks("A:17,B:17,C:17"))

    assert result["apartments"] == 51
    assert {a["username"] for a in result["operators"]} == {
        "admin", "registration", "mc", "scrutineer1", "scrutineer2"}

    with svc.db.reader() as conn:
        agm_row = conn.execute("SELECT * FROM agms").fetchone()
        assert agm_row["is_demo"] == 0          # no DEMO DATA banner on a real meeting
        assert agm_row["status"] == "SETUP"     # the desk opens on the day, not now
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM apartments").fetchone()["n"] == 51
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM operators").fetchone()["n"] == 5


def test_the_generated_operator_passwords_are_usable_and_distinct(svc):
    from sgoa_vote.domain import auth

    result = init_agm(svc, title="SGOA AGM 2026", agm_date="2026-09-20",
                      location="Hall", apartments=parse_blocks("A:2"))
    passwords = {a["password"] for a in result["operators"]}
    assert len(passwords) == len(result["operators"])

    with svc.db.reader() as conn:
        for account in result["operators"]:
            row = auth.verify_operator(conn, account["username"], account["password"])
            assert row["role"] == account["role"]


def test_two_meetings_cannot_share_one_data_directory(svc):
    init_agm(svc, title="First", agm_date="2026-09-20", location="Hall",
             apartments=parse_blocks("A:2"))
    with pytest.raises(Conflict) as caught:
        init_agm(svc, title="Second", agm_date="2027-09-20", location="Hall",
                 apartments=parse_blocks("A:2"))
    assert "fresh --data-dir" in str(caught.value)


def test_generated_passwords_avoid_characters_confused_in_print():
    for _ in range(200):
        assert not (set(strong_password()) & set("IO01"))


# -- the agenda ------------------------------------------------------------

def test_a_bare_single_column_of_wordings_is_enough(tmp_path):
    """What a committee typing the agenda into column A actually produces."""
    path = tmp_path / "agenda.csv"
    path.write_text(
        "That the Association approve the lift replacement as tabled.\n"
        "That the Association adopt the annual budget as circulated.\n",
        encoding="utf-8")

    agenda = load_resolutions_csv(path)
    assert len(agenda) == 2
    assert agenda[0]["full_text"].startswith("That the Association approve the lift")
    assert agenda[0]["voting_rule"] == "FOR_GT_AGAINST"
    assert agenda[0]["number"] is None          # numbered R1, R2 on creation
    # A usable handle for the console is derived from the wording.
    assert "lift replacement" in agenda[0]["title"]


def test_blank_rows_in_the_spreadsheet_are_skipped(tmp_path):
    path = tmp_path / "agenda.csv"
    path.write_text("First resolution wording.\n\n\nSecond resolution wording.\n",
                    encoding="utf-8")
    assert len(load_resolutions_csv(path)) == 2


def test_optional_columns_set_the_number_title_and_majority(tmp_path):
    path = tmp_path / "agenda.csv"
    path.write_text(
        "number,title,full_text,voting_rule\n"
        "R1,Lift replacement,That the lift be replaced.,\n"
        "R2,By-law amendment,That the by-laws be amended.,two-thirds\n"
        "R3,Auditor,That the auditor be appointed.,majority of all eligible\n",
        encoding="utf-8")

    agenda = load_resolutions_csv(path)
    assert [r["number"] for r in agenda] == ["R1", "R2", "R3"]
    assert agenda[0]["title"] == "Lift replacement"
    assert agenda[0]["voting_rule"] == "FOR_GT_AGAINST"     # blank means the default
    assert agenda[1]["voting_rule"] == "TWO_THIRDS_OF_CAST"
    assert agenda[2]["voting_rule"] == "MAJORITY_OF_ALL_ELIGIBLE"


def test_an_unrecognised_majority_is_refused_rather_than_guessed(tmp_path):
    path = tmp_path / "agenda.csv"
    path.write_text("title,full_text,voting_rule\nX,Some wording.,three quarters\n",
                    encoding="utf-8")
    with pytest.raises(ValidationError) as caught:
        load_resolutions_csv(path)
    assert "not a voting rule" in str(caught.value)


def test_a_duplicated_resolution_number_is_refused(tmp_path):
    path = tmp_path / "agenda.csv"
    path.write_text("number,full_text\nR1,First.\nR1,Second.\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        load_resolutions_csv(path)


def test_an_empty_agenda_file_is_refused(tmp_path):
    path = tmp_path / "agenda.csv"
    path.write_text("\n\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        load_resolutions_csv(path)


def test_init_loads_the_agenda_as_drafts_ready_for_the_mc(svc, tmp_path):
    path = tmp_path / "agenda.csv"
    path.write_text(
        "That the Association approve the lift replacement as tabled.\n"
        "That the by-laws be amended as set out in Annexure 2.\n",
        encoding="utf-8")

    result = init_agm(svc, title="SGOA AGM 2026", agm_date="2026-09-20",
                      location="Hall", apartments=parse_blocks("A:4"),
                      resolutions=load_resolutions_csv(path))
    assert len(result["resolutions"]) == 2

    with svc.db.reader() as conn:
        rows = conn.execute(
            "SELECT number, status, text_hash FROM ballot.resolutions ORDER BY seq"
        ).fetchall()
    assert [r["number"] for r in rows] == ["R1", "R2"]
    # Drafts, not finalized: the MC still freezes each wording on the day.
    assert all(r["status"] == "DRAFT" for r in rows)
    assert all(r["text_hash"] is None for r in rows)


def test_derived_titles_drop_the_boilerplate_opening(tmp_path):
    """"That the Association..." on every title would tell the MC nothing."""
    path = tmp_path / "agenda.csv"
    path.write_text(
        "That the Association approve the replacement of the Block A lift.\n"
        "That the by-laws be amended as set out in Annexure 2.\n",
        encoding="utf-8")

    titles = [r["title"] for r in load_resolutions_csv(path)]
    assert titles[0].startswith("Approve the replacement")
    assert not any(t.lower().startswith("that ") for t in titles)


def test_a_code_column_in_the_apartment_sheet_is_reported_as_unused(tmp_path):
    """Silently dropping it would be discovered at the desk on the day."""
    from sgoa_vote.seed import ignored_apartment_columns

    path = tmp_path / "apartments.csv"
    path.write_text("apartment_id,owner_name,eligible,code\nA1,Someone,yes,KTR7\n",
                    encoding="utf-8")

    assert ignored_apartment_columns(path) == ["code"]
    # The apartments themselves still import cleanly.
    assert len(load_apartments_csv(path)) == 1


def test_a_sheet_using_only_the_supported_columns_reports_nothing_unused(tmp_path):
    from sgoa_vote.seed import ignored_apartment_columns

    path = tmp_path / "apartments.csv"
    path.write_text("apartment_id,owner_name,eligible\nA1,Someone,yes\n", encoding="utf-8")
    assert ignored_apartment_columns(path) == []


def test_a_meeting_can_still_be_created_with_no_agenda_file(svc):
    result = init_agm(svc, title="SGOA AGM 2026", agm_date="2026-09-20",
                      location="Hall", apartments=parse_blocks("A:4"))
    assert result["resolutions"] == []
