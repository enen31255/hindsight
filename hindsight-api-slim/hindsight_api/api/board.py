"""Fork-only addition: lightweight built-in memory board UI (hindsight-board).

Serves static assets from ``hindsight_api/board_static`` under ``/ui``, same
origin as the HTTP API, so the board can call the API with relative URLs and
needs no separate process, no CORS handling, and no proxy. The engine and all
upstream routes are untouched: this module only mounts a static directory.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

BOARD_DIR = Path(__file__).resolve().parent.parent / "board_static"


class BoardStaticFiles(StaticFiles):
    """StaticFiles with hardened response headers (fork-only).

    The CSP keeps inline script/style (the board is a single-file app) while
    restricting sources. Deliberately NO ``frame-ancestors`` /
    ``X-Frame-Options``: the DSH shell embeds /ui in an iframe from a
    different origin (127.0.0.1:59455) and must keep working.
    ``connect-src`` allows the board's no-cors LAN dependency probes.
    """

    CSP = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "font-src 'self' data:; "
        "connect-src 'self' http://127.0.0.1:* http://192.168.1.14:*; "
        "object-src 'none'; "
        "base-uri 'self'"
    )

    async def get_response(self, path, scope):  # type: ignore[override]
        response = await super().get_response(path, scope)
        response.headers.setdefault("Content-Security-Policy", self.CSP)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        return response


def mount_board(app: FastAPI) -> None:
    """Mount the board UI at /ui when its static directory is present.

    Kept deliberately passive: a missing asset directory logs a warning and
    leaves the API exactly as upstream ships it (the board is a fork-only
    convenience, never a hard dependency).
    """
    if not (BOARD_DIR / "index.html").is_file():
        logging.warning(
            "Board UI assets missing at %s; /ui not mounted (fork-only feature)",
            BOARD_DIR,
        )
        return
    app.mount(
        "/ui",
        BoardStaticFiles(directory=str(BOARD_DIR), html=True),
        name="board",
    )
    logging.info("Board UI mounted at /ui (%s)", BOARD_DIR)
