"""Entrypoint: bring up the dashboard and the engine in one event loop."""

from __future__ import annotations

import asyncio
import os
import secrets
import signal
from pathlib import Path
from urllib.parse import quote

import uvicorn

from . import config as config_module
from . import engine as engine_module
from .state import STATE_DIR, Store
from .web.server import create_app

HOST = os.environ.get("LINKEDINPD_HOST", "0.0.0.0")
PORT = int(os.environ.get("LINKEDINPD_PORT", "8765"))
NOVNC_PORT = int(os.environ.get("LINKEDINPD_NOVNC_PORT", "6080"))


def _novnc_url() -> str:
    """One-click noVNC link, password included so there is nothing to type.

    Only ever handed out over the token-protected API, so it is no more exposed
    than the dashboard itself. Empty when there is no VNC running, e.g. during
    a local non-Docker run.
    """
    path = Path(os.environ.get("LINKEDINPD_VNCPASS_FILE", "/state/vncpass.txt"))
    try:
        password = path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    if not password:
        return ""
    return (
        f"http://127.0.0.1:{NOVNC_PORT}/vnc.html"
        f"?autoconnect=true&resize=scale&password={quote(password)}"
    )


def _load_token() -> str:
    """Stable per-install token, kept in the state volume.

    Regenerating it on every boot would invalidate the bookmarked dashboard URL
    each restart, which gets old fast. 0600 so it is not readable by other
    users if the volume is ever mounted somewhere shared.
    """
    path = Path(os.environ.get("LINKEDINPD_TOKEN_FILE", STATE_DIR / "dashboard-token"))
    path.parent.mkdir(parents=True, exist_ok=True)

    if path.exists():
        token = path.read_text(encoding="utf-8").strip()
        if token:
            return token

    token = secrets.token_urlsafe(32)
    path.write_text(token, encoding="utf-8")
    os.chmod(path, 0o600)
    return token


async def main() -> None:
    cfg = config_module.load()
    store = Store()
    token = _load_token()

    engine = engine_module.Engine(cfg, store)
    app = create_app(engine, store, token, _novnc_url())

    server = uvicorn.Server(
        uvicorn.Config(app, host=HOST, port=PORT, log_level="warning", access_log=False)
    )

    # The container publishes only on 127.0.0.1, so print the loopback URL the
    # user actually needs rather than the bind address.
    print("\n" + "=" * 68, flush=True)
    print("  LinkediNPD dashboard:", flush=True)
    print(f"  http://127.0.0.1:{PORT}/#token={token}", flush=True)
    print("=" * 68 + "\n", flush=True)

    loop = asyncio.get_running_loop()
    stopping = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stopping.set)

    engine_task = asyncio.create_task(engine.run())
    server_task = asyncio.create_task(server.serve())
    stop_task = asyncio.create_task(stopping.wait())

    # Whichever finishes first ends the process: if the engine dies there is
    # nothing left to supervise, and on SIGTERM we want a clean exit.
    await asyncio.wait(
        [engine_task, server_task, stop_task], return_when=asyncio.FIRST_COMPLETED
    )

    engine.stop()
    server.should_exit = True
    for task in (engine_task, server_task):
        if not task.done():
            try:
                await asyncio.wait_for(task, timeout=20)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                task.cancel()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
