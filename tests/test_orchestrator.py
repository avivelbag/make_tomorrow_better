"""Tests for the orchestrator wiring of the retrospective phase."""

from __future__ import annotations

import json

from src.orchestrator import Orchestrator


def _seed(workspace):
    workspace.mkdir()
    (workspace / "workers.json").write_text(
        json.dumps([{"branch": "swarm/01-a", "status": "completed", "commit": "a", "summary": "s"}])
    )
    reviews = workspace / "reviews"
    reviews.mkdir()
    (reviews / "a.md").write_text("---\nbranch: swarm/01-a\nverdict: approve\n---\n")
    logs = workspace / "logs"
    logs.mkdir()
    (logs / "merge-tests.log").write_text("=== merge swarm/01-a -> tests rc=0 ===\n")


def test_run_cycle_invokes_retro_after_merge_and_prints(tmp_path, capsys):
    ws = tmp_path / "ws"
    _seed(ws)
    calls = []
    orch = Orchestrator(ws, {"retrospective_enabled": True})
    orig_merge = orch._merge_reviewed
    orig_retro = orch._run_retrospective
    orch._merge_reviewed = lambda c: (calls.append("merge"), orig_merge(c))[1]
    orch._run_retrospective = lambda c: (calls.append("retro"), orig_retro(c))[1]

    result = orch.run_cycle(5)

    assert calls == ["merge", "retro"]
    assert (ws / "cycles" / "005" / "retro.md").is_file()
    out = capsys.readouterr().out
    assert "[retrospective] cycle 005" in out
    assert result["retro"]["summary"].startswith("[retrospective] cycle 005")


def test_retrospective_disabled_skips_phase(tmp_path, capsys):
    ws = tmp_path / "ws"
    _seed(ws)
    orch = Orchestrator(ws, {"retrospective_enabled": False})

    result = orch.run_cycle(2)

    assert result["retro"] is None
    assert not (ws / "cycles").exists()
    assert capsys.readouterr().out == ""


def test_retrospective_enabled_by_default(tmp_path, capsys):
    ws = tmp_path / "ws"
    _seed(ws)
    orch = Orchestrator(ws)

    orch.run_cycle(1)

    assert (ws / "cycles" / "001" / "retro.md").is_file()
    assert "[retrospective] cycle 001" in capsys.readouterr().out
