"""Filter rules. This is the logic that decides what gets liked, so it gets the
most attention: every mode, and every way a block can override an allow.
"""

import pytest

from app.config import Filters
from app.filters import Post, decide


def f(**kw):
    """Filters with the defaults the tests care about, overridden per case."""
    base = dict(
        mode="except",
        poster_allow=[],
        poster_block=[],
        liker_allow=[],
        liker_block=[],
        skip_suggested=True,
    )
    base.update(kw)
    return Filters(**base)


def post(**kw):
    base = dict(urn="urn:li:activity:1", author="Peter Smith", likers=[])
    base.update(kw)
    return Post(**base)


# --- structural rules, independent of mode ---------------------------------

@pytest.mark.parametrize("mode", ["all", "except", "stalk"])
def test_already_liked_is_never_liked_again(mode):
    d = decide(post(already_liked=True), f(mode=mode, poster_allow=["Peter"]))
    assert d.like is False
    assert "already liked" in d.reason


@pytest.mark.parametrize("mode", ["all", "except", "stalk"])
def test_card_without_like_button_is_skipped(mode):
    d = decide(post(likeable=False), f(mode=mode, poster_allow=["Peter"]))
    assert d.like is False


def test_suggested_is_skipped_when_configured():
    assert decide(post(is_suggested=True), f(mode="all")).like is False


def test_suggested_is_liked_when_the_user_allows_it():
    assert decide(post(is_suggested=True), f(mode="all", skip_suggested=False)).like


# --- mode: all --------------------------------------------------------------

def test_mode_all_likes_an_ordinary_post():
    assert decide(post(), f(mode="all")).like


def test_mode_all_still_respects_the_block_list():
    """A block list entry has to win even in the most permissive mode."""
    d = decide(post(author="Peter Smith"), f(mode="all", poster_block=["Peter"]))
    assert d.like is False
    assert "block list" in d.reason


# --- mode: except -----------------------------------------------------------

def test_mode_except_likes_anyone_not_blocked():
    assert decide(post(author="Anna Jones"), f(poster_block=["Peter"])).like


def test_mode_except_blocks_the_named_poster():
    assert decide(post(author="Peter Smith"), f(poster_block=["Peter"])).like is False


def test_mode_except_blocks_on_a_liker():
    d = decide(
        post(author="Anna Jones", likers=["Peter Smith"]), f(liker_block=["Peter"])
    )
    assert d.like is False
    assert "liker" in d.reason


# --- mode: stalk (the "only like these people" case) ------------------------

def test_mode_stalk_likes_a_post_by_someone_on_the_allow_list():
    d = decide(post(author="Peter Smith"), f(mode="stalk", poster_allow=["Peter"]))
    assert d.like
    assert "allow list" in d.reason


def test_mode_stalk_ignores_everyone_else():
    d = decide(post(author="Anna Jones"), f(mode="stalk", poster_allow=["Peter"]))
    assert d.like is False
    assert "nobody on the allow lists" in d.reason


def test_mode_stalk_likes_what_the_watched_person_liked():
    """The headline case from the brief: only like what Peter has liked."""
    d = decide(
        post(author="Someone Random", likers=["Peter Smith"]),
        f(mode="stalk", liker_allow=["Peter"]),
    )
    assert d.like
    assert "Peter Smith" in d.reason


def test_mode_stalk_block_beats_allow():
    """Blocked poster wins even though a watched liker matched."""
    d = decide(
        post(author="Blocked Guy", likers=["Peter Smith"]),
        f(mode="stalk", liker_allow=["Peter"], poster_block=["Blocked Guy"]),
    )
    assert d.like is False
    assert "block list" in d.reason


# --- name matching ----------------------------------------------------------

def test_matching_is_case_insensitive_and_ignores_extra_whitespace():
    d = decide(post(author="  peter   SMITH \n"), f(mode="stalk", poster_allow=["Peter Smith"]))
    assert d.like


def test_matching_is_substring_so_a_first_name_is_enough():
    assert decide(post(author="Peter Smith"), f(mode="stalk", poster_allow=["Peter"])).like


def test_substring_matching_can_over_match():
    """Documents a known sharp edge: 'Peter' also catches 'Peterson'.

    Kept deliberately, because requiring full names would make the feature
    useless in practice - LinkedIn names carry emoji and credentials.
    """
    assert decide(post(author="Ann Peterson"), f(mode="stalk", poster_allow=["Peter"])).like


def test_empty_author_does_not_match_an_allow_list():
    d = decide(post(author=""), f(mode="stalk", poster_allow=["Peter"]))
    assert d.like is False
