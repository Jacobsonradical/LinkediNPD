"""End-to-end over fake feed pages, old markup and new.

Runs the real Playwright scan against tests/fixtures/feed-*.html and feeds the
result through the real filter rules, so a broken selector or a wrong decision
shows up here rather than on the live site. Every test runs against both
fixtures: LinkedIn rewrote its feed in 2026 and the selectors keep the legacy
markup as a fallback, so both paths need to stay green.
"""

from pathlib import Path

import pytest
import pytest_asyncio
import yaml
from playwright.async_api import async_playwright

from app.config import Filters
from app.feed import LOGGED_IN, LOGGED_OUT, UNKNOWN, click_like, diagnose, scan_posts, scroll_feed, session_state
from app.filters import decide

FIXTURES = Path(__file__).parent / "fixtures"
SELECTORS = yaml.safe_load(
    (Path(__file__).parent.parent / "config" / "selectors.yml").read_text()
)


def f(**kw):
    base = dict(
        mode="except",
        poster_allow=[], poster_block=[], liker_allow=[], liker_block=[],
        skip_suggested=True,
    )
    base.update(kw)
    return Filters(**base)


@pytest_asyncio.fixture(params=["feed-legacy.html", "feed-new.html", "feed-aria.html"])
async def page(request):
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        pg = await browser.new_page()
        await pg.goto((FIXTURES / request.param).resolve().as_uri())
        yield pg
        await browser.close()


def liked_authors(posts, filters):
    return [p.author for _, p in posts if decide(p, filters).like]


def by_author(posts):
    return {p.author: p for _, p in posts}


@pytest.mark.asyncio
async def test_scan_reads_every_card_with_a_unique_id(page):
    posts = await scan_posts(page, SELECTORS)
    ids = [p.urn for _, p in posts]
    assert len(ids) == 6
    assert len(set(ids)) == 6
    assert not any(i.startswith("fallback:") for i in ids)


@pytest.mark.asyncio
async def test_scan_extracts_the_fields_the_filters_rely_on(page):
    posts = by_author(await scan_posts(page, SELECTORS))

    # Connection-degree tails like " • 2nd" must not leak into the name.
    assert "Peter Smith" in posts
    assert posts["Anna Jones"].likers == ["Peter Smith"]
    assert posts["Ad Corp"].is_suggested is True
    assert posts["Stranger Danger"].is_suggested is True
    assert posts["Old Friend"].already_liked is True
    assert posts["Hiring Bot"].likeable is False


@pytest.mark.asyncio
async def test_mode_all_skips_ads_already_liked_and_unlikeable(page):
    posts = await scan_posts(page, SELECTORS)
    assert liked_authors(posts, f(mode="all")) == ["Peter Smith", "Anna Jones"]


@pytest.mark.asyncio
async def test_mode_except_honours_the_block_list(page):
    posts = await scan_posts(page, SELECTORS)
    assert liked_authors(posts, f(poster_block=["Peter Smith"])) == ["Anna Jones"]


@pytest.mark.asyncio
async def test_only_like_what_peter_liked(page):
    """The brief's example: stalk mode following Peter's reactions.

    Peter's own post has nobody reacting in its header, so it is correctly
    left alone; Anna's post is the one he liked.
    """
    posts = await scan_posts(page, SELECTORS)
    assert liked_authors(posts, f(mode="stalk", liker_allow=["Peter"])) == ["Anna Jones"]


@pytest.mark.asyncio
async def test_stalk_on_a_poster_picks_only_that_poster(page):
    posts = await scan_posts(page, SELECTORS)
    assert liked_authors(posts, f(mode="stalk", poster_allow=["Peter"])) == ["Peter Smith"]


@pytest.mark.asyncio
async def test_suggested_can_be_allowed_back_in(page):
    posts = await scan_posts(page, SELECTORS)
    assert liked_authors(posts, f(mode="all", skip_suggested=False)) == [
        "Peter Smith", "Anna Jones", "Ad Corp", "Stranger Danger",
    ]


