"""Unit tests: credential generation, canonical wording, and the audit chain."""

from __future__ import annotations

from sgoa_vote.domain import audit
from sgoa_vote.domain.credentials import DIGITS, LETTERS, generate_code, hash_code
from sgoa_vote.domain.resolutions import canonical_text, compute_text_hash

AMBIGUOUS = set("IO01")


def test_generated_codes_exclude_characters_confused_in_print():
    for _ in range(10_000):
        code = generate_code()
        assert len(code) == 4
        assert not (set(code) & AMBIGUOUS), f"{code} contains an ambiguous character"
        assert all(ch in LETTERS for ch in code[:3])
        assert code[3] in DIGITS


def test_generated_codes_are_spread_across_the_space():
    # 23^3 * 8 = 97,336 possibilities, so 2,000 draws should rarely repeat much.
    codes = {generate_code() for _ in range(2_000)}
    assert len(codes) > 1_900


def test_code_hash_is_case_and_space_insensitive():
    key = b"k" * 32
    assert hash_code(key, "ktr7") == hash_code(key, " KTR7 ")


def test_code_hash_differs_under_a_different_key():
    assert hash_code(b"a" * 32, "KTR7") != hash_code(b"b" * 32, "KTR7")


# -- canonical wording ------------------------------------------------------

def test_canonical_text_ignores_line_endings_and_trailing_space():
    assert canonical_text("Approve the lift.  \r\nSecond line.\r\n\n") == \
        "Approve the lift.\nSecond line."


def test_text_hash_is_stable_across_cosmetic_differences():
    a = compute_text_hash("Approve the lift.\nSecond line.", 1)
    b = compute_text_hash("Approve the lift.   \r\nSecond line.\n\n", 1)
    assert a == b


def test_text_hash_changes_with_the_version():
    assert compute_text_hash("Same wording", 1) != compute_text_hash("Same wording", 2)


def test_text_hash_changes_with_a_real_edit():
    assert compute_text_hash("Approve the lift.", 1) != \
           compute_text_hash("Approve the lifts.", 1)


# -- audit chain ------------------------------------------------------------

def test_chain_links_each_event_to_the_previous_one(svc):
    with svc.db.writer() as conn:
        audit.append(conn, "AGM_CREATED", {"title": "test"})
        audit.append(conn, "REGISTRATION_OPENED", {})
        audit.append(conn, "VOTING_OPENED", {"resolution_number": "R1"})

    with svc.db.reader() as conn:
        rows = conn.execute(
            "SELECT * FROM audit.audit_log ORDER BY sequence").fetchall()
        assert [r["sequence"] for r in rows] == [1, 2, 3]
        assert rows[0]["prev_hash"] == audit.GENESIS_HASH
        assert rows[1]["prev_hash"] == rows[0]["event_hash"]
        assert rows[2]["prev_hash"] == rows[1]["event_hash"]
        assert audit.verify_chain(conn)["ok"] is True


def test_checkpoint_records_the_current_head(svc):
    with svc.db.writer() as conn:
        audit.append(conn, "AGM_CREATED", {"title": "test"})
        mark = audit.create_checkpoint(conn, "start")

    with svc.db.reader() as conn:
        seq, head = audit.head(conn)
        assert mark["head_sequence"] == seq
        assert mark["head_hash"] == head
