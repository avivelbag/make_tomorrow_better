"""Tests for the post-cycle retrospective agent.

All artifacts are mocked under ``tmp_path`` so the suite is deterministic and
leaks nothing outside the temp dir; no network or LLM calls are involved.
"""

from __future__ import annotations

import json

import pytest

from src import retrospective as retro


def _write_review(reviews_dir, slug, branch, verdict):
    reviews_dir.mkdir(parents=True, exist_ok=True)
    (reviews_dir / f"{slug}.md").write_text(
        f"---\nbranch: {branch}\nverdict: {verdict}\n---\n\n## Summary\nbody\n"
    )


def _seed(tmp_path, *, workers, reviews, merge_lines):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "workers.json").write_text(json.dumps(workers))
    reviews_dir = workspace / "reviews"
    for slug, branch, verdict in reviews:
        _write_review(reviews_dir, slug, branch, verdict)
    log_dir = workspace / "logs"
    log_dir.mkdir()
    (log_dir / "merge-tests.log").write_text("\n".join(merge_lines) + "\n")
    return workspace


def test_happy_path_full_structure(tmp_path):
    workspace = _seed(
        tmp_path,
        workers=[
            {"branch": "swarm/01-a", "status": "completed", "commit": "aaa", "summary": "did a"},
            {"branch": "swarm/01-b", "status": "completed", "commit": "bbb", "summary": "did b"},
            {"branch": "swarm/01-c", "status": "blocked", "commit": None, "summary": "stuck"},
        ],
        reviews=[
            ("a", "swarm/01-a", "approve"),
            ("b", "swarm/01-b", "reject"),
        ],
        merge_lines=["[10:00:00] === merge swarm/01-a -> tests rc=0 ==="],
    )

    result = retro.run(workspace=workspace, cycle=1)

    retro_path = workspace / "cycles" / "001" / "retro.md"
    assert result["path"] == retro_path
    assert retro_path.is_file()

    text = retro_path.read_text()
    # Three mandated sections present.
    assert "## Branch verdicts" in text
    assert "## Top patterns" in text
    assert "## Tweak to try next cycle" in text

    # One-line verdict per branch with the correct classification.
    outcomes = {o["branch"]: o["outcome"] for o in result["outcomes"]}
    assert outcomes == {
        "swarm/01-a": retro.MERGED,
        "swarm/01-b": retro.REJECTED,
        "swarm/01-c": retro.BLOCKED,
    }

    # Top patterns: at most 3, non-empty.
    assert 1 <= len(result["patterns"]) <= 3
    # Exactly one concrete tweak.
    assert isinstance(result["tweak"], str) and result["tweak"]
    assert "[retrospective] cycle 001" in result["summary"]


def test_approved_but_failed_merge_tests_is_rejected(tmp_path):
    workspace = _seed(
        tmp_path,
        workers=[{"branch": "swarm/01-x", "status": "completed", "commit": "x"}],
        reviews=[("x", "swarm/01-x", "approve")],
        merge_lines=["=== merge swarm/01-x -> tests rc=1 ==="],
    )
    result = retro.run(workspace=workspace, cycle=1)
    assert result["outcomes"][0]["outcome"] == retro.REJECTED


def test_timeout_rc_counts_as_failure(tmp_path):
    results = retro.parse_merge_results(_log_with(tmp_path, "=== merge b -> tests rc=timeout ==="))
    assert results == {"b": False}


def _log_with(tmp_path, line):
    p = tmp_path / "merge-tests.log"
    p.write_text(line + "\n")
    return p


