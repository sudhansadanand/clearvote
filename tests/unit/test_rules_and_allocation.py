"""Unit tests: the result rules, and the arithmetic of splitting an entitlement."""

from __future__ import annotations

import pytest

from sgoa_vote.domain.errors import ValidationError
from sgoa_vote.domain.results import apply_rule
from sgoa_vote.domain.voting import parse_allocation


def tally(f, a, ab=0):
    return {"FOR": f, "AGAINST": a, "ABSTAIN": ab}


# -- default rule -----------------------------------------------------------

def test_for_greater_than_against_passes():
    assert apply_rule("FOR_GT_AGAINST", tally(22, 6, 1), 36) == "PASSED"


def test_for_less_than_against_fails():
    assert apply_rule("FOR_GT_AGAINST", tally(6, 22, 1), 36) == "FAILED"


def test_equal_votes_report_tied_and_never_invent_an_outcome():
    assert apply_rule("FOR_GT_AGAINST", tally(14, 14, 2), 34) == "TIED"


def test_abstentions_are_excluded_from_the_comparison():
    # 100 abstentions must not turn a 3-2 result into anything else.
    assert apply_rule("FOR_GT_AGAINST", tally(3, 2, 100), 200) == "PASSED"


# -- special rules ----------------------------------------------------------

@pytest.mark.parametrize("f,a,expected", [
    (20, 10, "PASSED"),   # exactly two-thirds
    (19, 10, "FAILED"),   # just short
    (2, 1, "PASSED"),
    (0, 0, "FAILED"),     # nothing decisive cast: never a free pass
])
def test_two_thirds_of_cast(f, a, expected):
    assert apply_rule("TWO_THIRDS_OF_CAST", tally(f, a), 40) == expected


@pytest.mark.parametrize("f,eligible,expected", [
    (19, 36, "PASSED"),
    (18, 36, "FAILED"),   # exactly half is not a majority
    (18, 35, "PASSED"),
])
def test_majority_of_all_eligible(f, eligible, expected):
    assert apply_rule("MAJORITY_OF_ALL_ELIGIBLE", tally(f, 0), eligible) == expected


# -- allocation arithmetic --------------------------------------------------

def test_allocation_accepts_a_clean_split():
    assert parse_allocation({"FOR": 2, "AGAINST": 1, "ABSTAIN": 0}) == \
        {"FOR": 2, "AGAINST": 1, "ABSTAIN": 0}


def test_allocation_defaults_missing_choices_to_zero():
    assert parse_allocation({"FOR": 3}) == {"FOR": 3, "AGAINST": 0, "ABSTAIN": 0}


def test_allocation_rejects_negative_numbers():
    with pytest.raises(ValidationError):
        parse_allocation({"FOR": -1, "AGAINST": 2})


def test_allocation_rejects_non_integers():
    with pytest.raises(ValidationError):
        parse_allocation({"FOR": 1.5})
    with pytest.raises(ValidationError):
        parse_allocation({"FOR": "two"})


def test_allocation_rejects_unknown_choices():
    with pytest.raises(ValidationError):
        parse_allocation({"FOR": 1, "MAYBE": 1})
