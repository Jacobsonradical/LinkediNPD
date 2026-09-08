"""The wait between likes is the whole point of the project, so the ways it
can be cut short are pinned down here. Resume must not be one of them."""

import asyncio
import time

import pytest

from app.config import Browser, Config, Filters, Pacing
from app.engine import Engine


class FakeStore:
    def __init__(self):
        self.events = []

    def log(self, message, level="info"):
        self.events.append((level, message))

    def total_likes(self): return 0
    def likes_today(self): return 0
    def recent_likes(self, n=20): return []


def make_engine(start_paused=False):
    cfg = Config(filters=Filters(), pacing=Pacing(), browser=Browser(start_paused=start_paused), selectors={})
    return Engine(cfg, FakeStore())


@pytest.mark.asyncio
async def test_resume_does_not_shorten_the_next_wait():
    """Start paused, resume, then sleep: the sleep must run its full length."""
    e = make_engine(start_paused=True)
    waiter = asyncio.create_task(e._wait_while_paused())
    await asyncio.sleep(0.05)
    e.resume()
    await waiter

    t0 = time.monotonic()
    await e._sleep(1.5, "waiting", "test")
    assert time.monotonic() - t0 >= 1.4


@pytest.mark.asyncio
async def test_a_skip_requested_earlier_does_not_apply_to_a_later_wait():
    e = make_engine()
    e.skip_wait()                      # nothing is waiting right now
    t0 = time.monotonic()
    await e._sleep(1.5, "waiting", "test")
    assert time.monotonic() - t0 >= 1.4


@pytest.mark.asyncio
async def test_like_now_does_cut_the_current_wait_short():
    e = make_engine()
    task = asyncio.create_task(e._sleep(30, "waiting", "test"))
    await asyncio.sleep(0.3)
    e.skip_wait()
    t0 = time.monotonic()
    await task
    assert time.monotonic() - t0 < 1.5


@pytest.mark.asyncio
async def test_pausing_holds_the_countdown():
    e = make_engine()
    task = asyncio.create_task(e._sleep(2.0, "waiting", "test"))
    await asyncio.sleep(0.3)
    e.pause()
    await asyncio.sleep(1.5)           # paused time must not count
    e.resume()
    t0 = time.monotonic()
    await task
    assert time.monotonic() - t0 >= 1.2
