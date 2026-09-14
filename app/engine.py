"""The scheduler loop: scan the feed, pick one post, like it, wait a long time.

The whole design goal here is to look boring. One like per interval, randomised
intervals, a hard daily cap, and a full stop the moment LinkedIn stops showing
us a logged-in feed.
"""

from __future__ import annotations

import asyncio
import os
import random
import time
from pathlib import Path

from playwright.async_api import TimeoutError as PlaywrightTimeoutError, async_playwright

from . import feed
from .filters import decide

PROFILE_DIR = Path(os.environ.get("LINKEDINPD_PROFILE_DIR", "/state/chrome-profile"))

# Status values the dashboard knows how to render.
STARTING = "starting"
NEEDS_LOGIN = "needs_login"
RUNNING = "running"
WAITING = "waiting"
PAUSED = "paused"
STOPPED = "stopped"
ERROR = "error"


class Engine:
    def __init__(self, config, store):
        self.config = config
        self.store = store

        self.status = STARTING
        self.detail = ""
        # Unix timestamp the dashboard counts down to, or None when not waiting.
        self.next_action_at: float | None = None

        self._paused = config.browser.start_paused
        self._stopping = False
        # Tracks whether we have already complained about being logged out, so
        # the 60s retry does not fill the event log with the same line.
        self._login_warned = False
        # Set by the dashboard's "like now" button to cut a wait short.
        self._skip_wait = asyncio.Event()

        self._page = None

    # -- controls the web API calls -----------------------------------------

    def pause(self) -> None:
        self._paused = True
        # Clear the countdown here rather than waiting for the sleep loop's next
        # tick, otherwise the dashboard keeps counting down for a second after
        # the user has already paused.
        self.next_action_at = None
        self.store.log("Paused by user", "warn")

    def resume(self) -> None:
        # Deliberately does not touch _skip_wait: the paused loops poll every
        # second and notice on their own. Setting the skip event here is what
        # used to hand out two instant likes after every Resume.
        self._paused = False
        self.store.log("Resumed by user", "info")

    def skip_wait(self) -> None:
        self._skip_wait.set()
        self.store.log("Wait cut short by user", "info")

    def apply_config(self, config) -> None:
        """Swap in settings saved from the dashboard.

        Filters are read fresh for every decision and pacing for every wait,
        so the change takes effect at the next post rather than the next pass.
        """
        self.config = config
        self.store.log(
            f"Settings updated: mode '{config.filters.mode}', "
            f"{config.pacing.min_seconds_between_likes // 60}-"
            f"{config.pacing.max_seconds_between_likes // 60} min between likes, "
            f"cap {config.pacing.max_likes_per_day}/day",
            "info",
        )

    def stop(self) -> None:
        # main()'s shutdown path can call this more than once; log it once.
        if self._stopping:
            return
        self._stopping = True
        self._skip_wait.set()
        self.store.log("Shutdown requested by user", "warn")

    @property
    def is_paused(self) -> bool:
        return self._paused

    async def page_html(self) -> str:
        """Current HTML of the browser tab, for patching selectors.

        LinkedIn's feed now uses hashed class names that change with every
        deploy, so when the selectors break the only way to write new ones is
        to look at the real page. This is what the /api/debug/page endpoint
        hands back. Empty string when there is no page yet.
        """
        if self._page is None:
            return ""
        try:
            return await self._page.content()
        except Exception:
            # Mid-navigation; the caller can simply try again.
            return ""

    def snapshot(self) -> dict:
        """Everything the dashboard needs in one call."""
        return {
            "status": PAUSED if self._paused and self.status != STOPPED else self.status,
            "detail": self.detail,
            "paused": self._paused,
            "next_action_at": self.next_action_at,
            "total_likes": self.store.total_likes(),
            "likes_today": self.store.likes_today(),
            "max_likes_per_day": self.config.pacing.max_likes_per_day,
            "mode": self.config.filters.mode,
            "recent_likes": self.store.recent_likes(20),
        }

    # -- internal helpers ----------------------------------------------------

    async def _sleep(self, seconds: float, status: str, detail: str) -> None:
        """Wait, while staying responsive to pause / skip / shutdown.

        Time spent paused does not count towards the wait: pausing should hold
        the countdown where it is, not quietly let it run out in the background.
        """
        self.status = status
        self.detail = detail
        remaining = seconds
        self.next_action_at = time.time() + remaining
        # A skip requested before this wait began must not apply to it.
        self._skip_wait.clear()

        while remaining > 0 and not self._stopping:
            if self._paused:
                self.next_action_at = None
                await self._overlay("Paused", "Resume from the dashboard to continue")
                await asyncio.sleep(1)
                # Recompute the target so the countdown restarts from where it
                # was rather than jumping.
                self.next_action_at = time.time() + remaining
                continue

            await self._overlay(f"Next like in {int(remaining) // 60:02d}:{int(remaining) % 60:02d}", detail)

            tick = min(1.0, remaining)
            try:
                await asyncio.wait_for(self._skip_wait.wait(), timeout=tick)
                self._skip_wait.clear()
                break
            except asyncio.TimeoutError:
                remaining -= tick

        self.next_action_at = None
        await self._overlay(None)

    async def _wait_while_paused(self) -> None:
        while self._paused and not self._stopping:
            self.status = PAUSED
            self.detail = "Paused"
            self.next_action_at = None
            await self._overlay("Paused", "Resume from the dashboard to continue")
            await asyncio.sleep(1)
        await self._overlay(None)

    async def _overlay(self, title: str | None, sub: str = "") -> None:
        """Big status text drawn over the LinkedIn page in the live view.

        The noVNC window is where people actually watch this thing, and a page
        that just sits there for eight minutes looks broken. Purely cosmetic
        and purely client-side; failures are ignored.
        """
        if self._page is None:
            return
        try:
            await feed.overlay(self._page, title, sub)
        except Exception:
            pass

    def _daily_cap_reached(self) -> bool:
        return self.store.likes_today() >= self.config.pacing.max_likes_per_day

    def _seconds_until_midnight(self) -> float:
        now = time.localtime()
        return max(
            60.0,
            (23 - now.tm_hour) * 3600 + (59 - now.tm_min) * 60 + (60 - now.tm_sec),
        )

    # -- the main loop -------------------------------------------------------

    async def run(self) -> None:
        """Keep the engine alive for the life of the process.

        The browser session and the dashboard share this process, so a crash
        in here must not take the dashboard down with it - that is exactly how
        a locked profile turned into "cannot connect to server" once. Anything
        that escapes _run_browser is logged, shown as an error, and retried.
        """
        while not self._stopping:
            try:
                await self._run_browser()
            except Exception as exc:
                if self._stopping:
                    break
                self.status = ERROR
                self.detail = f"Browser session failed: {str(exc).splitlines()[0][:120]}"
                self.next_action_at = None
                self.store.log(
                    "Browser failed: " + str(exc).splitlines()[0][:300]
                    + " - retrying in 30 seconds",
                    "error",
                )
                await self._sleep(30, ERROR, self.detail)

        self.status = STOPPED
        self.detail = "Engine stopped"

    @staticmethod
    def _clear_stale_profile_lock() -> None:
        """Remove Chromium's singleton files left by an unclean shutdown.

        A killed container never lets Chromium delete these, and the next
        container has a different hostname, so Chromium concludes the profile
        is in use "on another computer" and refuses to start. Only this engine
        ever opens this profile, so at startup the lock is always stale.
        """
        for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
            path = PROFILE_DIR / name
            try:
                if path.is_symlink() or path.exists():
                    path.unlink()
            except OSError:
                pass

    async def _run_browser(self) -> None:
        cfg = self.config
        self.store.log("Engine starting", "info")

        async with async_playwright() as pw:
            PROFILE_DIR.mkdir(parents=True, exist_ok=True)
            self._clear_stale_profile_lock()

            # A persistent context is what keeps the LinkedIn session alive
            # between restarts, so the user only logs in through noVNC once.
            # Headed, because the same profile is what they log in with.
            context = await pw.chromium.launch_persistent_context(
                user_data_dir=str(PROFILE_DIR),
                headless=False,
                viewport={
                    "width": cfg.browser.viewport_width,
                    "height": cfg.browser.viewport_height,
                },
                locale=cfg.browser.locale,
                args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
            )
            try:
                page = context.pages[0] if context.pages else await context.new_page()
                self._page = page

                # Load the feed even when paused so the login window is ready.
                # A slow load can still leave a usable page; other failures
                # must reach run() so it can replace the browser session.
                try:
                    await page.goto(feed.FEED_URL, wait_until="domcontentloaded", timeout=60000)
                except PlaywrightTimeoutError as exc:
                    self.store.log(f"Initial feed load timed out: {exc}", "warn")

                while not self._stopping:
                    await self._wait_while_paused()
                    if self._stopping:
                        break

                    # Retrying a crashed tab never repairs it. Let run() back
                    # off and reopen the persistent profile after cleanup.
                    await self._one_pass(page)
            finally:
                self.next_action_at = None
                self._page = None
                self.store.log("Engine stopped", "warn")
                await context.close()

    async def _await_login(self, page) -> None:
        """Sit still while the user signs in over noVNC.

        Deliberately does not navigate: the browser on screen is the one they
        are typing into. We just re-read it every 20 seconds until the login
        form is gone.
        """
        self.status = NEEDS_LOGIN
        self.detail = "Not logged in - use the Log in button and sign in"
        self.next_action_at = None

        if not self._login_warned:
            self.store.log(
                "LinkedIn is signed out. Use the 'Log in to LinkedIn' button on "
                "this dashboard and sign in by hand; the session is then "
                "remembered. Nothing will be reloaded while you do it.",
                "error",
            )
            self._login_warned = True

        await self._sleep(20, NEEDS_LOGIN, self.detail)

    async def _one_pass(self, page) -> None:
        """One sweep of the feed from the top, then a cooldown."""
        cfg = self.config

        # Look at whatever is on screen *before* navigating anywhere. If the
        # user is part-way through logging in over noVNC, reloading the feed
        # underneath them would throw the login away - which is exactly what an
        # earlier version of this did.
        state = await feed.session_state(page, cfg.selectors)

        if state == feed.LOGGED_OUT:
            await self._await_login(page)
            return

        if state == feed.UNKNOWN:
            # Could just be a blank tab on startup, or some other LinkedIn page.
            # Go to the feed once and look again before drawing any conclusion.
            self.status = RUNNING
            self.detail = "Loading the feed"
            await page.goto(feed.FEED_URL, wait_until="domcontentloaded", timeout=60000)
            # The feed hydrates well after domcontentloaded, so give it room
            # before concluding the selectors are wrong.
            await page.wait_for_timeout(12000)
            state = await feed.session_state(page, cfg.selectors)

            if state == feed.LOGGED_OUT:
                await self._await_login(page)
                return

            if state == feed.UNKNOWN:
                # No login form and no posts. We are almost certainly signed in
                # and the selectors have gone stale.
                self.status = ERROR
                self.detail = "Feed did not parse - selectors may need updating"
                details = await feed.diagnose(page, cfg.selectors)
                self.store.log(
                    "Signed in, but no posts matched any selector. LinkedIn has "
                    "probably changed its markup - patch config/selectors.yml. "
                    + details,
                    "error",
                )
                await self._sleep(300, ERROR, self.detail)
                return
        else:
            # Already showing a parseable feed; reload for a fresh pass.
            self.status = RUNNING
            self.detail = "Loading the feed"
            await page.goto(feed.FEED_URL, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(5000)

        if self._login_warned:
            self.store.log("Signed in again, back to work", "info")
            self._login_warned = False

        self.store.log("Feed loaded, scanning from the top", "info")
        seen_this_pass: set[str] = set()
        stuck_scrolls = 0

        for scroll_index in range(cfg.pacing.max_scrolls_per_pass):
            if self._stopping:
                return
            await self._wait_while_paused()

            if self._daily_cap_reached():
                wait = self._seconds_until_midnight()
                self.store.log(
                    f"Daily cap of {cfg.pacing.max_likes_per_day} likes reached, "
                    "sleeping until tomorrow",
                    "warn",
                )
                await self._sleep(wait, WAITING, "Daily cap reached")
                return

            self.status = RUNNING
            self.detail = f"Scanning (scroll {scroll_index + 1})"
            posts = await feed.scan_posts(page, cfg.selectors)

            # Once per pass, say what the scan actually saw. Without this the
            # log just shows scroll counters ticking, which tells you nothing
            # when the selectors are half-working.
            if scroll_index == 0:
                self.store.log(feed.describe_scan(posts, self.config.filters), "info")

            liked_one = False
            for container, post in posts:
                if post.urn in seen_this_pass:
                    continue
                seen_this_pass.add(post.urn)

                # The DB is the authority across restarts; the DOM only knows
                # about this page load.
                if self.store.already_liked(post.urn):
                    continue

                verdict = decide(post, self.config.filters)

                # One line per post seen this pass, in a fixed shape so the
                # log can be scanned by eye: who wrote it, who reacted to it,
                # what we decided and why.
                who = post.author or "unknown"
                self.store.log(
                    f"Poster: {who}\n"
                    f"Likers: {', '.join(post.likers) if post.likers else '-'}\n"
                    f"Whether to like: {'yes' if verdict.like else 'no'}\n"
                    f"Reason: {verdict.reason}",
                    "decide",
                )
                if not verdict.like:
                    continue

                ok = await feed.click_like(page, container, cfg.selectors)
                if ok:
                    self.store.record_like(post.urn, post.author, verdict.reason)
                    self.store.log(
                        f"Liked post by {who} "
                        f"({self.store.likes_today()} today, "
                        f"{self.store.total_likes()} total)",
                        "like",
                    )
                else:
                    self.store.log(
                        f"Like did not register for post by {who}, moving on",
                        "warn",
                    )

                liked_one = True
                wait = random.uniform(
                    self.config.pacing.min_seconds_between_likes,
                    self.config.pacing.max_seconds_between_likes,
                )
                await self._sleep(
                    wait, WAITING, f"Waiting {int(wait / 60)} min before the next like"
                )
                break  # one like per scan, then re-read the page

            if self._stopping:
                return

            if not liked_one:
                # Nothing worth liking in view, so go further down the feed.
                moved = await feed.scroll_feed(
                    page, cfg.selectors, int(cfg.browser.viewport_height * 0.85)
                )
                if not moved:
                    # scroll_feed already waited ~10s for a lazy load; four
                    # of those in a row is about 40s of nothing, which is a
                    # real end of feed rather than a slow fetch.
                    stuck_scrolls += 1
                    if stuck_scrolls >= 4:
                        self.store.log(
                            "The feed is not scrolling any further; ending this pass",
                            "warn",
                        )
                        break
                else:
                    stuck_scrolls = 0
                await page.wait_for_timeout(random.uniform(2500, 5000))

        # Feed exhausted for this pass: rest, then start again from the top.
        wait = random.uniform(
            cfg.pacing.min_seconds_between_passes,
            cfg.pacing.max_seconds_between_passes,
        )
        self.store.log(
            f"Reached the end of this pass, resting {int(wait / 60)} min "
            "before reloading the feed",
            "info",
        )
        await self._sleep(wait, WAITING, "Resting between feed passes")
