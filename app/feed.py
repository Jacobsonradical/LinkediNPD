"""Everything that touches the LinkedIn DOM.

All selectors come from config/selectors.yml rather than being hardcoded here,
because LinkedIn rewrites its markup regularly and a DOM fix should be a YAML
edit, not a code change. The text parsing below is pure so it can be tested
without a browser.
"""

from __future__ import annotations

import hashlib
import re

from .filters import Post

FEED_URL = "https://www.linkedin.com/feed/"


# ---------------------------------------------------------------------------
# Pure text helpers
# ---------------------------------------------------------------------------

def looks_suggested(context_text: str, keywords: list[str]) -> bool:
    """True when the grey context line marks this as non-organic content."""
    haystack = (context_text or "").lower()
    return any(k.lower() in haystack for k in keywords)


def parse_likers(context_text: str, reaction_keywords: list[str]) -> list[str]:
    """Pull names out of lines like "Peter Smith and 3 others like this".

    LinkedIn writes this line a dozen slightly different ways, so rather than
    trying to match them all we cut the string at the reaction phrase and treat
    whatever came before it as the names.
    """
    text = re.sub(r"\s+", " ", context_text or "").strip()
    if not text:
        return []

    lowered = text.lower()
    cut = None
    for keyword in reaction_keywords:
        idx = lowered.find(keyword.lower())
        if idx != -1 and (cut is None or idx < cut):
            cut = idx
    if cut is None:
        return []

    head = text[:cut].strip(" ,")
    if not head:
        return []

    # "Peter Smith and 3 others" / "Peter, Anna and Bob"
    parts = re.split(r",| and ", head)
    names = []
    for part in parts:
        name = part.strip()
        # Drop the "3 others" / "12 other people" tail, it is not a name.
        if not name or re.fullmatch(r"\d+\s+other(s|\s+people)?", name.lower()):
            continue
        names.append(name)
    return names


# ---------------------------------------------------------------------------
# DOM helpers
# ---------------------------------------------------------------------------

async def _first_locator(scope, selector_list: list[str]):
    """First selector in the list that actually matches something."""
    for selector in selector_list or []:
        try:
            node = scope.locator(selector).first
            if await node.count() > 0:
                return node
        except Exception:
            continue
    return None


# What session_state() can report back.
LOGGED_IN = "logged_in"
LOGGED_OUT = "logged_out"
UNKNOWN = "unknown"


async def session_state(page, selectors: dict) -> str:
    """Tell apart 'signed out' from 'signed in but the feed did not parse'.

    These two look identical if you only ask "did we find any posts?", and
    conflating them sends you off to log in again when the real problem is that
    LinkedIn moved its markup. So a login form is the only thing that counts as
    proof of being logged out; finding neither a form nor a post is UNKNOWN.
    """
    # LinkedIn bounces a dead session to /login, /authwall or /checkpoint. The
    # URL is a far steadier signal than any selector on those pages, whose
    # markup has changed underneath us once already.
    try:
        path = page.url.split("linkedin.com", 1)[-1].lower()
    except Exception:
        path = ""
    if any(marker in path for marker in selectors.get("logged_out_paths", [])):
        return LOGGED_OUT
    if await _first_locator(page, selectors.get("login_marker", [])) is not None:
        return LOGGED_OUT
    if await _first_locator(page, selectors.get("post_container", [])) is not None:
        return LOGGED_IN
    return UNKNOWN


# Walks the live page and reports what a post actually looks like now. Kept as
# a JS blob because it needs to read every attribute of every node, which would
# be hundreds of round trips through the Playwright protocol otherwise.
_PROBE_JS = """() => {
  const out = {};

  // Anything carrying an activity urn, whichever attribute holds it.
  const carriers = [];
  for (const el of document.querySelectorAll("*")) {
    for (const attr of el.attributes) {
      if (attr.value && attr.value.includes("urn:li:activity")) {
        carriers.push({
          tag: el.tagName.toLowerCase(),
          attr: attr.name,
          cls: (el.className && el.className.baseVal !== undefined
                ? el.className.baseVal : String(el.className || "")).slice(0, 120),
        });
        break;
      }
    }
    if (carriers.length >= 6) break;
  }
  out.urn_carriers = carriers;

  // Candidate Like buttons.
  const likes = [];
  for (const b of document.querySelectorAll("button")) {
    const label = (b.getAttribute("aria-label") || "") + " " + (b.innerText || "");
    if (/like|react/i.test(label)) {
      likes.push({
        cls: String(b.className || "").slice(0, 100),
        label: (b.getAttribute("aria-label") || "").slice(0, 60),
        pressed: b.getAttribute("aria-pressed"),
      });
    }
    if (likes.length >= 4) break;
  }
  out.like_buttons = likes;

  // Which class-name families are present at all.
  const families = {};
  for (const name of ["feed-shared", "update-components", "fie-impression",
                      "occludable-update", "scaffold-finite-scroll"]) {
    families[name] = document.querySelectorAll(`[class*="${name}"]`).length;
  }
  out.families = families;
  out.total_divs = document.querySelectorAll("div").length;
  return out;
}"""


