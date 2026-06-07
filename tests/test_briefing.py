"""Tests for the morning-briefing endpoint and its data gathering.

Everything is seeded under ``tmp_path`` and time is injected, so the suite is
deterministic — no real clock, network, or files outside the temp dir.
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from ui.app import create_app
from ui import briefing

NOW = datetime(2026, 6, 6, 7, 30, 0)
MIDNIGHT_TODAY = "2026-06-06T02:15:00"
YESTERDAY = "2026-06-05T23:50:00"


def _seed(workspace, *, cycles=None, workers=None, retros=None):
    workspace.mkdir(parents=True, exist_ok=True)
    if cycles is not None:
        (workspace / "state.json").write_text(json.dumps({"cycles": cycles}))
    if workers is not None:
        (workspace / "workers.json").write_text(json.dumps(workers))
    for cycle, body in (retros or {}).items():
        d = workspace / "cycles" / f"{cycle:03d}"
        d.mkdir(parents=True, exist_ok=True)
        (d / "retro.md").write_text(body)
    return workspace


def _full_fixture(workspace):
    return _seed(
        workspace,
        cycles=[
            {"cycle": 1, "completed_at": YESTERDAY, "merged": ["swarm/00-old"], "rejected": []},
            {"cycle": 2, "completed_at": MIDNIGHT_TODAY, "merged": ["swarm/02-a"], "rejected": ["swarm/02-b"]},
            {"cycle": 3, "completed_at": "2026-06-06T05:00:00", "merged": ["swarm/03-c"], "rejected": []},
        ],
        workers=[
            {"branch": "swarm/03-c", "status": "completed", "commit": "c", "summary": "shipped"},
            {"branch": "swarm/03-d", "status": "blocked", "commit": None, "summary": "stuck on config"},
        ],
        retros={3: "# Cycle 003 retrospective\n\n## Tweak to try next cycle\n- hold\n"},
    )


# --- happy path ----------------------------------------------------------------

def test_endpoint_returns_200_with_all_headings(tmp_path):
    app = create_app(_full_fixture(tmp_path / "ws"))
    resp = TestClient(app).get("/briefing")
    assert resp.status_code == 200
    body = resp.text
    for heading in (
        briefing.H_CYCLES,
        briefing.H_MERGED,
        briefing.H_REJECTED,
        briefing.H_RETRO,
        briefing.H_BLOCKED,
    ):
        assert heading in body


def test_gather_counts_only_cycles_since_midnight(tmp_path):
    data = briefing.gather_briefing(_full_fixture(tmp_path / "ws"), NOW)
    # Cycles 2 and 3 are today; cycle 1 (yesterday) excluded.
    assert data["cycles_since_midnight"] == 2
    assert data["merged"] == ["swarm/02-a", "swarm/03-c"]
    assert data["rejected"] == ["swarm/02-b"]


def test_gather_surfaces_blocked_workers_and_latest_retro(tmp_path):
    data = briefing.gather_briefing(_full_fixture(tmp_path / "ws"), NOW)
    assert [b["branch"] for b in data["blocked"]] == ["swarm/03-d"]
    assert data["blocked"][0]["summary"] == "stuck on config"
    assert data["retro_cycle"] == "003"
    assert "Cycle 003 retrospective" in data["retro_md"]


def test_dashboard_nav_links_to_briefing(tmp_path):
    app = create_app(_seed(tmp_path / "ws", cycles=[], workers=[]))
    resp = TestClient(app).get("/")
    assert resp.status_code == 200
    assert 'href="/briefing"' in resp.text


# --- edge cases ----------------------------------------------------------------

def test_empty_workspace_renders_zeros_not_error(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    resp = TestClient(create_app(ws)).get("/briefing")
    assert resp.status_code == 200
    data = briefing.gather_briefing(ws, NOW)
    assert data["cycles_since_midnight"] == 0
    assert data["merged"] == [] and data["rejected"] == []
    assert data["blocked"] == []
    assert data["retro_md"] is None
    assert "No retrospective" in resp.text
    assert "No workers are currently blocked." in resp.text


def test_latest_retro_picks_highest_numbered_cycle(tmp_path):
    ws = _seed(
        tmp_path / "ws",
        cycles=[],
        retros={1: "# Cycle 001 retrospective\n", 12: "# Cycle 012 retrospective\n"},
    )
    cycle, md = briefing._latest_retro(ws)
    assert cycle == "012"
    assert "Cycle 012" in md


def test_cycle_exactly_at_midnight_is_included(tmp_path):
    ws = _seed(
        tmp_path / "ws",
        cycles=[{"cycle": 1, "completed_at": "2026-06-06T00:00:00", "merged": ["m"], "rejected": []}],
    )
    data = briefing.gather_briefing(ws, NOW)
    assert data["cycles_since_midnight"] == 1


# --- failure modes -------------------------------------------------------------

def test_malformed_state_json_degrades_to_empty(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "state.json").write_text("{not json")
    data = briefing.gather_briefing(ws, NOW)
    assert data["cycles_since_midnight"] == 0
    assert data["merged"] == []


def test_malformed_cycle_entries_and_timestamps_are_skipped(tmp_path):
    ws = _seed(
        tmp_path / "ws",
        cycles=[
            "not-a-dict",
            {"cycle": 1, "completed_at": "garbage", "merged": ["x"]},
            {"cycle": 2, "merged": ["y"]},  # no timestamp
            {"cycle": 3, "completed_at": MIDNIGHT_TODAY, "merged": ["good"], "rejected": [42]},
        ],
    )
    data = briefing.gather_briefing(ws, NOW)
    # Only the well-formed, in-window cycle counts; the int branch is filtered.
    assert data["cycles_since_midnight"] == 1
    assert data["merged"] == ["good"]
    assert data["rejected"] == []


def test_html_escapes_untrusted_branch_and_summary(tmp_path):
    ws = _seed(
        tmp_path / "ws",
        cycles=[],
        workers=[{"branch": "<script>", "status": "blocked", "summary": "<b>x</b>"}],
    )
    html = briefing.render_briefing_html(briefing.gather_briefing(ws, NOW))
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
