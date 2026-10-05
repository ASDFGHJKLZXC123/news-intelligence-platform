"""Serve the existing personal browser and its assets from the loopback application."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
ENTRY = FRONTEND / "SIGNAL - Intelligence Platform.html"
_BROWSER_ROUTES = {"", "today", "saved", "briefs", "reading", "settings"}
_DETAIL_ROUTES = {"event", "brief", "article"}


def mount_personal_frontend(app: FastAPI) -> None:
    # Mount only actual application assets; project uploads and source data are not public.
    app.mount("/app", StaticFiles(directory=FRONTEND / "app"), name="personal-assets")

    @app.get("/{browser_path:path}", include_in_schema=False, response_class=HTMLResponse)
    def personal_browser(browser_path: str) -> HTMLResponse:
        parts = browser_path.rstrip("/").split("/")
        known = browser_path.rstrip("/") in _BROWSER_ROUTES or (
            len(parts) == 2 and parts[0] in _DETAIL_ROUTES and bool(parts[1])
        )
        if not known and browser_path != ENTRY.name:
            # In particular, an unknown API path retains the API's JSON 404 envelope.
            raise HTTPException(status_code=404, detail="Not Found")
        shell = ENTRY.read_text(encoding="utf-8")
        setup = '<base href="/" /><script>window.SIGNAL_PERSONAL_APP=true;'
        setup += "window.SIGNAL_API_BASE=window.location.origin;"
        setup += 'if(!window.location.hash && window.location.pathname!=="/") {'
        setup += r'const route=window.location.pathname.replace(/^\/+|\/+$/g, "");'
        setup += 'if(!route.endsWith(".html")) window.location.hash="#/"+route;}'
        setup += "</script>"
        return HTMLResponse(shell.replace("<head>", "<head>" + setup, 1))
