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


def _seed_cycle_snapshot(workspace, cycle, branch, verdict):
    cdir = workspace / "cycles" / cycle
    reviews = cdir / "reviews"
    reviews.mkdir(parents=True, exist_ok=True)
    (cdir / "workers.json").write_text(
        json.dumps(
            [{"branch": branch, "status": "completed", "commit": "x", "summary": "9 tests"}]
        )
    )
    (reviews / "r.md").write_text(f"---\nbranch: {branch}\nverdict: {verdict}\n---\n")


def test_run_cycle_records_and_logs_betterness(tmp_path, capsys):
    ws = tmp_path / "ws"
    _seed(ws)
    _seed_cycle_snapshot(ws, "007", "swarm/07-a", "approve")

    result = Orchestrator(ws, {"retrospective_enabled": True}).run_cycle(7)

    out = capsys.readouterr().out
    assert "[betterness] cycle 007" in out
    assert (ws / "betterness.jsonl").is_file()
    assert result["betterness"]["branches_merged"] == 1
    assert result["betterness"]["delta_vs_yesterday"] is None


def test_betterness_skipped_when_retrospective_disabled(tmp_path, capsys):
    ws = tmp_path / "ws"
    _seed(ws)
    orch = Orchestrator(ws, {"retrospective_enabled": False})

    result = orch.run_cycle(3)

    assert result["betterness"] is None
    assert not (ws / "betterness.jsonl").exists()
    assert capsys.readouterr().out == ""


def _seed_rejecting_review(workspace, cycle, slug, branch, reason):
    reviews = workspace / "cycles" / f"{cycle:03d}" / "reviews"
    reviews.mkdir(parents=True, exist_ok=True)
    (reviews / f"{slug}.md").write_text(
        f"---\nbranch: {branch}\nverdict: reject\n---\n\n## Summary\n{reason}\n"
    )


def test_run_cycle_refreshes_lessons_from_prior_reviews(tmp_path, capsys):
    ws = tmp_path / "ws"
    _seed(ws)
    _seed_rejecting_review(ws, 1, "a", "swarm/01-a", "Missing tests for failure mode.")
    _seed_rejecting_review(ws, 2, "b", "swarm/02-b", "Missing tests for failure mode.")

    result = Orchestrator(ws).run_cycle(3)

    lessons_file = ws / "_lessons.md"
    assert lessons_file.is_file()
    assert result["lessons"] == lessons_file
    text = lessons_file.read_text()
    assert "(2×) Missing tests for failure mode." in text


def test_run_cycle_writes_empty_lessons_when_no_rejections(tmp_path):
    ws = tmp_path / "ws"
    _seed(ws)

    Orchestrator(ws).run_cycle(1)

    lessons_file = ws / "_lessons.md"
    assert lessons_file.is_file()
    assert lessons_file.read_text() == ""


def test_lessons_disabled_skips_refresh(tmp_path):
    ws = tmp_path / "ws"
    _seed(ws)
    _seed_rejecting_review(ws, 1, "a", "swarm/01-a", "Some recurring problem.")

    result = Orchestrator(ws, {"lessons_enabled": False}).run_cycle(2)

    assert result["lessons"] is None
    assert not (ws / "_lessons.md").exists()
