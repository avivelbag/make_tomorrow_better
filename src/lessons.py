"""Distill recurring reviewer feedback into a lessons file for worker prompts.

Reviewers write ``request-changes`` / ``reject`` verdicts every cycle, but that
hard-won signal otherwise dies in ``cycles/*/reviews/*.md``. This module reads
those review snapshots across all cycles, pulls the stated reason out of each
rejecting review, counts how often each distinct reason recurs, and writes a
deduplicated, frequency-ranked ``_lessons.md`` (top recurring issues only).

The rendered lessons are meant to be injected into the worker prompt via the
``<<lessons>>`` placeholder so the swarm stops repeating the same mistakes.
This project has no ``load_prompt()`` machinery, so :func:`load_lessons` and
:func:`substitute_lessons` stand in for that flow: they read ``_lessons.md``
(empty string when absent) and substitute it into a template.

Everything here is pure-Python and deterministic — no LLM call — so it is free,
reproducible, and unit-testable.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

# Verdicts whose reviews carry corrective signal worth learning from.
_LESSON_VERDICTS = ("reject", "request-changes")

# Keep the file (and therefore the prompt) bounded: at most this many distinct
# recurring reasons, each truncated to this many characters.
DEFAULT_TOP_N = 10
MAX_REASON_LEN = 200

LESSONS_FILENAME = "_lessons.md"
LESSONS_PLACEHOLDER = "<<lessons>>"


def _read_text(path: Path) -> str:
    try:
        return path.read_text()
    except (OSError, UnicodeDecodeError):
        return ""


def _split_frontmatter(text: str) -> tuple[str, str]:
    """Return ``(frontmatter, body)`` for a ``---``-delimited markdown file.

    Mirrors the tolerant frontmatter handling in :mod:`src.retrospective`. If
    the text has no leading ``---`` block, the frontmatter is empty and the
    whole text is treated as the body.
    """
    if not text.startswith("---"):
        return "", text
    end = text.find("\n---", 3)
    if end < 0:
        return "", text
    front = text[3:end]
    # Skip past the closing "\n---" line to the start of the body.
    body_start = text.find("\n", end + 1)
    body = text[body_start + 1 :] if body_start >= 0 else ""
    return front, body


def _parse_verdict(front: str) -> str:
    for line in front.splitlines():
        line = line.strip()
        if line.startswith("verdict:"):
            return line.split(":", 1)[1].strip().lower()
    return ""


def extract_reason(body: str) -> str:
    """Pull the first meaningful block of a review body as its "reason".

    Leading blank lines and markdown headings (``#`` / ``##`` …) are skipped,
    then the first contiguous run of non-blank lines is taken. A bullet list is
    flattened into a single ``"a; b; c"`` line; a plain paragraph is joined with
    spaces. Returns ``""`` when the body has no prose.
    """
    block: list[str] = []
    for raw in body.splitlines():
        line = raw.strip()
        if not block:
            if not line or line.startswith("#"):
                continue
            block.append(line)
        else:
            if not line:
                break
            block.append(line)
    if not block:
        return ""

    is_bullets = all(re.match(r"^[-*+]\s+", b) for b in block)
    if is_bullets:
        items = [re.sub(r"^[-*+]\s+", "", b).strip() for b in block]
        return "; ".join(i for i in items if i)
    return " ".join(block)


def _norm_key(reason: str) -> str:
    """Coarse dedup key: lowercase, strip punctuation, collapse whitespace.

    Two reasons that differ only in casing, trailing punctuation, or spacing
    collapse to the same key so their occurrences are counted together.
    """
    key = reason.lower()
    key = re.sub(r"[^a-z0-9]+", " ", key)
    return key.strip()


def parse_review(path: Path) -> tuple[str, str]:
    """Return ``(verdict, reason)`` for one review file.

    ``verdict`` is lowercased; ``reason`` is the extracted first block of the
    body. Either may be ``""`` for a malformed or empty file.
    """
    text = _read_text(path)
    front, body = _split_frontmatter(text)
    return _parse_verdict(front), extract_reason(body)


def collect_reasons(cycles_dir: Path) -> list[str]:
    """Gather rejection reasons from every ``cycles/*/reviews/*.md`` snapshot.

    Only reviews whose verdict is in :data:`_LESSON_VERDICTS` and which carry a
    non-empty reason contribute. Cycle dirs are visited in sorted order so the
    output is deterministic. Returns one reason string per qualifying review
    (duplicates intended — they drive the frequency ranking).
    """
    reasons: list[str] = []
    if not cycles_dir.is_dir():
        return reasons
    for cycle_dir in sorted(cycles_dir.iterdir()):
        reviews_dir = cycle_dir / "reviews"
        if not reviews_dir.is_dir():
            continue
        for review in sorted(reviews_dir.glob("*.md")):
            verdict, reason = parse_review(review)
            if verdict in _LESSON_VERDICTS and reason:
                reasons.append(reason)
    return reasons


def rank_lessons(reasons: list[str], top_n: int = DEFAULT_TOP_N) -> list[tuple[str, int]]:
    """Deduplicate and frequency-rank reasons, keeping the top ``top_n``.

    Reasons are grouped by their coarse :func:`_norm_key`; within a group the
    first-seen representative (truncated to :data:`MAX_REASON_LEN`) is kept for
    display. Groups are ordered by descending count, ties broken alphabetically
    on the representative text for stability. One-off reasons fall off the end
    once the recurring ones fill the cap.
    """
    counts: Counter[str] = Counter()
    representative: dict[str, str] = {}
    for reason in reasons:
        key = _norm_key(reason)
        if not key:
            continue
        counts[key] += 1
        if key not in representative:
            representative[key] = reason.strip()[:MAX_REASON_LEN]

    ranked = sorted(
        ((representative[k], c) for k, c in counts.items()),
        key=lambda rc: (-rc[1], rc[0]),
    )
    return ranked[:top_n]


def render_lessons(ranked: list[tuple[str, int]]) -> str:
    """Render ranked lessons as markdown. Empty string when there are none."""
    if not ranked:
        return ""
    lines = [
        "# Lessons from past reviews",
        "",
        "Recurring reasons branches were sent back in review "
        "(request-changes / reject), most frequent first. Address these "
        "proactively before you commit.",
        "",
    ]
    for i, (reason, count) in enumerate(ranked, 1):
        lines.append(f"{i}. ({count}×) {reason}")
    lines.append("")
    return "\n".join(lines)


def build_lessons(workspace: Path, top_n: int = DEFAULT_TOP_N) -> str:
    """Compute the rendered lessons markdown for an instance workspace."""
    reasons = collect_reasons(Path(workspace) / "cycles")
    return render_lessons(rank_lessons(reasons, top_n=top_n))


def write_lessons(workspace: Path, top_n: int = DEFAULT_TOP_N) -> Path:
    """Build and write ``_lessons.md`` under ``workspace``; return its path.

    When no rejecting reviews exist the file is written empty (zero bytes) so
    downstream :func:`load_lessons` substitutes an empty string rather than
    stale content.
    """
    workspace = Path(workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    out_path = workspace / LESSONS_FILENAME
    out_path.write_text(build_lessons(workspace, top_n=top_n))
    return out_path


def load_lessons(workspace: Path) -> str:
    """Read ``_lessons.md`` content for prompt injection; ``""`` when absent."""
    path = Path(workspace) / LESSONS_FILENAME
    return _read_text(path).strip()


def substitute_lessons(template: str, workspace: Path) -> str:
    """Replace the ``<<lessons>>`` placeholder with the instance's lessons.

    Stands in for the ``load_prompt()`` flow described in the suggestion: the
    placeholder is filled from ``_lessons.md`` and becomes the empty string when
    that file is missing or empty.
    """
    return template.replace(LESSONS_PLACEHOLDER, load_lessons(workspace))
