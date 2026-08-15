"""Setting up the real meeting, as opposed to the demo one."""

from __future__ import annotations

import pytest

from sgoa_vote.domain.errors import Conflict, ValidationError
from sgoa_vote.seed import (init_agm, load_apartments_csv, parse_blocks,
                            strong_password)


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
