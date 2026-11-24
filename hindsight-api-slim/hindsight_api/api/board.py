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
        StaticFiles(directory=str(BOARD_DIR), html=True),
        name="board",
    )
    logging.info("Board UI mounted at /ui (%s)", BOARD_DIR)
