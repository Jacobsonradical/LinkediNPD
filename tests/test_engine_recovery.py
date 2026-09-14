"""Browser failures must replace the unusable session before another pass."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from playwright.async_api import Error, TimeoutError as PlaywrightTimeoutError

from app import engine as engine_module
from app.engine import ERROR, STOPPED
from tests.test_engine_waits import make_engine


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_at", ["startup", "pass", "new_page", "close"])
async def test_failure_relaunches_browser_with_same_profile(tmp_path, monkeypatch, failure_at):
    e = make_engine()
    monkeypatch.setattr(engine_module, "PROFILE_DIR", tmp_path)
    pages = [SimpleNamespace(goto=AsyncMock()), SimpleNamespace(goto=AsyncMock())]
    contexts = [
        SimpleNamespace(pages=[page], close=AsyncMock(), new_page=AsyncMock())
        for page in pages
    ]
    failure = Error("Page.goto: Page crashed")
    if failure_at == "startup":
        pages[0].goto.side_effect = failure
    elif failure_at == "new_page":
        contexts[0].pages = []
        contexts[0].new_page.side_effect = Error("Target page, context or browser has been closed")
    elif failure_at == "close":
        contexts[0].close.side_effect = Error("Browser disconnected")

    launch = AsyncMock(side_effect=contexts)
    manager = AsyncMock()
    manager.__aenter__.return_value = SimpleNamespace(
        chromium=SimpleNamespace(launch_persistent_context=launch)
    )
    monkeypatch.setattr(engine_module, "async_playwright", lambda: manager)
    monkeypatch.setattr(e, "_overlay", AsyncMock())
    scanned = []

    async def one_pass(page):
        scanned.append(page)
        if page is pages[0]:
            raise failure
        e.stop()

    async def recovery_wait(seconds, status, detail):
        assert seconds == 30
        assert status == ERROR
        assert "Browser session failed" in detail
        assert e._page is None
        contexts[0].close.assert_awaited_once()

    monkeypatch.setattr(e, "_one_pass", one_pass)
    monkeypatch.setattr(e, "_sleep", AsyncMock(side_effect=recovery_wait))
    await e.run()

    assert launch.await_count == 2
    assert [call.kwargs["user_data_dir"] for call in launch.await_args_list] == [str(tmp_path)] * 2
    assert scanned == (pages if failure_at in ("pass", "close") else [pages[1]])
    for context in contexts:
        context.close.assert_awaited_once()
    e._sleep.assert_awaited_once()
    assert e.status == STOPPED
    assert e._page is None
    assert e.next_action_at is None


@pytest.mark.asyncio
async def test_initial_timeout_keeps_usable_page(tmp_path, monkeypatch):
    e = make_engine()
    monkeypatch.setattr(engine_module, "PROFILE_DIR", tmp_path)
    page = SimpleNamespace(goto=AsyncMock(side_effect=PlaywrightTimeoutError("slow load")))
    context = SimpleNamespace(pages=[page], close=AsyncMock())
    launch = AsyncMock(return_value=context)
    manager = AsyncMock()
    manager.__aenter__.return_value = SimpleNamespace(
        chromium=SimpleNamespace(launch_persistent_context=launch)
    )
    monkeypatch.setattr(engine_module, "async_playwright", lambda: manager)
    monkeypatch.setattr(e, "_overlay", AsyncMock())
    one_pass = AsyncMock(side_effect=lambda page: e.stop())
    monkeypatch.setattr(e, "_one_pass", one_pass)
    await e.run()
    launch.assert_awaited_once()
    one_pass.assert_awaited_once_with(page)
    context.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_shutdown_during_failure_does_not_restart():
    e = make_engine()

    async def fail_on_shutdown():
        e.stop()
        raise Error("Browser closed")

    monkeypatch_sleep = AsyncMock()
    e._run_browser = AsyncMock(side_effect=fail_on_shutdown)
    e._sleep = monkeypatch_sleep
    await e.run()
    e._run_browser.assert_awaited_once()
    monkeypatch_sleep.assert_not_awaited()
    assert e.status == STOPPED
