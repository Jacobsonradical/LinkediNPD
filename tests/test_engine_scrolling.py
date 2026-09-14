"""Loading retries must not exhaust the scan budget or discard late posts."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app import feed
from app.filters import Post
from tests.test_engine_waits import make_engine


@pytest.mark.asyncio
@pytest.mark.parametrize("arrival_scan, expected_checks", [(None, 4), (4, 7), (5, 8)])
async def test_stalled_feed_rescans_before_resting(monkeypatch, arrival_scan, expected_checks):
    engine = make_engine()
    # Waiting for the loader must not use up the one permitted scroll.
    engine.config.pacing.max_scrolls_per_pass = 1
    engine.store.already_liked = lambda urn: False
    page = SimpleNamespace(goto=AsyncMock(), wait_for_timeout=AsyncMock())
    monkeypatch.setattr(feed, "session_state", AsyncMock(return_value=feed.LOGGED_IN))
    scroll = AsyncMock(return_value=False)
    monkeypatch.setattr(feed, "scroll_feed", scroll)
    engine._sleep = AsyncMock()
    calls = 0

    async def scan(page, selectors):
        nonlocal calls
        calls += 1
        posts = [(None, Post("one", author="First", likeable=False))]
        if arrival_scan is not None and calls >= arrival_scan:
            posts.append((None, Post("two", author="Late arrival", likeable=False)))
        return posts

    monkeypatch.setattr(feed, "scan_posts", scan)
    await engine._one_pass(page)

    assert scroll.await_count == expected_checks
    assert calls > scroll.await_count  # includes the final rescan
    engine._sleep.assert_awaited_once()
    assert engine._sleep.await_args.args[2] == "Resting between feed passes"
    decisions = [message for level, message in engine.store.events if level == "decide"]
    assert len(decisions) == (1 if arrival_scan is None else 2)
    if arrival_scan is not None:
        assert "Late arrival" in decisions[-1]
