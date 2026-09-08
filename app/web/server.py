"""Local dashboard: counters, live event log, and the run controls.

This replaces the desktop tray icon from the original plan.
It binds to localhost only and every call needs the token
that is generated at startup, because a page controlling a logged-in LinkedIn
session is not something any random local process should be able to poke.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from .. import config as config_module

STATIC_DIR = Path(__file__).parent / "static"


def create_app(engine, store, token: str, novnc_url: str = "") -> FastAPI:
    app = FastAPI(title="LinkediNPD", docs_url=None, redoc_url=None)

    def snapshot() -> dict:
        """Engine state plus the login link, which only the engine's host
        knows how to build."""
        return {**engine.snapshot(), "novnc_url": novnc_url}

    def check(supplied: str | None) -> None:
        """Constant-ish token check on every call.

        A malicious page in the user's browser can *send* requests to
        127.0.0.1, but the same-origin policy stops it reading our responses,
        so it can never learn the token and cannot forge a valid call.
        """
        if not supplied or supplied != token:
            raise HTTPException(status_code=401, detail="bad or missing token")

    @app.get("/")
    async def index():
        # The page itself is harmless without a token; the JS on it reads the
        # token out of the URL fragment and uses it for the real calls.
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/status")
    async def status(x_token: str | None = Header(default=None)):
        check(x_token)
        return JSONResponse(snapshot())

    @app.get("/api/events")
    async def events(limit: int = 200, x_token: str | None = Header(default=None)):
        check(x_token)
        return JSONResponse(store.recent_events(limit))

    @app.post("/api/control/{action}")
    async def control(action: str, x_token: str | None = Header(default=None)):
        check(x_token)
        if action == "pause":
            engine.pause()
        elif action == "resume":
            engine.resume()
        elif action == "skip":
            engine.skip_wait()
        else:
            raise HTTPException(status_code=400, detail=f"unknown action {action}")
        return JSONResponse(snapshot())

    @app.get("/api/config")
    async def get_config(x_token: str | None = Header(default=None)):
        check(x_token)
        return JSONResponse(config_module.as_dict(engine.config))

    @app.post("/api/config")
    async def set_config(request: Request, x_token: str | None = Header(default=None)):
        """Save settings typed into the dashboard.

        Validated before anything touches disk; a bad value comes back as a
        400 with the same message the startup check would have printed.
        """
        check(x_token)
        raw = await request.json()
        try:
            cfg = config_module.save(raw)
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        engine.apply_config(cfg)
        return JSONResponse(config_module.as_dict(cfg))

    @app.get("/api/debug/page")
    async def debug_page(x_token: str | None = Header(default=None)):
        """Raw HTML of whatever the engine's browser is showing right now.

        Exists purely so that the next time LinkedIn reshuffles its markup the
        fix is: fetch this, write new selectors, restart. Token-protected like
        everything else, and it contains your feed, so do not paste it anywhere
        public.
        """
        check(x_token)
        html = await engine.page_html()
        if not html:
            raise HTTPException(status_code=503, detail="page not ready, try again")
        return PlainTextResponse(html, media_type="text/html; charset=utf-8")

    @app.websocket("/ws")
    async def ws(websocket: WebSocket, token_q: str = Query(alias="token", default="")):
        if token_q != token:
            await websocket.close(code=1008)
            return
        await websocket.accept()

        queue = store.subscribe()
        try:
            # Replay recent history so a freshly opened tab is not blank.
            for event in store.recent_events(200):
                await websocket.send_json({"type": "event", "data": event})
            await websocket.send_json({"type": "status", "data": snapshot()})

            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=2.0)
                    await websocket.send_json({"type": "event", "data": event})
                except asyncio.TimeoutError:
                    # No new events; push a status tick so the countdown and
                    # the counters stay live.
                    await websocket.send_json(
                        {"type": "status", "data": snapshot()}
                    )
        except WebSocketDisconnect:
            pass
        finally:
            store.unsubscribe(queue)

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app
