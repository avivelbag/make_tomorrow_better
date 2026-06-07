"""Carryover notes for branches that pass review but fail the merge test gate.

The merge gate merges a branch only when its review verdict is ``approve`` *and*
the post-merge ``tests_command`` passes. When the tests fail, the branch is
hard-reset out of ``main`` and — without this module — the work simply vanishes:
the next cycle has no signal the suggestion was attempted and broke. Here we turn
that dead end into a structured carryover note:
``workspace/<instance>/carryover/<branch-slug>-failed.md``. The note carries the
original suggestion's title and acceptance criteria, the branch name, and a
bounded tail of the failing test output pulled from ``logs/merge-tests.log``. Its
frontmatter is marked ``status: failed-merge`` so the next suggester can choose to
re-attempt with fixes rather than re-propose blindly or skip it.
"""

from __future__ import annotations

import pathlib
import re

import src.retrospective as retrospective
import src.scorer as scorer

DEFAULT_TAIL_LINES = 40

FAILED_STATUS = "failed-merge"

_MERGE_LINE_RE = re.compile(
    r"===\s*merge\s+(?P<branch>\S+)\s*->\s*tests\s+rc=(?P<rc>\S+?)\s*==="
)

_APPROVE = "approve"


def branch_slug(branch: str) -> str:
    """Map a branch name to the slug used for its workspace artifact files.

    Mirrors the reviewer's convention (``swarm/02-04-foo`` -> ``swarm-02-04-foo``)
    so the failed-merge note sits beside the branch's other per-branch files.
    """
    return branch.replace("/", "-")


def failing_tail(log_text: str, branch: str, max_lines: int = DEFAULT_TAIL_LINES) -> str:
    """Return up to ``max_lines`` trailing lines of ``branch``'s failing test output.

    ``merge-tests.log`` interleaves, per branch, the captured test output followed
    by a ``=== merge <branch> -> tests rc=N ===`` summary marker. The output for a
    given branch is therefore the block of lines between the previous marker and
    that branch's own marker (inclusive of the marker). We locate the first such
    block whose marker is for ``branch`` with a non-zero return code and return its
    last ``max_lines`` lines. A non-positive ``max_lines`` returns the whole block.
    Returns an empty string when the branch has no failing marker.
    """
    lines = log_text.splitlines()
    marker_idxs = [i for i, line in enumerate(lines) if _MERGE_LINE_RE.search(line)]
    prev = -1
    for idx in marker_idxs:
        m = _MERGE_LINE_RE.search(lines[idx])
        if m.group("branch") == branch and m.group("rc") != "0":
            block = lines[prev + 1 : idx + 1]
            tail = block if max_lines <= 0 else block[-max_lines:]
            return "\n".join(tail).strip()
        prev = idx
    return ""


def parse_title_acceptance(suggestion_text: str) -> tuple[str, list[str]]:
    """Pull the ``title`` and ``acceptance`` bullets out of a suggestion's frontmatter.

    Returns ``(title, acceptance_lines)``. ``acceptance_lines`` preserves the
    bullet text (without the leading ``-``) in document order; either part is empty
    when the frontmatter is missing or malformed.
    """
    frontmatter, _ = scorer.split_frontmatter(suggestion_text)
    title = ""
    acceptance: list[str] = []
    in_acceptance = False
    for raw in frontmatter.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if in_acceptance:
            if stripped.startswith("-"):
                acceptance.append(stripped[1:].strip())
                continue
            if re.match(r"^\S.*:", stripped):
                in_acceptance = False
        if stripped.lower().startswith("acceptance:"):
            in_acceptance = True
            rest = stripped.split(":", 1)[1].strip()
            if rest and rest != "[]":
                acceptance.append(rest)
            continue
        if not title and stripped.lower().startswith("title:"):
            title = stripped.split(":", 1)[1].strip().strip("'\"")
    return title, acceptance


