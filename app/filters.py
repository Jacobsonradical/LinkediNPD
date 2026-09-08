"""Decide whether a single post deserves a like.

Deliberately pure: no Playwright, no I/O, no clock. Everything the decision
needs arrives in a Post, so the whole rule set is unit-testable without a
browser, which matters because this is the part that is easy to get subtly
wrong and expensive to debug against a live feed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class Post:
    """What we manage to scrape out of one feed item."""

    urn: str
    author: str = ""
    # Names pulled out of the grey "X likes this" line above the post.
    likers: list[str] = field(default_factory=list)
    is_suggested: bool = False
    already_liked: bool = False
    # False when the Like button is missing entirely, e.g. some job/ad cards.
    likeable: bool = True


@dataclass
class Decision:
    like: bool
    reason: str


def _normalise(name: str) -> str:
    """Lowercase and collapse whitespace so name matching is forgiving.

    LinkedIn pads names with newlines and duplicated text nodes, and people
    sprinkle emoji and credentials into them, so an exact match is hopeless.
    """
    return re.sub(r"\s+", " ", (name or "")).strip().lower()


def _matches_any(name: str, patterns: list[str]) -> str | None:
    """Return the pattern that matched this name, or None.

    Substring matching, so "Peter" catches "Peter Smith". Returning the pattern
    rather than a bool lets the event log say *which* rule fired.
    """
    target = _normalise(name)
    if not target:
        return None
    for pattern in patterns:
        needle = _normalise(pattern)
        if needle and needle in target:
            return pattern
    return None


def _first_match(names: list[str], patterns: list[str]) -> tuple[str, str] | None:
    """First (name, pattern) pair that matches across a list of names."""
    for name in names:
        hit = _matches_any(name, patterns)
        if hit:
            return name, hit
    return None


def decide(post: Post, f) -> Decision:
    """Apply the filter rules to one post. `f` is a config.Filters.

    Order matters, and it goes cheapest and most certain first:
    structural facts, then block lists, then the mode-specific rule.
    """
    # --- structural: nothing to do with the user's preferences ---------------
    if post.already_liked:
        return Decision(False, "already liked")
    if not post.likeable:
        return Decision(False, "no like button on this card")
    if f.skip_suggested and post.is_suggested:
        return Decision(False, "suggested or promoted content")

    # --- block lists win everywhere, including stalk mode --------------------
    # Someone on a block list should never be liked, whatever else matches.
    # Doing this before the mode check is what makes that promise hold.
    hit = _matches_any(post.author, f.poster_block)
    if hit:
        return Decision(False, f"poster matches block list entry {hit!r}")

    hit = _first_match(post.likers, f.liker_block)
    if hit:
        return Decision(False, f"liker {hit[0]!r} matches block list entry {hit[1]!r}")

    # --- mode-specific -------------------------------------------------------
    if f.mode == "all":
        return Decision(True, "mode 'all'")

    if f.mode == "except":
        # Blocks already handled above, so anything still standing is a yes.
        return Decision(True, "mode 'except', nothing blocked it")

    if f.mode == "stalk":
        hit = _matches_any(post.author, f.poster_allow)
        if hit:
            return Decision(True, f"poster matches allow list entry {hit!r}")

        hit = _first_match(post.likers, f.liker_allow)
        if hit:
            return Decision(
                True, f"liker {hit[0]!r} matches allow list entry {hit[1]!r}"
            )

        return Decision(False, "mode 'stalk' and nobody on the allow lists")

    # config.load() rejects unknown modes, so this is unreachable in practice.
    return Decision(False, f"unknown mode {f.mode!r}")