def test_empty_cycle_no_artifacts(tmp_path):
    """Missing artifacts must yield a retro noting the absence, not an error."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    result = retro.run(workspace=workspace, cycle=7)

    retro_path = workspace / "cycles" / "007" / "retro.md"
    assert retro_path.is_file()
    assert result["outcomes"] == []
    assert result["patterns"] == ["No worker branches were produced this cycle."]
    text = retro_path.read_text()
    assert "Cycle 007 retrospective" in text
    assert "(no branches this cycle)" in text


def test_large_input_patterns_capped_at_three(tmp_path):
    workers = [
        {"branch": f"swarm/01-{i}", "status": "blocked", "commit": None}
        for i in range(20)
    ]
    workspace = _seed(tmp_path, workers=workers, reviews=[], merge_lines=[""])
    result = retro.run(workspace=workspace, cycle=2)
    assert len(result["outcomes"]) == 20
    assert all(o["outcome"] == retro.BLOCKED for o in result["outcomes"])
    assert len(result["patterns"]) <= 3
    # Dominant-blocked pattern surfaces first.
    assert "blocked" in result["patterns"][0].lower()


def test_malformed_workers_json_degrades_gracefully(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "workers.json").write_text("{not json")
    assert retro.parse_workers(workspace / "workers.json") == []
    # And a non-array JSON value is also ignored.
    (workspace / "workers.json").write_text('{"branch": "x"}')
    assert retro.parse_workers(workspace / "workers.json") == []


def test_review_with_no_matching_worker_still_appears(tmp_path):
    workspace = _seed(
        tmp_path,
        workers=[],
        reviews=[("only", "swarm/01-only", "approve")],
        merge_lines=["=== merge swarm/01-only -> tests rc=0 ==="],
    )
    result = retro.run(workspace=workspace, cycle=3)
    branches = [o["branch"] for o in result["outcomes"]]
    assert branches == ["swarm/01-only"]
    assert result["outcomes"][0]["outcome"] == retro.MERGED


def test_classify_branch_precedence():
    # Blocked worker beats any verdict.
    assert retro.classify_branch("blocked", "approve", True) == retro.BLOCKED
    # request-changes -> rejected.
    assert retro.classify_branch("completed", "request-changes", None) == retro.REJECTED
    # approve with no merge record -> merged.
    assert retro.classify_branch("completed", "approve", None) == retro.MERGED
    # completed but unreviewed -> blocked (never cleared review).
    assert retro.classify_branch("completed", "", None) == retro.BLOCKED


def test_parse_review_verdicts_skips_malformed(tmp_path):
    reviews_dir = tmp_path / "reviews"
    reviews_dir.mkdir()
    (reviews_dir / "good.md").write_text("---\nbranch: b1\nverdict: approve\n---\n")
    (reviews_dir / "no-frontmatter.md").write_text("just text, no frontmatter\n")
    (reviews_dir / "no-branch.md").write_text("---\nverdict: reject\n---\n")
    verdicts = retro.parse_review_verdicts(reviews_dir)
    assert verdicts == {"b1": "approve"}


def test_parse_review_verdicts_missing_dir(tmp_path):
    assert retro.parse_review_verdicts(tmp_path / "nope") == {}


def test_run_creates_nested_cycle_dir(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    retro.run(workspace=workspace, cycle=123)
    assert (workspace / "cycles" / "123" / "retro.md").is_file()


def test_separate_log_dir_argument(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "workers.json").write_text(
        json.dumps([{"branch": "swarm/01-z", "status": "completed", "commit": "z"}])
    )
    _write_review(workspace / "reviews", "z", "swarm/01-z", "approve")
    alt_logs = tmp_path / "elsewhere"
    alt_logs.mkdir()
    (alt_logs / "merge-tests.log").write_text("=== merge swarm/01-z -> tests rc=0 ===\n")
    result = retro.run(workspace=workspace, cycle=4, log_dir=alt_logs)
    assert result["outcomes"][0]["outcome"] == retro.MERGED


def test_suggest_tweak_targets_dominant_failure(tmp_path):
    blocked = [{"branch": f"b{i}", "outcome": retro.BLOCKED} for i in range(3)]
    assert "suggester" in retro.suggest_tweak(blocked, {}).lower()

    rejected = [{"branch": f"b{i}", "outcome": retro.REJECTED} for i in range(3)]
    assert "worker" in retro.suggest_tweak(rejected, {}).lower()

    healthy = [{"branch": f"b{i}", "outcome": retro.MERGED} for i in range(3)]
    assert "hold" in retro.suggest_tweak(healthy, {}).lower()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
