<img src="assets/icons/icon-128.png" alt="LinkediNPD" width="96" align="right">

# LinkediNPD

Automatically likes the posts in your LinkedIn feed, slowly and on your terms.

LinkedIn runs on performed enthusiasm. Being liked is how a post travels, so
everybody wants your like, and clicking that button all day is a chore nobody
should actually be doing by hand. This does it for you, at roughly one like
every five to ten minutes, with filters so you decide whose posts count.

It runs entirely inside Docker and is controlled from a small dashboard in your
browser. There is no LinkedIn API for this, so it drives a real Chromium.

---

## What it does

- Scans your feed from the top, likes what passes your filters, then scrolls on.
- **One like per interval**, randomised between 5 and 10 minutes by default,
  with a hard daily cap. This is deliberately slow. It is not a growth hack.
- Skips anything already liked, including across restarts.
- Skips **Suggested** and **Promoted** posts, which come from outside your network.
- Reloads the feed and starts again from the top when it runs out.
- Remembers your LinkedIn session, so you log in once.

### Three ways to filter

Set `mode` in `config/config.yml`:

| Mode | What it does |
|---|---|
| `all` | Like everything the feed shows. |
| `except` | Like everything **except** the people on your block lists. |
| `stalk` | Like **nothing except** the people on your allow lists. |

Each mode reads four name lists — `poster_allow`, `poster_block`,
`liker_allow`, `liker_block`. Posters are who wrote the post; likers are the
people named in the grey line above it ("Peter Smith and 3 others like this").

So `mode: stalk` with `liker_allow: ["Peter"]` means *only like what Peter has
liked*. And `mode: except` with `poster_block: ["Recruiter Bob"]` means *like
everything, but never Bob*.

**Block always beats allow**, in every mode. If someone is on a block list they
will not be liked, whatever else matches. Names match as case-insensitive
substrings, so `Peter` catches `Peter Smith` — and also `Ann Peterson`, so keep
them specific enough to mean what you want.

---

## Install

