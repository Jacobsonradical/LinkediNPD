"""Scroll progress includes nested containers and virtualised post replacement."""

import time

import pytest
import pytest_asyncio
from playwright.async_api import async_playwright

from app import feed

SELECTORS = {"post_container": ["article"], "post_urn_attributes": ["data-id"]}


@pytest_asyncio.fixture
async def page(monkeypatch):
    monkeypatch.setattr(feed, "SCROLL_WAIT_SECONDS", 2)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        yield page
        await browser.close()


@pytest.mark.asyncio
async def test_nested_feed_scrolls_while_document_stays_still(page):
    await page.set_content('''
        <style>body {margin:0} main {height:300px; overflow-y:auto}
        article {height:400px}</style>
        <main><article data-id="one">One</article><article data-id="two">Two</article></main>
    ''')
    assert await feed.scroll_feed(page, SELECTORS, 250)
    assert await page.evaluate("window.scrollY") == 0
    assert await page.locator("main").evaluate("el => el.scrollTop") > 0


@pytest.mark.asyncio
async def test_virtualised_replacement_counts_without_geometry_change(page):
    await page.set_content('<article data-id="one" style="height:100px">One</article>')
    before = await feed._scroll_state(page, SELECTORS)
    await page.evaluate('''() => setTimeout(() => {
        document.querySelector('article').dataset.id = 'two';
    }, 900)''')
    assert await feed.scroll_feed(page, SELECTORS, 300)
    after = await feed._scroll_state(page, SELECTORS)
    assert (before["y"], before["height"], before["count"]) == (
        after["y"], after["height"], after["count"])


@pytest.mark.asyncio
async def test_late_nested_growth_is_detected(page):
    await page.set_content('''<main style="height:200px; overflow:auto">
        <article data-id="one" style="height:400px">One</article></main>''')
    await page.locator("main").evaluate("el => el.scrollTop = el.scrollHeight")
    height = await page.evaluate("document.documentElement.scrollHeight")
    await page.evaluate('''() => setTimeout(() => {
        document.querySelector('article').style.height = '800px';
    }, 1100)''')
    assert await feed.scroll_feed(page, SELECTORS, 200)
    assert await page.evaluate("document.documentElement.scrollHeight") == height


@pytest.mark.asyncio
async def test_true_end_waits_full_grace_period(page, monkeypatch):
    monkeypatch.setattr(feed, "SCROLL_WAIT_SECONDS", 1.2)
    await page.set_content('<article data-id="one">One</article>')
    started = time.monotonic()
    assert not await feed.scroll_feed(page, SELECTORS, 300)
    assert time.monotonic() - started >= 1.2


@pytest.mark.asyncio
async def test_pointer_stays_in_view_with_first_post_above_viewport(page):
    await page.set_content('''<style>article {height:800px}</style>
        <article data-id="one">One</article><article data-id="two">Two</article>
        <article data-id="three">Three</article>''')
    await page.evaluate("window.scrollTo(0, 1000)")
    state = await feed._scroll_state(page, SELECTORS)
    assert 0 < state["point"][1] < page.viewport_size["height"]
    assert await feed.scroll_feed(page, SELECTORS, 300)


@pytest.mark.asyncio
async def test_load_more_button_can_add_posts_without_scrolling(page):
    await page.set_content('''<article data-id="one">One</article>
        <button onclick="document.querySelector('article').dataset.id='two'">Show more</button>''')
    assert await feed.scroll_feed(page, SELECTORS, 300)
