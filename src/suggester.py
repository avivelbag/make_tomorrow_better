"""Suggestion generator prompt builder.

The suggester opens each cycle by proposing the batch of
``workspace/suggestions/*.md`` the ranker and workers act on. A lesson only
makes tomorrow better if the *next* cycle reads it, so this module threads the
distilled review feedback (:mod:`src.lessons`, written to ``_lessons.md``) into
the suggester prompt through the shared ``<<lessons>>`` placeholder.

When ``_lessons.md`` is missing or empty the placeholder collapses to nothing —
no header, no instruction, no dangling token — so a cold start behaves exactly
as it did before lessons existed. Pure-Python and deterministic: no LLM call, so
the rendered prompt is reproducible and unit-testable.
"""

from __future__ import annotations

from pathlib import Path

from src import lessons

# The suggester system prompt. ``<<main_task>>`` carries the instance mandate and
# ``<<lessons>>`` is filled from _lessons.md (empty when there are none).
SUGGESTER_PROMPT_TEMPLATE = """\
You generate concrete, reviewable improvement suggestions for the swarm.

MAIN TASK:
<<main_task>>

Write each suggestion as its own markdown file with a title, rationale, and
acceptance criteria. Favour small, additive, independently mergeable changes.
<<lessons>>"""

# Wraps the raw lessons markdown with the instruction that turns it into
# actionable guidance. Only emitted when there are lessons to show.
_LESSONS_GUIDANCE = (
    "Before proposing anything, study the lessons distilled from past reviews "
    "below. Avoid repeating these mistakes, and prefer ideas that address the "
    "recurring complaints reviewers keep raising.\n\n"
)


def _load_lessons(workspace: Path) -> str:
    """Return the distilled lessons text for ``workspace`` (``""`` when absent)."""
    return lessons.load_lessons(workspace)


def _lessons_section(workspace: Path) -> str:
    """Render the lessons block for the prompt, or ``""`` when there are none.

    A leading newline separates the block from the preceding prompt text. When
    no lessons exist the section is empty so the rendered prompt carries neither
    a header nor an instruction demanding lessons that do not exist.
    """
    text = _load_lessons(workspace)
    if not text:
        return ""
    return "\n" + _LESSONS_GUIDANCE + text


def build_prompt(workspace: Path, main_task: str = "") -> str:
    """Render the suggester prompt with lessons fed forward from prior reviews.

    ``<<lessons>>`` is replaced with the rendered lessons section (or an empty
    string), so the result never contains a dangling placeholder regardless of
    whether ``_lessons.md`` exists. ``<<main_task>>`` is filled from ``main_task``.
    """
    template = SUGGESTER_PROMPT_TEMPLATE.replace(
        lessons.LESSONS_PLACEHOLDER, _lessons_section(Path(workspace))
    )
    return template.replace("<<main_task>>", main_task)