async def diagnose(page, selectors: dict) -> str:
    """Describe what the page really contains, not just what failed to match.

    A bare list of zero-hit selectors says nothing useful about how to fix them,
    so this also reports which elements carry an activity urn, what the Like
    buttons look like, and which class-name families are present.
    """
    try:
        url = page.url
        title = await page.title()
    except Exception:
        url, title = "?", "?"

    probes = []
    for selector in selectors.get("post_container", []):
        try:
            probes.append(f"{selector}={await page.locator(selector).count()}")
        except Exception:
            probes.append(f"{selector}=err")

    try:
        found = await page.evaluate(_PROBE_JS)
    except Exception as exc:
        found = {"probe_error": str(exc)}

    return (
        f"url={url} title={title!r} | tried: " + " ".join(probes) + " | found: "
        + repr(found)[:1400]
    )


# Reads one post card in a single round trip. LinkedIn's new feed has no
# meaningful classes and no urn attribute, so this anchors on data-view-name
# and aria attributes; the legacy selectors are still tried as fallbacks.
_EXTRACT_JS = """(container, cfg) => {
  const first = (sels, root) => {
    for (const s of sels || []) { const el = root.querySelector(s); if (el) return el; }
    return null;
  };
  const text = (el) => (el ? (el.innerText || el.textContent || "") : "")
    .replace(/\\s+/g, " ").trim();

  // -- identifier: legacy urn attribute, else the new per-post ancestor id --
  let id = "";
  for (const attr of cfg.post_urn_attributes || []) {
    const v = container.getAttribute(attr);
    if (v && v.includes("urn:li:")) { id = v; break; }
  }
  if (!id && cfg.post_id_ancestor) {
    const anc = container.closest(cfg.post_id_ancestor);
    if (anc && anc.id) id = anc.id;
  }

  // -- author: the "..." menu button names the poster in its aria-label,
  //    which survives every markup variant seen so far. Fall back to the
  //    actor element, first line only, minus the " • 2nd" degree tail.
  let author = "";
  const menu = first(cfg.post_author_label, container);
  const prefix = cfg.post_author_label_prefix || "";
  if (menu && prefix) {
    const label = menu.getAttribute("aria-label") || "";
    if (label.startsWith(prefix)) author = label.slice(prefix.length).trim();
  }
  if (!author) {
    author = text(first(cfg.post_author, container)).split("\\n")[0];
    author = author.replace(/\\s*[•·]\\s*\\d(st|nd|rd|th)\\+?.*$/, "").trim();
  }

  // -- grey header line ("Peter Smith likes this") ------------------------
  let headerLine = "", headerNames = [];
  const anchor = first(cfg.post_header_anchor, container);
  if (anchor) {
    const line = anchor.closest("p") || anchor.parentElement;
    headerLine = text(line);
    // The linked names are cleaner than splitting the sentence ourselves,
    // which breaks on "Jane Doe, MBA and PMP likes this".
    headerNames = Array.from(line.querySelectorAll((cfg.post_header_anchor || []).join(",")))
      .map(text).filter(Boolean);
  } else {
    headerLine = text(first(cfg.post_context, container));
  }
  if (!headerLine) {
    // No anchor and no legacy element: look for the first short paragraph
    // that talks about a reaction. The post body is far longer than the cap.
    const maxChars = cfg.post_header_max_chars || 140;
    const keys = (cfg.reaction_keywords || []).map((k) => k.toLowerCase());
    for (const p of container.querySelectorAll("p")) {
      const t = text(p);
      if (t && t.length <= maxChars && keys.some((k) => t.toLowerCase().includes(k))) {
        headerLine = t;
        headerNames = Array.from(p.querySelectorAll("a")).map(text).filter(Boolean);
        break;
      }
    }
  }

  // -- paid / algorithmic markers: short standalone text nodes ------------
  let suggested = false;
  const markers = (cfg.suggested_markers || []).map((m) => m.toLowerCase());
  const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT);
  let node;
  while ((node = walker.nextNode())) {
    const t = node.textContent.trim().toLowerCase();
    if (t && t.length < 40 && markers.includes(t)) { suggested = true; break; }
  }

  // -- Like button ---------------------------------------------------------
  const btn = first(cfg.like_button, container);
  return {
    id, author, headerLine, headerNames, suggested,
    hasLike: !!btn,
    likeLabel: btn ? (btn.getAttribute("aria-label") || "") : "",
    ariaPressed: btn ? btn.getAttribute("aria-pressed") : null,
  };
}"""


