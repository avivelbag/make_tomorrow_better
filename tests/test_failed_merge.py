"""Tests for failed-merge carryover notes (src/failed_merge.py)."""

from __future__ import annotations

import json

import src.failed_merge as failed_merge
from src.orchestrator import Orchestrator

SUGGESTION = """---
title: Make the widget faster
acceptance:
  - It is fast
  - It does not crash
---

## Body
Do the thing.
"""


def _log(branch: str, rc: int, body: str = "") -> str:
    tail = (body + "\n") if body else ""
    return f"{tail}=== merge {branch} -> tests rc={rc} ===\n"


def _seed_branch(workspace, branch, slug, verdict, rc, *, log_body="", suggestion=SUGGESTION):
    (workspace / "reviews").mkdir(parents=True, exist_ok=True)
    (workspace / "reviews" / f"{slug}.md").write_text(
        f"---\nbranch: {branch}\nverdict: {verdict}\n---\n"
    )
    (workspace / "suggestions").mkdir(parents=True, exist_ok=True)
    (workspace / "suggestions" / f"{slug}.md").write_text(suggestion)
    (workspace / "logs").mkdir(parents=True, exist_ok=True)
    (workspace / "logs" / "merge-tests.log").write_text(_log(branch, rc, log_body))


# --- failing_tail -----------------------------------------------------------


def test_failing_tail_returns_block_for_failing_branch():
    log = "line1\nline2\n" + _log("swarm/01-a", 1)
    tail = failed_merge.failing_tail(log, "swarm/01-a")
    assert "line1" in tail and "line2" in tail
    assert "rc=1" in tail


def test_failing_tail_empty_for_passing_branch():
    assert failed_merge.failing_tail(_log("swarm/01-a", 0), "swarm/01-a") == ""


def test_failing_tail_empty_for_missing_branch():
    assert failed_merge.failing_tail(_log("swarm/01-a", 1), "swarm/99-z") == ""


def test_failing_tail_is_bounded():
    body = "\n".join(f"out{i}" for i in range(200))
    log = body + "\n" + _log("swarm/01-a", 1)
    tail = failed_merge.failing_tail(log, "swarm/01-a", max_lines=5)
    assert len(tail.splitlines()) == 5
    assert "out199" in tail
    assert "out0" not in tail


def test_failing_tail_isolates_each_branch():
    log = "aaa\n" + _log("swarm/01-a", 1) + "bbb\n" + _log("swarm/02-b", 1)
    tail_b = failed_merge.failing_tail(log, "swarm/02-b")
    assert "bbb" in tail_b
    assert "aaa" not in tail_b


# --- parse_title_acceptance -------------------------------------------------


def test_parse_title_acceptance():
    title, acceptance = failed_merge.parse_title_acceptance(SUGGESTION)
    assert title == "Make the widget faster"
    assert acceptance == ["It is fast", "It does not crash"]


def test_parse_title_acceptance_missing_frontmatter():
    title, acceptance = failed_merge.parse_title_acceptance("no frontmatter here")
    assert title == ""
    assert acceptance == []


# --- write_failed_merge_note ------------------------------------------------


def test_write_note_captures_title_acceptance_and_tail(tmp_path):
    path = failed_merge.write_failed_merge_note(
        tmp_path, "swarm/01-a", SUGGESTION, _log("swarm/01-a", 1, "FAILED test_x")
    )
    assert path == tmp_path / "carryover" / "swarm-01-a-failed.md"
    text = path.read_text()
    assert "status: failed-merge" in text
    assert "branch: swarm/01-a" in text
    assert "Make the widget faster" in text
    assert "- It is fast" in text
    assert "FAILED test_x" in text


def test_write_note_handles_no_captured_output(tmp_path):
    path = failed_merge.write_failed_merge_note(tmp_path, "swarm/01-a", SUGGESTION, "")
    assert "(no captured output)" in path.read_text()


# --- record_failed_merges ---------------------------------------------------


def test_record_writes_note_for_approved_failing_branch(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _seed_branch(ws, "swarm/01-a", "01-a", "approve", rc=1, log_body="E   assert 1 == 2")

    written = failed_merge.record_failed_merges(ws)

    assert len(written) == 1
    note = ws / "carryover" / "swarm-01-a-failed.md"
    assert note in written
    assert "assert 1 == 2" in note.read_text()


def test_record_no_note_for_passing_merge(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _seed_branch(ws, "swarm/01-a", "01-a", "approve", rc=0)

    written = failed_merge.record_failed_merges(ws)

    assert written == []
    assert not (ws / "carryover" / "swarm-01-a-failed.md").exists()


def test_record_skips_failing_branch_that_was_not_approved(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _seed_branch(ws, "swarm/01-a", "01-a", "reject", rc=1)

    written = failed_merge.record_failed_merges(ws)

    assert written == []


def test_record_handles_missing_log_dir(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    assert failed_merge.record_failed_merges(ws) == []


def test_record_matches_suggestion_by_slug_when_index_differs(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "reviews").mkdir()
    (ws / "reviews" / "r.md").write_text(
        "---\nbranch: swarm/004-07-make-the-widget-faster\nverdict: approve\n---\n"
    )
    (ws / "suggestions").mkdir()
    (ws / "suggestions" / "02-make-the-widget-faster.md").write_text(SUGGESTION)
    (ws / "logs").mkdir()
    (ws / "logs" / "merge-tests.log").write_text(
        _log("swarm/004-07-make-the-widget-faster", 1, "boom")
    )

    written = failed_merge.record_failed_merges(ws)

    assert len(written) == 1
    assert "Make the widget faster" in written[0].read_text()


# --- orchestrator wiring ----------------------------------------------------


def test_orchestrator_merge_reviewed_writes_failed_note(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "workers.json").write_text(
        json.dumps([{"branch": "swarm/01-a", "status": "completed", "commit": "a"}])
    )
    _seed_branch(ws, "swarm/01-a", "01-a", "approve", rc=1, log_body="traceback here")

    result = Orchestrator(ws)._merge_reviewed(1)

    assert len(result["failed_notes"]) == 1
    assert (ws / "carryover" / "swarm-01-a-failed.md").is_file()


def test_orchestrator_merge_reviewed_no_note_on_clean_merge(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _seed_branch(ws, "swarm/01-a", "01-a", "approve", rc=0)

    result = Orchestrator(ws)._merge_reviewed(1)

    assert result["failed_notes"] == []
    assert not (ws / "carryover").exists()
