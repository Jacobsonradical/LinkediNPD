"""Parsing of the grey context line above a post.

This is where liker names come from, so getting it wrong silently breaks the
liker_allow / liker_block filters.
"""

from pathlib import Path

import pytest
import yaml

from app.feed import looks_suggested, parse_likers

# Read the real selector file rather than restating the keyword lists here, so
# a keyword added for the live site is covered by these tests automatically.
_SELECTORS = yaml.safe_load(
    (Path(__file__).parent.parent / "config" / "selectors.yml").read_text()
)
REACTIONS = _SELECTORS["reaction_keywords"]
SUGGESTED = _SELECTORS["suggested_keywords"]


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Peter Smith likes this", ["Peter Smith"]),
        ("Peter Smith and 3 others like this", ["Peter Smith"]),
        ("Peter Smith and Anna Jones like this", ["Peter Smith", "Anna Jones"]),
        ("Peter Smith, Anna Jones and 12 others liked this", ["Peter Smith", "Anna Jones"]),
        ("Anna Jones found this insightful", ["Anna Jones"]),
        ("Peter Smith  \n  reacted to this", ["Peter Smith"]),
    ],
)
def test_liker_names_are_pulled_out_of_the_context_line(text, expected):
    assert parse_likers(text, REACTIONS) == expected


@pytest.mark.parametrize("text", ["", "Promoted", "Suggested", "Peter Smith commented"])
def test_lines_without_a_reaction_phrase_yield_no_likers(text):
    assert parse_likers(text, REACTIONS) == []


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Promoted", True),
        ("Suggested", True),
        ("Recommended for you", True),
        ("SPONSORED", True),
        ("Peter Smith likes this", False),
        ("", False),
    ],
)
def test_suggested_detection(text, expected):
    assert looks_suggested(text, SUGGESTED) is expected