def _is_liked(label: str, pressed: str | None, unliked_marker: str) -> bool:
    """Legacy markup says aria-pressed; the new one only changes the label."""
    if pressed is not None:
        return pressed.lower() == "true"
    if label and unliked_marker:
        return unliked_marker.lower() not in label.lower()
    return False


def _fallback_id(author: str, header: str, index: int) -> str:
    """Last resort when the page offers nothing stable to key on.

    Not great - it changes if the author edits the post - but far better than
    liking the same thing twice inside one session.
    """
    digest = hashlib.sha1(f"{author}|{header}|{index}".encode("utf-8")).hexdigest()
    return f"fallback:{digest[:16]}"


async def _container_selector(page, selectors: dict) -> str | None:
    """Which of the candidate container selectors this page actually uses."""
    for candidate in selectors.get("post_container", []):
        try:
            if await page.locator(candidate).count() > 0:
                return candidate
        except Exception:
            continue
    return None


async def scan_posts(page, selectors: dict, limit: int = 40) -> list[tuple]:
    """Read the currently rendered feed items.

    Returns (locator, Post) pairs so the caller can act on the same element it
    made a decision about. One page.evaluate per card keeps this fast enough
    to run on every scroll step.
    """
    selector = await _container_selector(page, selectors)
    if not selector:
        return []

    nodes = page.locator(selector)
    count = min(await nodes.count(), limit)
    reaction_keywords = selectors.get("reaction_keywords", [])
    unliked_marker = selectors.get("like_button_unliked_marker", "")

    results = []
    for i in range(count):
        node = nodes.nth(i)
        try:
            raw = await node.evaluate(_EXTRACT_JS, selectors)
        except Exception:
            # One malformed card should never abort the whole scan.
            continue

        header = raw.get("headerLine", "")
        header_l = header.lower()
        reacted = any(k.lower() in header_l for k in reaction_keywords)
        if reacted:
            likers = raw.get("headerNames") or parse_likers(header, reaction_keywords)
        else:
            likers = []

        author = raw.get("author", "")
        urn = raw.get("id") or _fallback_id(author, header, i)

        post = Post(
            urn=urn,
            author=author,
            likers=likers,
            is_suggested=bool(raw.get("suggested"))
            or looks_suggested(header, selectors.get("suggested_keywords", [])),
            already_liked=_is_liked(
                raw.get("likeLabel", ""), raw.get("ariaPressed"), unliked_marker
            ),
            likeable=bool(raw.get("hasLike")),
        )
        results.append((node, post))

    return results


async def click_like(page, container, selectors: dict) -> bool:
    """Click the Like button on one post. True if it ended up liked.

    Scrolls the post into view and pauses first: clicking a button that is
    offscreen, or the instant it appears, is exactly the pattern that looks
    automated.
    """
    button = await _first_locator(container, selectors.get("like_button", []))
    if button is None:
        return False

    await button.scroll_into_view_if_needed(timeout=5000)
    await page.wait_for_timeout(1200)
    await button.hover(timeout=5000)
    await page.wait_for_timeout(600)
    await button.click(timeout=5000)

    # LinkedIn updates the button once its own request comes back.
    await page.wait_for_timeout(1500)
    label = await button.get_attribute("aria-label") or ""
    pressed = await button.get_attribute("aria-pressed")
    return _is_liked(label, pressed, selectors.get("like_button_unliked_marker", ""))


