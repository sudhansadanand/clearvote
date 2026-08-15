"""Every outcome the rules engine can produce must be presentable.

Colour alone must never carry the meaning (work order §8: "choices always
spelled out as text, never colour alone"), and adding a new outcome to the
rules engine must not silently render as an unstyled box on the hall screen.
"""

from __future__ import annotations

from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[2] / "sgoa_vote" / "web"
CSS = (WEB / "static" / "app.css").read_text(encoding="utf-8")
PROJECTOR = (WEB / "templates" / "projector.html").read_text(encoding="utf-8")
MC = (WEB / "templates" / "mc.html").read_text(encoding="utf-8")

OUTCOMES = ("PASSED", "FAILED", "TIED")


def relative_luminance(hex_colour: str) -> float:
    raw = hex_colour.lstrip("#")
    channels = [int(raw[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
              for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def contrast(a: str, b: str) -> float:
    high, low = sorted([relative_luminance(a), relative_luminance(b)], reverse=True)
    return (high + 0.05) / (low + 0.05)


def outcome_colours(name: str) -> tuple[str, str]:
    """Pull the declared background and text colour out of the stylesheet."""
    import re

    match = re.search(rf"\.outcome-{name}\s*\{{([^}}]*)\}}", CSS)
    assert match, f"no .outcome-{name} rule in app.css"
    block = match.group(1)
    background = re.search(r"background:\s*(#[0-9a-fA-F]{6})", block)
    colour = re.search(r"color:\s*(#[0-9a-fA-F]{6})", block)
    assert background and colour, f".outcome-{name} must set both background and color"
    return background.group(1), colour.group(1)


@pytest.mark.parametrize("outcome", OUTCOMES)
def test_every_outcome_has_its_own_colour_rule(outcome):
    background, text = outcome_colours(outcome)
    assert background and text


@pytest.mark.parametrize("outcome", OUTCOMES)
def test_outcome_text_meets_wcag_aa_on_its_box(outcome):
    background, text = outcome_colours(outcome)
    ratio = contrast(text, background)
    assert ratio >= 4.5, (
        f"{outcome}: text {text} on {background} is only {ratio:.2f}:1, "
        "below the 4.5:1 floor for readable text")


def test_the_outcome_fills_are_separable_without_colour_vision():
    """Red and green at the same brightness are the classic accessibility trap.

    A viewer with red-green colour blindness, or anyone looking at a
    black-and-white photo of the screen, sees only the luminance difference.
    """
    fills = {name: outcome_colours(name)[0] for name in OUTCOMES}
    pairs = [("PASSED", "FAILED"), ("PASSED", "TIED"), ("TIED", "FAILED")]
    for first, second in pairs:
        ratio = contrast(fills[first], fills[second])
        assert ratio >= 1.8, (
            f"{first} ({fills[first]}) and {second} ({fills[second]}) differ by only "
            f"{ratio:.2f}:1 in brightness and would be hard to tell apart without colour")


@pytest.mark.parametrize("outcome", OUTCOMES)
def test_each_outcome_carries_a_non_colour_cue_on_both_screens(outcome):
    """The word is always present; the glyph repeats it without using hue."""
    for template, label in ((PROJECTOR, "projector"), (MC, "MC console")):
        assert f"{outcome}:" in template, (
            f"{label} has no glyph mapped for {outcome}; the box would rely on colour alone")


def test_the_glyph_maps_cover_exactly_the_outcomes_the_rules_engine_produces():
    from sgoa_vote.domain.results import apply_rule

    produced = set()
    for rule in ("FOR_GT_AGAINST", "TWO_THIRDS_OF_CAST", "MAJORITY_OF_ALL_ELIGIBLE"):
        for tally in ({"FOR": 3, "AGAINST": 1, "ABSTAIN": 0},
                      {"FOR": 1, "AGAINST": 3, "ABSTAIN": 0},
                      {"FOR": 2, "AGAINST": 2, "ABSTAIN": 0},
                      {"FOR": 0, "AGAINST": 0, "ABSTAIN": 0}):
            produced.add(apply_rule(rule, tally, 15))

    assert produced <= set(OUTCOMES), (
        f"the rules engine can produce {produced - set(OUTCOMES)}, which has no "
        "colour rule or glyph. Add one before shipping.")
