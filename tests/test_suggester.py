"""Tests for feeding distilled lessons forward into the suggester prompt.

All artifacts live under ``tmp_path`` so the suite is deterministic and leaks
nothing outside the temp dir; no network or LLM calls are involved.
"""

from __future__ import annotations

from src import lessons, suggester


def _write_review(workspace, cycle, slug, branch, verdict, body):
    reviews_dir = workspace / "cycles" / f"{cycle:03d}" / "reviews"
    reviews_dir.mkdir(parents=True, exist_ok=True)
    (reviews_dir / f"{slug}.md").write_text(
        f"---\nbranch: {branch}\nverdict: {verdict}\n---\n\n{body}\n"
    )


def test_lessons_present_appear_in_rendered_prompt(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _write_review(workspace, 1, "a", "swarm/01-a", "reject", "Missing edge-case tests")
    _write_review(workspace, 2, "b", "swarm/02-b", "reject", "Missing edge-case tests")
    lessons.write_lessons(workspace)

    prompt = suggester.build_prompt(workspace, main_task="make it better")

    assert "Missing edge-case tests" in prompt
    assert "recurring complaints" in prompt
    assert "(2×)" in prompt
    assert lessons.LESSONS_PLACEHOLDER not in prompt
    assert "make it better" in prompt


def test_lessons_absent_no_file_renders_clean(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()

    prompt = suggester.build_prompt(workspace, main_task="ship it")

    assert lessons.LESSONS_PLACEHOLDER not in prompt
    assert "ship it" in prompt
    assert _LESSONS_GUIDANCE_ABSENT(prompt)


def test_empty_lessons_file_renders_clean(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    path = lessons.write_lessons(workspace)
    assert path.read_text() == ""

    prompt = suggester.build_prompt(workspace)

    assert lessons.LESSONS_PLACEHOLDER not in prompt
    assert _LESSONS_GUIDANCE_ABSENT(prompt)
    assert prompt.rstrip().endswith("independently mergeable changes.")


def test_load_lessons_returns_empty_when_missing(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()

    assert suggester._load_lessons(workspace) == ""


def test_main_task_only_substituted_no_lessons_leak(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()

    prompt = suggester.build_prompt(workspace, main_task="MANDATE")

    assert "<<main_task>>" not in prompt
    assert "MANDATE" in prompt


def _LESSONS_GUIDANCE_ABSENT(prompt: str) -> bool:
    """No lessons -> the guidance instruction must not appear in the prompt."""
    return "recurring complaints" not in prompt and "distilled from past reviews" not in prompt
