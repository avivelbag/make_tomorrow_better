"""Dashboard FastAPI app.

Hosts the workspace dashboard at ``/`` and mounts the morning-briefing router
(``/briefing``). The dashboard nav links to the briefing so the overnight
summary is one click from the home page.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from ui.briefing import build_router

_DASHBOARD_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Swarm dashboard</title>
<style>
  body { font-family: system-ui, sans-serif; max-width: 52rem; margin: 2rem auto; padding: 0 1rem; }
  nav a { margin-right: 1rem; }
</style>
</head>
<body>
<h1>Swarm dashboard</h1>
<nav>
  <a href="/">Home</a>
  <a href="/briefing">Morning briefing</a>
</nav>
<p>Welcome. Start your day with the <a href="/briefing">morning briefing</a>.</p>
</body>
</html>"""


def create_app(workspace: Path) -> FastAPI:
    """Build the dashboard app rooted at ``workspace``."""
    app = FastAPI(title="Swarm dashboard")

    @app.get("/", response_class=HTMLResponse)
    def dashboard() -> HTMLResponse:
        return HTMLResponse(_DASHBOARD_HTML)

    app.include_router(build_router(Path(workspace)))
    return app
