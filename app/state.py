"""Persistent state: which posts we already liked, counters, and the event log.

SQLite because it survives container restarts, needs no server, and gives us a
cheap uniqueness guarantee on the post URN so a post never gets liked twice
across sessions. The event log is capped so the file cannot grow forever.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import time
from datetime import date, datetime
from pathlib import Path

STATE_DIR = Path(os.environ.get("LINKEDINPD_STATE_DIR", "/state"))

# Keep the log readable in the dashboard and the DB small on disk.
MAX_EVENTS = 2000

SCHEMA = """
CREATE TABLE IF NOT EXISTS liked_posts (
    urn        TEXT PRIMARY KEY,
    author     TEXT,
    reason     TEXT,
    liked_at   REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    level      TEXT NOT NULL,
    message    TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_liked_at ON liked_posts (liked_at);
"""


class Store:
    def __init__(self, path: Path | None = None):
        self.path = Path(path or (STATE_DIR / "linkedinpd.db"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False: the web server and the engine loop live in
        # the same process but not always on the same thread. Writes are all
        # short and serialised by the lock below.
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(SCHEMA)
        self._db.commit()
        self._lock = asyncio.Lock()

        # Anything that wants to be told about a new event registers here. The
        # WebSocket handler is the only subscriber today.
        self._subscribers: list[asyncio.Queue] = []

    # -- liked posts ---------------------------------------------------------

    def already_liked(self, urn: str) -> bool:
        row = self._db.execute(
            "SELECT 1 FROM liked_posts WHERE urn = ?", (urn,)
        ).fetchone()
        return row is not None

    def record_like(self, urn: str, author: str, reason: str) -> None:
        self._db.execute(
            "INSERT OR IGNORE INTO liked_posts (urn, author, reason, liked_at) "
            "VALUES (?, ?, ?, ?)",
            (urn, author, reason, time.time()),
        )
        self._db.commit()

    def total_likes(self) -> int:
        return self._db.execute("SELECT COUNT(*) FROM liked_posts").fetchone()[0]

    def likes_today(self) -> int:
        """Likes since local midnight - this is what the daily cap counts."""
        midnight = datetime.combine(date.today(), datetime.min.time()).timestamp()
        return self._db.execute(
            "SELECT COUNT(*) FROM liked_posts WHERE liked_at >= ?", (midnight,)
        ).fetchone()[0]

    def recent_likes(self, limit: int = 20) -> list[dict]:
        rows = self._db.execute(
            "SELECT urn, author, reason, liked_at FROM liked_posts "
            "ORDER BY liked_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    # -- event log -----------------------------------------------------------

    def log(self, message: str, level: str = "info") -> dict:
        now = time.time()
        cur = self._db.execute(
            "INSERT INTO events (level, message, created_at) VALUES (?, ?, ?)",
            (level, message, now),
        )
        # Trim on write rather than on a timer; cheap enough at this volume.
        self._db.execute(
            "DELETE FROM events WHERE id <= (SELECT MAX(id) - ? FROM events)",
            (MAX_EVENTS,),
        )
        self._db.commit()

        event = {
            "id": cur.lastrowid,
            "level": level,
            "message": message,
            "created_at": now,
        }
        self._publish(event)
        return event

    def recent_events(self, limit: int = 200) -> list[dict]:
        rows = self._db.execute(
            "SELECT id, level, message, created_at FROM events "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        # Oldest first, so the dashboard can just append as they arrive.
        return [dict(r) for r in reversed(rows)]

    # -- live push -----------------------------------------------------------

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=100)
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        if q in self._subscribers:
            self._subscribers.remove(q)

    def _publish(self, event: dict) -> None:
        """Fan an event out to live listeners.

        A slow or dead browser tab must never stall the engine, so a full queue
        just drops the event for that one subscriber.
        """
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass
