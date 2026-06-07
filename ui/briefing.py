"""Morning briefing: a one-glance snapshot of overnight swarm activity.

Assembles a static snapshot from three workspace artifacts and renders it as a
standalone HTML page served at ``GET /briefing``:

* ``workspace/state.json``    — per-cycle completion records (timestamp + the
  branches that merged / were rejected that cycle).
* ``workspace/workers.json``  — the latest worker result array, used to surface
  any worker still in a ``blocked`` state.
* ``workspace/cycles/<NNN>/retro.md`` — the most recent cycle's retrospective,
  shown verbatim so the morning review needs no log digging.

All data is read on demand inside the request handler; there is no background
process. The gathering logic takes an explicit ``now`` so "since midnight" is
deterministic and unit-testable.
"""

from __future__ import annotations

import json
from datetime import datetime
from html import escape
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

# Section headings are part of the page contract — tests assert on them and the
# briefing's whole value is that the same four answers always live in the same
# place. Treat these strings as stable.
H_CYCLES = "Cycles completed since midnight"
H_MERGED = "Branches merged"
H_REJECTED = "Branches rejected"
H_RETRO = "Latest retrospective"
H_BLOCKED = "Currently blocked workers"


def _load_json(path: Path):
    """Return parsed JSON at ``path``, or ``None`` if missing/unreadable/invalid.

    Briefing is a read-only snapshot of files written by other processes; a
    half-written or absent artifact must degrade to an empty section, never a
    500.
    """
    try:
        text = path.read_text()
    except (OSError, UnicodeDecodeError):
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _parse_dt(value) -> datetime | None:
    """Parse an ISO-8601 string into a naive/aware datetime, tolerating junk."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _latest_retro(workspace: Path) -> tuple[str | None, str | None]:
    """Return ``(cycle_label, retro_markdown)`` for the highest-numbered cycle.

    Scans ``workspace/cycles/<NNN>/retro.md`` and picks the numerically largest
    cycle directory that actually contains a ``retro.md``. Returns
    ``(None, None)`` when no retro exists yet.
    """
    cycles_dir = workspace / "cycles"
    if not cycles_dir.is_dir():
        return None, None
    best_key: int | None = None
    best_dir: Path | None = None
    for child in cycles_dir.iterdir():
        if not child.is_dir() or not (child / "retro.md").is_file():
            continue
        try:
            key = int(child.name)
        except ValueError:
            key = -1
        if best_key is None or key > best_key:
            best_key, best_dir = key, child
    if best_dir is None:
        return None, None
    try:
        return best_dir.name, (best_dir / "retro.md").read_text()
    except (OSError, UnicodeDecodeError):
        return best_dir.name, None


def gather_briefing(workspace: Path, now: datetime) -> dict:
    """Assemble the briefing payload from workspace artifacts.

    ``now`` anchors the "since midnight" window so the result is deterministic
    in tests. Cycles whose ``completed_at`` is at or after local midnight of
    ``now`` count toward the overnight tally, and their merged/rejected branch
    lists are aggregated. Malformed or missing fields are skipped, not fatal.
    """
    workspace = Path(workspace)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)

    state = _load_json(workspace / "state.json") or {}
    cycles = state.get("cycles") if isinstance(state, dict) else None
    if not isinstance(cycles, list):
        cycles = []

    cycles_since_midnight = 0
    merged: list[str] = []
    rejected: list[str] = []
    for entry in cycles:
        if not isinstance(entry, dict):
            continue
        completed = _parse_dt(entry.get("completed_at"))
        if completed is None or completed < midnight:
            continue
        cycles_since_midnight += 1
        merged.extend(b for b in entry.get("merged", []) if isinstance(b, str))
        rejected.extend(b for b in entry.get("rejected", []) if isinstance(b, str))

    workers = _load_json(workspace / "workers.json")
    blocked: list[dict] = []
    if isinstance(workers, list):
        for w in workers:
            if isinstance(w, dict) and w.get("status") == "blocked":
                blocked.append(
                    {
                        "branch": str(w.get("branch") or "(unknown)"),
                        "summary": str(w.get("summary") or "").strip(),
                    }
                )

    retro_cycle, retro_md = _latest_retro(workspace)

    return {
        "now": now.isoformat(timespec="seconds"),
        "cycles_since_midnight": cycles_since_midnight,
        "merged": merged,
        "rejected": rejected,
        "retro_cycle": retro_cycle,
        "retro_md": retro_md,
        "blocked": blocked,
    }


def _branch_list(branches: list[str]) -> str:
    if not branches:
        return "<p class='empty'>none</p>"
    items = "".join(f"<li><code>{escape(b)}</code></li>" for b in branches)
    return f"<ul>{items}</ul>"


def render_briefing_html(data: dict) -> str:
    """Render the gathered briefing payload into a standalone HTML page."""
    if data["blocked"]:
        rows = "".join(
            f"<li><code>{escape(b['branch'])}</code>"
            + (f" — {escape(b['summary'])}" if b["summary"] else "")
            + "</li>"
            for b in data["blocked"]
        )
        blocked_html = f"<ul>{rows}</ul>"
    else:
        blocked_html = "<p class='empty'>No workers are currently blocked.</p>"

    if data["retro_md"]:
        label = escape(data["retro_cycle"] or "")
        retro_html = (
            f"<p class='muted'>cycle {label}</p>"
            f"<pre>{escape(data['retro_md'])}</pre>"
        )
    else:
        retro_html = "<p class='empty'>No retrospective found for the latest cycle.</p>"

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Morning briefing</title>
<style>
  body {{ font-family: system-ui, sans-serif; max-width: 52rem; margin: 2rem auto; padding: 0 1rem; }}
  nav a {{ margin-right: 1rem; }}
  section {{ margin: 1.5rem 0; }}
  .big {{ font-size: 2.5rem; font-weight: 700; }}
  .empty, .muted {{ color: #888; }}
  pre {{ background: #f5f5f5; padding: 1rem; overflow-x: auto; white-space: pre-wrap; }}
  code {{ background: #f0f0f0; padding: 0 .25rem; }}
</style>
</head>
<body>
<nav><a href="/">&larr; Dashboard</a></nav>
<h1>Morning briefing</h1>
<p class="muted">snapshot at {escape(data['now'])}</p>

<section>
  <h2>{H_CYCLES}</h2>
  <p class="big">{data['cycles_since_midnight']}</p>
</section>

<section>
  <h2>{H_MERGED} ({len(data['merged'])})</h2>
  {_branch_list(data['merged'])}
</section>

<section>
  <h2>{H_REJECTED} ({len(data['rejected'])})</h2>
  {_branch_list(data['rejected'])}
</section>

<section>
  <h2>{H_RETRO}</h2>
  {retro_html}
</section>

<section>
  <h2>{H_BLOCKED}</h2>
  {blocked_html}
</section>
</body>
</html>"""


def build_router(workspace: Path) -> APIRouter:
    """Return an ``APIRouter`` serving ``GET /briefing`` for ``workspace``."""
    router = APIRouter()

    @router.get("/briefing", response_class=HTMLResponse)
    def briefing() -> HTMLResponse:
        data = gather_briefing(Path(workspace), datetime.now())
        return HTMLResponse(render_briefing_html(data))

    return router
