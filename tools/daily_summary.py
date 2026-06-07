"""Daily git-activity summary CLI.

Gathers the day's commits (``git log --since=<date> --no-merges`` with machine
parseable ``--numstat`` output), prints what was shipped, and asks ``claude -p``
for a short *future-you memo* describing the work and the most natural place to
pick up tomorrow.

The git-log parsing and date arithmetic are factored into pure functions so they
are unit-testable without a real repo or LLM. The LLM and git calls are injected
as callables (``runner``) so tests stay deterministic and offline.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime
from typing import Callable, Optional, Sequence

# ASCII control chars used as field/record separators in the git pretty format.
# They never appear in commit metadata, so parsing is unambiguous even when a
# subject line contains tabs, pipes, or commas.
_RS = "\x1e"  # record separator: prefixes each commit header line
_FS = "\x1f"  # field separator: between commit header fields

_PRETTY = f"{_RS}%H{_FS}%an{_FS}%ad{_FS}%s"

Runner = Callable[[Sequence[str]], str]


def default_since(now: datetime) -> str:
    """Return midnight (00:00:00) of ``now``'s day as an ISO-8601 string.

    ``now`` is passed in rather than read from the clock so callers (and tests)
    control the reference time; this keeps date arithmetic deterministic.
    """
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.isoformat()


def resolve_since(value: Optional[str], now: datetime) -> str:
    """Resolve the ``--since`` argument, falling back to midnight today.

    A blank or missing value yields :func:`default_since`; any other value is
    passed through verbatim so the user keeps git's full date-spec vocabulary
    (e.g. ``"2 days ago"``, ``"2026-06-01"``).
    """
    if value is None or not value.strip():
        return default_since(now)
    return value


def _run_git(args: Sequence[str]) -> str:
    return subprocess.run(
        list(args),
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def collect_git_log(since: str, *, runner: Optional[Runner] = None) -> str:
    """Return raw ``git log`` output for commits since ``since``.

    Uses ``--numstat`` for machine-readable per-file add/delete counts and a
    control-char ``--pretty`` format so :func:`parse_git_log` can split records
    without ambiguity. Merge commits are excluded to keep the summary about work
    actually authored.
    """
    if runner is None:
        runner = _run_git
    return runner(
        [
            "git",
            "log",
            f"--since={since}",
            "--no-merges",
            "--numstat",
            f"--pretty=format:{_PRETTY}",
        ]
    )


def _parse_numstat(line: str) -> Optional[dict]:
    """Parse one ``--numstat`` line (``added\\tdeleted\\tpath``).

    Binary files report ``-`` for the counts; those are treated as zero. Returns
    ``None`` for lines that are not valid numstat rows.
    """
    parts = line.split("\t")
    if len(parts) != 3:
        return None
    added_s, deleted_s, path = parts
    added = 0 if added_s == "-" else int(added_s) if added_s.isdigit() else None
    deleted = 0 if deleted_s == "-" else int(deleted_s) if deleted_s.isdigit() else None
    if added is None or deleted is None:
        return None
    return {"added": added, "deleted": deleted, "path": path}


def parse_git_log(text: str) -> dict:
    """Parse :func:`collect_git_log` output into structured activity data.

    Returns a dict with ``commits`` (each carrying its files), the count of
    unique ``files_changed``, and total ``insertions``/``deletions``. An empty
    or whitespace-only input yields a zeroed, empty result.
    """
    commits: list[dict] = []
    files_changed: set[str] = set()
    insertions = 0
    deletions = 0

    records = [r for r in text.split(_RS) if r.strip()]
    for record in records:
        lines = record.split("\n")
        header = lines[0]
        fields = header.split(_FS)
        if len(fields) != 4:
            continue
        commit_hash, author, date, subject = fields
        commit_files: list[dict] = []
        for line in lines[1:]:
            if not line.strip():
                continue
            parsed = _parse_numstat(line)
            if parsed is None:
                continue
            commit_files.append(parsed)
            files_changed.add(parsed["path"])
            insertions += parsed["added"]
            deletions += parsed["deleted"]
        commits.append(
            {
                "hash": commit_hash,
                "author": author,
                "date": date,
                "subject": subject,
                "files": commit_files,
            }
        )

    return {
        "commits": commits,
        "files_changed": len(files_changed),
        "insertions": insertions,
        "deletions": deletions,
    }


def format_activity(activity: dict) -> str:
    """Render parsed activity as a human-readable plain-text block."""
    commits = activity["commits"]
    lines = [
        f"Commits: {len(commits)}",
        f"Files changed: {activity['files_changed']}",
        f"Insertions: +{activity['insertions']}  Deletions: -{activity['deletions']}",
        "",
    ]
    if commits:
        lines.append("Commits:")
        for c in commits:
            short = c["hash"][:8]
            lines.append(f"  {short}  {c['subject']}")
    else:
        lines.append("No commits in this window.")
    return "\n".join(lines)


def build_prompt(since: str, activity: dict) -> str:
    """Build the ``claude -p`` prompt requesting a future-you memo."""
    return (
        "You are writing a short memo to your future self. Below is a summary of "
        "the git activity since "
        f"{since}. Write ONE paragraph addressed to 'you' that says what you got "
        "done today and where the most natural place to pick up tomorrow is. "
        "Write it as a memo ('here is what you did, here is what to pick up "
        "tomorrow'), NOT as a changelog or bullet list.\n\n"
        f"{format_activity(activity)}\n"
    )


def _run_claude(args: Sequence[str]) -> str:
    return subprocess.run(
        list(args),
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def generate_narrative(
    since: str,
    activity: dict,
    *,
    runner: Optional[Runner] = None,
) -> str:
    """Ask ``claude -p`` for the future-you memo; degrade gracefully on failure.

    If there are no commits, returns a fixed message and skips the LLM entirely.
    Any error from the ``claude`` CLI is caught so the structured summary above
    is still useful on its own.
    """
    if not activity["commits"]:
        return "No commits today — nothing to summarize. Fresh start tomorrow."
    if runner is None:
        runner = _run_claude
    prompt = build_prompt(since, activity)
    try:
        out = runner(["claude", "-p", prompt])
    except (subprocess.CalledProcessError, FileNotFoundError, OSError) as exc:
        return f"(narrative unavailable: {exc})"
    return out.strip()


def build_summary(
    since: str,
    *,
    git_runner: Optional[Runner] = None,
    claude_runner: Optional[Runner] = None,
) -> str:
    """Produce the full printable summary: activity block + narrative memo."""
    raw = collect_git_log(since, runner=git_runner)
    activity = parse_git_log(raw)
    narrative = generate_narrative(since, activity, runner=claude_runner)
    return f"{format_activity(activity)}\n\n--- Tomorrow memo ---\n{narrative}"


def main(argv: Optional[Sequence[str]] = None, *, now: Optional[datetime] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Summarize today's git activity with a future-you memo."
    )
    parser.add_argument(
        "--since",
        default=None,
        help="Git date spec for the start of the window (default: midnight today).",
    )
    ns = parser.parse_args(argv)

    reference = now if now is not None else datetime.now()
    since = resolve_since(ns.since, reference)
    print(build_summary(since))
    return 0


if __name__ == "__main__":
    sys.exit(main())
