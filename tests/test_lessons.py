"""Tests for the review-lessons distiller.

All artifacts live under ``tmp_path`` so the suite is deterministic and leaks
nothing outside the temp dir; no network or LLM calls are involved.
"""

from __future__ import annotations

import pytest

from src import lessons


def _write_review(workspace, cycle, slug, branch, verdict, body):
    reviews_dir = workspace / "cycles" / f"{cycle:03d}" / "reviews"
    reviews_dir.mkdir(parents=True, exist_ok=True)
    (reviews_dir / f"{slug}.md").write_text(
        f"---\nbranch: {branch}\nverdict: {verdict}\n---\n\n{body}\n"
    )


def test_no_reviews_yields_empty_lessons_file_and_substitution(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()

    path = lessons.write_lessons(workspace)

    assert path == workspace / lessons.LESSONS_FILENAME
    assert path.is_file()
    assert path.read_text() == ""
    assert lessons.load_lessons(workspace) == ""

    template = "before\n<<lessons>>\nafter"
    assert lessons.substitute_lessons(template, workspace) == "before\n\nafter"


def test_repeated_reason_outranks_one_off(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    # Same reason rejected in two separate cycles ...
    _write_review(
        workspace, 1, "a", "swarm/01-a", "reject",
        "## Summary\nMissing tests for the failure mode.",
    )
    _write_review(
        workspace, 2, "b", "swarm/02-b", "reject",
        "## Summary\nMissing tests for the failure mode.",
    )
    # ... versus a one-off reason in a single cycle.
    _write_review(
        workspace, 2, "c", "swarm/02-c", "request-changes",
        "## Summary\nVariable name is unclear.",
    )

    lessons.write_lessons(workspace)
    text = lessons.load_lessons(workspace)

    assert "Missing tests for the failure mode." in text
    assert "Variable name is unclear." in text
    # The twice-seen reason is ranked first and carries a 2x count.
    assert text.index("Missing tests") < text.index("Variable name")
    assert "(2×) Missing tests for the failure mode." in text
    assert "(1×) Variable name is unclear." in text


def test_approve_reviews_are_ignored(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _write_review(
        workspace, 1, "ok", "swarm/01-ok", "approve",
        "## Summary\nLooks great, shipping it.",
    )
    lessons.write_lessons(workspace)
    assert lessons.load_lessons(workspace) == ""


def test_dedup_normalizes_casing_and_punctuation(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _write_review(
        workspace, 1, "a", "swarm/01-a", "reject", "## Summary\nNo edge case coverage.",
    )
    _write_review(
        workspace, 2, "b", "swarm/02-b", "reject", "## Summary\nno edge case coverage",
    )
    lessons.write_lessons(workspace)
    text = lessons.load_lessons(workspace)
    # Both map to one entry counted twice.
    assert "(2×)" in text
    assert text.count("edge case coverage") == 1


def test_bullet_list_body_flattened(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _write_review(
        workspace, 1, "a", "swarm/01-a", "request-changes",
        "## Summary\n- first problem\n- second problem",
    )
    verdict, reason = lessons.parse_review(
        workspace / "cycles" / "001" / "reviews" / "a.md"
    )
    assert verdict == "request-changes"
    assert reason == "first problem; second problem"


def test_top_n_caps_distinct_reasons(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    for i in range(15):
        _write_review(
            workspace, 1, f"r{i}", f"swarm/01-{i}", "reject",
            f"## Summary\nDistinct problem number {i}.",
        )
    lessons.write_lessons(workspace, top_n=10)
    text = lessons.load_lessons(workspace)
    # Numbered list lines "N. (...)" — exactly ten of them.
    numbered = [ln for ln in text.splitlines() if ln[:3].rstrip(". ").isdigit() and "(" in ln]
    assert len(numbered) == 10


def test_reason_is_length_capped():
    long_reason = "x" * 500
    ranked = lessons.rank_lessons([long_reason])
    assert len(ranked) == 1
    rep, count = ranked[0]
    assert count == 1
    assert len(rep) == lessons.MAX_REASON_LEN


def test_malformed_review_without_frontmatter_skipped(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    reviews_dir = workspace / "cycles" / "001" / "reviews"
    reviews_dir.mkdir(parents=True)
    (reviews_dir / "bad.md").write_text("just some text, no frontmatter\n")
    lessons.write_lessons(workspace)
    assert lessons.load_lessons(workspace) == ""


def test_missing_cycles_dir_returns_no_reasons(tmp_path):
    assert lessons.collect_reasons(tmp_path / "nope" / "cycles") == []


def test_substitute_with_lessons_present(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _write_review(
        workspace, 1, "a", "swarm/01-a", "reject", "## Summary\nFlaky test added.",
    )
    lessons.write_lessons(workspace)
    out = lessons.substitute_lessons("HEAD\n<<lessons>>\nTAIL", workspace)
    assert "Flaky test added." in out
    assert "<<lessons>>" not in out
    assert out.startswith("HEAD\n")
    assert out.endswith("\nTAIL")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