def render_note(branch: str, title: str, acceptance: list[str], tail: str) -> str:
    """Render the markdown body of a failed-merge carryover note."""
    lines = [
        "---",
        f"title: {title}",
        f"status: {FAILED_STATUS}",
        f"branch: {branch}",
        "---",
        "",
        "This branch was approved by review but its post-merge test gate failed, "
        "so it was hard-reset out of the base branch. Re-attempt it with fixes "
        "rather than re-proposing from scratch or skipping it blindly.",
        "",
        "## Acceptance criteria",
    ]
    if acceptance:
        lines.extend(f"- {item}" for item in acceptance)
    else:
        lines.append("(none recorded)")
    lines.extend(["", "## Failing test output (tail)", "```"])
    lines.append(tail if tail else "(no captured output)")
    lines.append("```")
    return "\n".join(lines) + "\n"


def write_failed_merge_note(
    workspace: pathlib.Path,
    branch: str,
    suggestion_text: str,
    log_text: str = "",
    *,
    max_tail_lines: int = DEFAULT_TAIL_LINES,
) -> pathlib.Path:
    """Write a single ``<branch-slug>-failed.md`` carryover note and return its path.

    Creates ``workspace/carryover/`` if needed. Captures a bounded tail of the
    branch's failing test output from ``log_text`` (the contents of
    ``logs/merge-tests.log``).
    """
    workspace = pathlib.Path(workspace)
    title, acceptance = parse_title_acceptance(suggestion_text)
    tail = failing_tail(log_text, branch, max_tail_lines)
    carryover = workspace / "carryover"
    carryover.mkdir(parents=True, exist_ok=True)
    path = carryover / f"{branch_slug(branch)}-failed.md"
    path.write_text(render_note(branch, title, acceptance, tail))
    return path


def _find_suggestion_text(suggestions_dir: pathlib.Path, branch: str) -> str:
    """Best-effort lookup of the suggestion file that produced ``branch``.

    Branches are ``swarm/<cycle>-<NN>-<slug>`` while suggestion files are
    ``<NN>-<slug>.md``; match on the full stem first, then on the bare slug so a
    differing index prefix still links. Returns ``""`` when nothing matches.
    """
    if not suggestions_dir.is_dir():
        return ""
    files = sorted(suggestions_dir.glob("*.md"))
    for f in files:
        if branch.endswith(f.stem):
            return scorer._read_text(f)
    for f in files:
        slug = scorer._slug_of(f.stem)
        if slug and slug in branch:
            return scorer._read_text(f)
    return ""


def record_failed_merges(
    workspace: pathlib.Path,
    log_dir: pathlib.Path | None = None,
    *,
    max_tail_lines: int = DEFAULT_TAIL_LINES,
) -> list[pathlib.Path]:
    """Write carryover notes for every approved branch whose merge test gate failed.

    Scans ``logs/merge-tests.log`` for branches with a non-zero test return code,
    keeps only those whose review verdict was ``approve`` (a failing rc on a branch
    that was never going to merge carries no lost work), and writes one
    ``<branch-slug>-failed.md`` note per such branch. Returns the written paths,
    sorted by branch. Branches that merged cleanly produce no notes.
    """
    workspace = pathlib.Path(workspace)
    if log_dir is None:
        log_dir = workspace / "logs"
    log_text = scorer._read_text(pathlib.Path(log_dir) / "merge-tests.log")
    merge_results = retrospective.parse_merge_results(pathlib.Path(log_dir) / "merge-tests.log")
    verdicts = retrospective.parse_review_verdicts(workspace / "reviews")
    suggestions_dir = workspace / "suggestions"

    written: list[pathlib.Path] = []
    for branch in sorted(merge_results):
        if merge_results[branch]:
            continue
        if verdicts.get(branch) != _APPROVE:
            continue
        suggestion_text = _find_suggestion_text(suggestions_dir, branch)
        written.append(
            write_failed_merge_note(
                workspace,
                branch,
                suggestion_text,
                log_text,
                max_tail_lines=max_tail_lines,
            )
        )
    return written