@pytest.mark.asyncio
async def test_clicking_like_flips_the_button(page):
    posts = await scan_posts(page, SELECTORS)
    container = next(c for c, p in posts if p.author == "Peter Smith")

    assert await click_like(page, container, SELECTORS) is True

    # A fresh scan must now report it as liked, which is what stops us from
    # liking the same post twice inside one pass.
    assert by_author(await scan_posts(page, SELECTORS))["Peter Smith"].already_liked is True


@pytest.mark.asyncio
async def test_session_state_reports_logged_in_on_a_parseable_feed(page):
    assert await session_state(page, SELECTORS) == LOGGED_IN


@pytest.mark.asyncio
async def test_session_state_reports_logged_out_when_a_login_form_is_present(page):
    await page.set_content('<form class="login__form"><input id="username"></form>')
    assert await session_state(page, SELECTORS) == LOGGED_OUT


@pytest.mark.asyncio
async def test_session_state_is_unknown_when_neither_is_found(page):
    """The case that matters: signed in, but the selectors no longer match.

    This must not be reported as 'logged out', or you go off re-authenticating
    when the real fix is to patch selectors.yml.
    """
    await page.set_content("<div>something else entirely</div>")
    assert await session_state(page, SELECTORS) == UNKNOWN


@pytest.mark.asyncio
async def test_diagnose_reports_selector_hit_counts(page):
    await page.set_content("<div>nothing here</div>")
    out = await diagnose(page, SELECTORS)
    assert "[data-view-name='feed-full-update']=0" in out


@pytest.mark.asyncio
async def test_a_login_redirect_counts_as_logged_out_whatever_the_markup(page):
    """LinkedIn's login page changed shape too; the URL is what we trust."""
    await page.route("https://www.linkedin.com/**", lambda r: r.fulfill(body="<div>new login page</div>"))
    await page.goto("https://www.linkedin.com/login/?session_redirect=%2Ffeed%2F")
    assert await session_state(page, SELECTORS) == LOGGED_OUT


@pytest.mark.asyncio
async def test_scroll_feed_actually_moves_the_page(page):
    """The first version wheeled at (0,0) over the nav bar and moved nothing."""
    assert await page.evaluate("() => window.scrollY") == 0
    assert await scroll_feed(page, SELECTORS, 600) is True
    assert await page.evaluate("() => window.scrollY") > 0


@pytest.mark.asyncio
async def test_scroll_feed_reports_false_only_at_a_real_end(page, monkeypatch):
    """At the true bottom it must return False - after being patient about it."""
    await page.evaluate("() => window.scrollTo(0, document.documentElement.scrollHeight)")
    from app import feed
    monkeypatch.setattr(feed, "SCROLL_WAIT_SECONDS", 0.6)
    assert await scroll_feed(page, SELECTORS, 600) is False


@pytest.mark.asyncio
async def test_scroll_feed_counts_a_growing_page_as_progress(page):
    """Lazy loading adds height without moving the scroll position."""
    await page.evaluate("() => window.scrollTo(0, document.documentElement.scrollHeight)")
    await page.evaluate("""() => setTimeout(() => {
        const d = document.createElement('div'); d.style.height = '2000px'; document.body.appendChild(d);
    }, 1500)""")
    assert await scroll_feed(page, SELECTORS, 600) is True


@pytest.mark.asyncio
async def test_overlay_shows_and_removes_without_blocking_clicks(page):
    from app.feed import overlay
    await overlay(page, "Next like in 07:12", "waiting")
    box = page.locator("#lnpd-overlay")
    assert await box.is_visible()
    assert "07:12" in await box.inner_text()
    assert await box.evaluate("el => getComputedStyle(el).pointerEvents") == "none"
    await overlay(page, None)
    assert await box.count() == 0