You need [Docker Desktop](https://www.docker.com/products/docker-desktop/)
(Windows 10/11, macOS) or Docker Engine + Compose (Linux). Nothing else — no
Python, no browser drivers on your machine.

```bash
git clone https://github.com/Jacobsonradical/LinkediNPD.git
cd LinkediNPD

# Your settings. Both copies are gitignored, so your lists stay private.
cp .env.example .env
cp config/config.example.yml config/config.yml

docker compose up -d --build
```

The first build pulls Chromium and takes a few minutes.

### Log in, once

```bash
docker compose logs linkedinpd
```

The log prints two links. Open the **dashboard** one
(`http://127.0.0.1:8765/#token=...`) and bookmark it — the token is part of
the link and does not change between restarts.

The dashboard will say LinkedIn is not logged in and show a **Log in to
LinkedIn** button. Click it: a new tab opens showing the container's own
browser, already connected, sitting on LinkedIn's login page. Sign in there
exactly as you normally would, 2FA included, then close that tab. The session
is kept in a Docker volume; you will not do this again unless LinkedIn signs
you out.

The engine starts **paused** so nothing is liked before you have set your
filters. Use the **Settings** card, then click **Resume**.

### The dashboard

Everything happens here:

- **Likes** — today against the daily cap, all-time, and a countdown to the
  next one.
- **Controls** — **Pause**, **Resume**, **Like now** (skip the wait). Pause is
  the everyday stop; to shut the whole thing down use `docker compose stop`,
  and `docker compose up -d` to bring it back.
- **Settings** — mode, the four name lists (one per line), skip suggested,
  start paused, minutes between likes, daily cap. **Save** validates first and
  refuses anything the engine would refuse at startup; the change applies from
  the next post, no restart.
- **Live events** — one entry per post seen, in a fixed shape:

  ```
  Poster: Jane Doe
  Likers: Peter Smith, Anna Jones
  Whether to like: yes
  Reason: liker 'Peter Smith' matches allow list entry 'Peter'
  ```

  plus a summary at the start of each pass and a green line for every like.
- **Live browser view ↗** (header) — watch the container's browser do it.
  Port 6080 is only that view; `http://127.0.0.1:6080/` is a landing page
  pointing back to the dashboard.

---

## Configuring

The Settings card on the dashboard writes `config/config.yml`. You can also edit
that file by hand — it is mounted into the container, so a restart is enough,
no rebuild:

```bash
docker compose restart
```

`config/config.example.yml` documents every field.

```yaml
filters:
  mode: stalk
  liker_allow: ["Peter", "Anna Jones"]
  poster_block: ["Recruiter Bob"]
  skip_suggested: true

pacing:
  min_seconds_between_likes: 300   # 5 min
  max_seconds_between_likes: 600   # 10 min
  max_likes_per_day: 40
```

Anything under 60 seconds is rejected at startup on purpose.

---

## Updating

```bash
cd LinkediNPD
git pull
docker compose up -d --build
```

Your login, your like history and your config all live outside the image, so an
update never costs you them.

To check what version is running:

```bash
docker compose logs --tail 20 linkedinpd
```

---

## Uninstalling

```bash
docker compose down            # stop, keep the session and history
docker compose down -v         # also delete the LinkedIn session and history
```

`-v` drops the volume, which means logging in again next time.

---

## When it stops liking things

**Status stuck on `needs login`.** LinkedIn signed the session out. Redo the
noVNC login step.

**Status is `error` saying "no posts matched any selector".** LinkedIn changed
its markup again. The event log line lists what the page actually contains.
For the full picture, fetch the live page HTML from the dashboard API:

```bash
curl -H "X-Token: <your token>" http://127.0.0.1:8765/api/debug/page > feed.html
```

Every selector is in `config/selectors.yml` with a comment on what it targets;
each entry is a list and the first match wins, so you can add a new selector
without deleting the old one. The file is mounted into the container, so a
`docker compose restart` picks the change up with no rebuild.

The feed uses build-hashed class names that change on every LinkedIn deploy,
so never anchor on a class. Use `data-view-name` and `aria-*` attributes — the
accessibility layer is the one thing a hashed build cannot scramble.

**Nothing gets liked in `stalk` mode.** That mode likes nothing until somebody
matches. Check the names in your allow lists against how they actually appear
in your feed, and remember matching is on substrings.

**Chromium keeps crashing.** Raise `shm_size` in `docker-compose.yml`.

---

## Security

The point of this section is that the container holds a live, logged-in
LinkedIn session. That is worth protecting.

- **Both ports bind to `127.0.0.1` only.** Neither the dashboard nor noVNC is
  reachable from your network. Do not "fix" this by changing it to `0.0.0.0`.
- **The dashboard needs a token** on every API call. It is generated on first
  run, stored in the volume at `0600`, and lives in the URL fragment so it never
  reaches a server log or a `Referer` header. A malicious page in your browser
  can send requests to localhost but cannot read the responses, so it cannot
  learn the token or forge a call.
- **noVNC has its own generated password**, and the VNC server itself listens
  only inside the container. The password is carried in the links the
  dashboard hands you, so there is nothing to type — it still matters, because
  a WebSocket is not covered by the same-origin policy and without it any page
  you visited could drive that browser.
- **No LinkedIn credentials are ever stored.** There is no password field
  anywhere in this project. You log in by hand and only the resulting session
  cookie lives in the Docker volume.
- `.env`, `config/config.yml` and the whole `state/` directory are gitignored.
  Never commit the volume contents.

---

## Development

```bash
pip install -r requirements-dev.txt
playwright install chromium
pytest
```

The tests run without Docker and without touching LinkedIn. `tests/fixtures/
feed.html` is a stand-in feed page built to match `config/selectors.yml`, so the
scan-and-filter path is covered end to end, and the filter rules themselves are
tested against every mode.

Layout:

```
app/
  config.py    settings loading and validation
  filters.py   the like / don't-like decision (pure, no browser)
  feed.py      everything that touches the LinkedIn DOM
  engine.py    the scan-like-wait loop
  state.py     SQLite: liked posts, counters, event log
  web/         dashboard API and page
config/
  config.yml     your settings
  selectors.yml  LinkedIn selectors, patch here when the site changes
```

---

## A word of warning

Automating LinkedIn is against their User Agreement. The realistic risk is that
the account gets restricted. The slow pacing, the daily cap and the randomised
intervals exist to keep this looking like a person who checks their feed a few
times a day, but nobody can promise you anything. Use it on an account you can
afford to lose access to, and leave the pacing alone.