def describe_scan(posts: list[tuple], filters) -> str:
    """One log line summarising a scan, for the event log."""
    from collections import Counter

    from .filters import decide

    # Collapse the per-name detail so the counts group sensibly.
    buckets = Counter()
    for _, post in posts:
        d = decide(post, filters)
        key = "candidate" if d.like else d.reason.split(",")[0].split(" matches")[0]
        buckets[key] += 1
    parts = ", ".join(f"{n} {k}" for k, n in buckets.most_common())
    return f"Scan saw {len(posts)} posts: {parts or 'nothing'}"


async def _scroll_metrics(page) -> tuple[int, int]:
    return await page.evaluate(
        "() => [Math.round(window.scrollY), document.documentElement.scrollHeight]"
    )


async def scroll_feed(page, selectors: dict, amount: int) -> bool:
    """Scroll the feed down by `amount` pixels. True if there was more to see.

    Two things make this fiddlier than a wheel event:

    - A wheel event lands wherever the pointer is, and Playwright's pointer
      starts at (0, 0) - the corner of the nav bar - so the first version of
      this scrolled nothing while reporting success. Park the pointer over
      the feed first, with a JS scroll as a fallback.
    - At the bottom of what is loaded, LinkedIn takes a few seconds to fetch
      the next batch, during which nothing moves. So a dead scroll is given
      time and retried before we believe it, and the page growing taller
      counts as progress even when the scroll position did not change.
    """
    y0, h0 = await _scroll_metrics(page)

    target = await _first_locator(page, selectors.get("post_container", []))
    box = None
    if target is not None:
        try:
            box = await target.bounding_box()
        except Exception:
            box = None
    if box:
        await page.mouse.move(box["x"] + box["width"] / 2, min(box["y"] + 40, 600))

    for attempt in range(3):
        await page.mouse.wheel(0, amount)
        await page.wait_for_timeout(500)
        y1, h1 = await _scroll_metrics(page)
        if y1 != y0 or h1 > h0:
            return True

        if attempt == 0:
            # The feed may live in its own scroll container; nudge that too.
            await page.evaluate(
                "(dy) => { window.scrollBy(0, dy); "
                "const el = document.querySelector('[data-testid=\"mainFeed\"]'); "
                "let n = el; while (n && n !== document.body) { "
                "  if (n.scrollHeight > n.clientHeight + 10) { n.scrollBy(0, dy); break; } "
                "  n = n.parentElement; } }",
                amount,
            )
            await page.wait_for_timeout(500)
            y1, h1 = await _scroll_metrics(page)
            if y1 != y0 or h1 > h0:
                return True

        # Some feed versions end with a button rather than loading on scroll.
        button = page.get_by_role("button", name=re.compile(selectors.get(
            "load_more_button_text", "show more|load more|more feed|new posts"), re.I))
        try:
            if await button.count() > 0 and await button.first.is_visible():
                await button.first.click(timeout=3000)
        except Exception:
            pass

        # Give the lazy loader a chance before trying again.
        await page.wait_for_timeout(4000)

    y1, h1 = await _scroll_metrics(page)
    return y1 != y0 or h1 > h0


_OVERLAY_JS = """([title, sub]) => {
  let el = document.getElementById("lnpd-overlay");
  if (!title) { if (el) el.remove(); return; }
  if (!el) {
    el = document.createElement("div");
    el.id = "lnpd-overlay";
    // pointer-events:none so it can never get in the way of a click.
    el.style.cssText = "position:fixed;left:50%;bottom:28px;transform:translateX(-50%);" +
      "z-index:2147483647;pointer-events:none;background:rgba(10,102,194,.94);color:#fff;" +
      "padding:16px 28px;border-radius:14px;box-shadow:0 8px 30px rgba(0,0,0,.35);" +
      "font:600 30px/1.2 -apple-system,Segoe UI,Roboto,Ubuntu,sans-serif;text-align:center;" +
      "font-variant-numeric:tabular-nums;white-space:nowrap";
    el.innerHTML = '<div id="lnpd-title"></div><div id="lnpd-sub" style="font-size:14px;font-weight:500;opacity:.9;margin-top:4px"></div>';
    document.body.appendChild(el);
  }
  el.querySelector("#lnpd-title").textContent = title;
  el.querySelector("#lnpd-sub").textContent = sub || "";
}"""


async def overlay(page, title: str | None, sub: str = "") -> None:
    """Show (or, with title=None, remove) the big status banner on the page."""
    await page.evaluate(_OVERLAY_JS, [title, sub])
