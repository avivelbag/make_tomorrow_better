"""Tests for the morning-briefing endpoint and its data gathering.

Fixtures seed the *real* production artifacts — ``logs/cycles.jsonl`` (tz-aware
UTC records) and a ``workers.json`` whose summary lives inside ``final_line`` —
so the suite exercises the contract the orchestrator actually writes, not an
invented schema. Everything is under ``tmp_path`` and time is injected, so the
suite is deterministic across timezones: in-window cycles are stamped at the
exact ``now`` instant (always >= local midnight) and out-of-window cycles 25h
earlier (always < local midnight), independent of the host's local zone.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from ui.app import create_app
from ui import briefing

NOW = datetime(2026, 6, 6, 7, 30, 0, tzinfo=timezone.utc)
YESTERDAY = NOW - timedelta(hours=25)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _final_line(status: str, summary: str) -> str:
    return json.dumps({"status": status, "summary": summary, "commit": None})


def _seed(workspace, *, cycles=None, workers=None, retros=None):
    workspace.mkdir(parents=True, exist_ok=True)
    if cycles is not None:
        log_dir = workspace / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / "cycles.jsonl").write_text(
            "".join(json.dumps(c) + "\n" for c in cycles)
        )
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
            {"iteration": 1, "ended_at": _iso(YESTERDAY), "merged": ["swarm/00-old"],
             "not_approved": [], "test_failures": []},
            {"iteration": 2, "ended_at": _iso(NOW), "merged": ["swarm/02-a"],
             "not_approved": [{"branch": "swarm/02-b", "verdict": "reject"}],
             "test_failures": []},
            {"iteration": 3, "ended_at": _iso(NOW), "merged": ["swarm/03-c"],
             "not_approved": [],
             "test_failures": [{"branch": "swarm/03-e", "verdict": "approve", "exit_code": 1}]},
        ],
        workers=[
            {"branch": "swarm/03-c", "status": "completed", "commit": "c",
             "final_line": _final_line("completed", "shipped")},
            {"branch": "swarm/03-d", "status": "blocked", "commit": None,
             "final_line": _final_line("blocked", "stuck on config")},
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
    # Cycles 2 and 3 are stamped at `now`; cycle 1 (25h earlier) is excluded.
    assert data["cycles_since_midnight"] == 2
    assert data["merged"] == ["swarm/02-a", "swarm/03-c"]
    # Rejected aggregates not_approved + test_failures across in-window cycles.
    assert data["rejected"] == ["swarm/02-b", "swarm/03-e"]


def test_gather_surfaces_blocked_workers_and_latest_retro(tmp_path):
    data = briefing.gather_briefing(_full_fixture(tmp_path / "ws"), NOW)
    assert [b["branch"] for b in data["blocked"]] == ["swarm/03-d"]
    # Summary is extracted from the worker's final_line JSON, not a top field.
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


def test_naive_now_against_tzaware_cycle_does_not_raise(tmp_path):
    # The production path: the route passes a naive local `datetime.now()` while
    # cycles.jsonl carries tz-aware UTC timestamps. Comparing the two unnormalised
    # raises TypeError; this asserts the normalisation keeps it working.
    naive_now = briefing._to_naive_local(NOW)
    ws = _seed(
        tmp_path / "ws",
        cycles=[{"iteration": 1, "ended_at": _iso(NOW), "merged": ["m"]}],
    )
    data = briefing.gather_briefing(ws, naive_now)
    assert data["cycles_since_midnight"] == 1
    assert data["merged"] == ["m"]


def test_started_at_used_when_ended_at_missing(tmp_path):
    ws = _seed(
        tmp_path / "ws",
        cycles=[{"iteration": 1, "started_at": _iso(NOW), "merged": ["only-start"]}],
    )
    data = briefing.gather_briefing(ws, NOW)
    assert data["cycles_since_midnight"] == 1
    assert data["merged"] == ["only-start"]


def test_latest_retro_picks_highest_numbered_cycle(tmp_path):
    ws = _seed(
        tmp_path / "ws",
        cycles=[],
        retros={1: "# Cycle 001 retrospective\n", 12: "# Cycle 012 retrospective\n"},
    )
    cycle, md = briefing._latest_retro(ws)
    assert cycle == "012"
    assert "Cycle 012" in md


def test_naive_cycle_exactly_at_midnight_is_included(tmp_path):
    # A naive timestamp passes through unchanged, so this is tz-independent.
    midnight = NOW.astimezone().replace(tzinfo=None, hour=0, minute=0, second=0, microsecond=0)
    ws = _seed(
        tmp_path / "ws",
        cycles=[{"iteration": 1, "ended_at": midnight.isoformat(timespec="seconds"),
                 "merged": ["m"]}],
    )
    data = briefing.gather_briefing(ws, briefing._to_naive_local(NOW))
    assert data["cycles_since_midnight"] == 1


# --- failure modes -------------------------------------------------------------

def test_missing_cycles_log_degrades_to_empty(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    data = briefing.gather_briefing(ws, NOW)
    assert data["cycles_since_midnight"] == 0
    assert data["merged"] == []


def test_malformed_jsonl_lines_are_skipped(tmp_path):
    ws = tmp_path / "ws"
    (ws / "logs").mkdir(parents=True)
    good = json.dumps({"iteration": 9, "ended_at": _iso(NOW), "merged": ["good"]})
    (ws / "logs" / "cycles.jsonl").write_text(
        "{not json\n"            # malformed
        "[1,2,3]\n"               # well-formed JSON but not a dict
        f"{good}\n"
        "\n"                       # blank line
    )
    data = briefing.gather_briefing(ws, NOW)
    assert data["cycles_since_midnight"] == 1
    assert data["merged"] == ["good"]


def test_malformed_branch_entries_and_timestamps_are_skipped(tmp_path):
    ws = _seed(
        tmp_path / "ws",
        cycles=[
            {"iteration": 1, "ended_at": "garbage", "merged": ["x"]},
            {"iteration": 2, "merged": ["y"]},  # no timestamp at all
            {"iteration": 3, "ended_at": _iso(NOW), "merged": ["good", 42],
             "not_approved": ["already-a-string", {"no": "branch"}]},
        ],
    )
    data = briefing.gather_briefing(ws, NOW)
    # Only the well-formed, in-window cycle counts; the int branch is dropped.
    assert data["cycles_since_midnight"] == 1
    assert data["merged"] == ["good"]
    assert data["rejected"] == ["already-a-string"]


def test_blocked_worker_with_malformed_final_line_renders_empty_summary(tmp_path):
    ws = _seed(
        tmp_path / "ws",
        cycles=[],
        workers=[{"branch": "swarm/x", "status": "blocked", "final_line": "{bad"}],
    )
    data = briefing.gather_briefing(ws, NOW)
    assert data["blocked"] == [{"branch": "swarm/x", "summary": ""}]


def test_html_escapes_untrusted_branch_and_summary(tmp_path):
    ws = _seed(
        tmp_path / "ws",
        cycles=[],
        workers=[{"branch": "<script>", "status": "blocked",
                  "final_line": _final_line("blocked", "<b>x</b>")}],
    )
    html = briefing.render_briefing_html(briefing.gather_briefing(ws, NOW))
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
