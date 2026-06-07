"""Daily "betterness score": one comparable number per day for the swarm.

The instance mandate is "make tomorrow better than today". This module turns
that slogan into a tracked signal: it reduces every cycle snapshot under
``workspace/<instance>/cycles/*/`` to a single weighted ``score`` and records
one row per day in ``workspace/<instance>/betterness.jsonl``. Re-running a day
overwrites that day's row, and each row carries ``delta_vs_yesterday`` so the
orchestrator can log whether today beat the previous recorded day.

Pure-Python and deterministic (no LLM call). Cycle artifacts are parsed via the
existing :mod:`src.retrospective` helpers so the score stays auditable and the
snapshot data contract lives in exactly one place.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from src import retrospective

# Cycle snapshot dirs are zero-padded numeric (e.g. "001"); anything else (a
# stray file or "latest" symlink) is ignored so aggregation stays robust.
_CYCLE_DIR_RE = re.compile(r"^\d+$")

# Pulls an explicit test count out of a worker summary, e.g. "plus 19 unit
# tests" -> 19, "12 deterministic tests" -> 12. Up to two adjective words may
# sit between the number and "test(s)"; only the first match per summary counts.
_TESTS_RE = re.compile(r"(\d+)\s+(?:[A-Za-z]+\s+){0,2}tests?\b")

# Normalization caps keep every weighted term in [0, 1] and make the score
# auditable. A day with at least this many merged branches / added tests
# saturates the corresponding term.
_BRANCHES_NORM = 10
_TESTS_NORM = 50

# Weights sum to 1.0: merge rate dominates, test volume next, raw merged-branch
# count last. Kept explicit per the suggestion so the score is auditable.
_W_MERGE_RATE = 0.5
_W_TESTS = 0.3
_W_BRANCHES = 0.2

# Score returned when there is no cycle history at all, so day one neither
# rewards nor penalizes an empty workspace.
NEUTRAL_BASELINE = 0.0


def _tests_in_summary(summary: str) -> int:
    if not summary:
        return 0
    m = _TESTS_RE.search(summary)
    return int(m.group(1)) if m else 0


def _iter_cycle_dirs(cycles_dir: Path) -> list[Path]:
    if not cycles_dir.is_dir():
        return []
    dirs = [c for c in cycles_dir.iterdir() if c.is_dir() and _CYCLE_DIR_RE.match(c.name)]
    dirs.sort(key=lambda c: int(c.name))
    return dirs


def compute_daily_score(instance: str, *, workspace_root: Path | str = "workspace") -> dict:
    """Aggregate every cycle snapshot for an instance into one daily score.

    Walks ``workspace_root/<instance>/cycles/*/``, parsing each ``workers.json``
    and ``reviews/*.md`` through :mod:`src.retrospective` to classify branch
    outcomes. Returns a dict with:

    - ``branches_merged``: branches that merged (approved and not test-reverted),
      summed across cycles.
    - ``merge_rate``: ``branches_merged / branches_attempted`` (0.0 when none).
    - ``tests_added``: test counts parsed from worker summaries, summed.
    - ``score``: weighted float in [0, 1] (see the ``_W_*`` weights), or
      ``NEUTRAL_BASELINE`` when there is no cycle history at all.
    """
    cycles_dir = Path(workspace_root) / instance / "cycles"
    cycle_dirs = _iter_cycle_dirs(cycles_dir)
    if not cycle_dirs:
        return {
            "merge_rate": 0.0,
            "branches_merged": 0,
            "tests_added": 0,
            "score": NEUTRAL_BASELINE,
        }

    branches_attempted = 0
    branches_merged = 0
    tests_added = 0
    for cdir in cycle_dirs:
        workers = retrospective.parse_workers(cdir / "workers.json")
        verdicts = retrospective.parse_review_verdicts(cdir / "reviews")
        merge_results = retrospective.parse_merge_results(cdir / "merge-tests.log")
        outcomes = retrospective.build_branch_outcomes(workers, verdicts, merge_results)

        branches_attempted += len(workers)
        branches_merged += sum(
            1 for o in outcomes if o["outcome"] == retrospective.MERGED
        )
        for w in workers:
            tests_added += _tests_in_summary(str(w.get("summary") or ""))

    merge_rate = branches_merged / branches_attempted if branches_attempted else 0.0
    norm_tests = min(tests_added / _TESTS_NORM, 1.0)
    norm_branches = min(branches_merged / _BRANCHES_NORM, 1.0)
    score = (
        merge_rate * _W_MERGE_RATE
        + norm_tests * _W_TESTS
        + norm_branches * _W_BRANCHES
    )
    return {
        "merge_rate": merge_rate,
        "branches_merged": branches_merged,
        "tests_added": tests_added,
        "score": score,
    }


def _read_rows(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    try:
        text = path.read_text()
    except (OSError, UnicodeDecodeError):
        return []
    rows: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and "date" in obj:
            rows.append(obj)
    return rows


def record_daily_score(
    instance: str, *, date: str, workspace_root: Path | str = "workspace"
) -> dict:
    """Compute today's score and append/overwrite its row in betterness.jsonl.

    ``date`` is an explicit ``YYYY-MM-DD`` string (injected rather than read from
    the clock so callers and tests stay deterministic). The row keyed by
    ``date`` replaces any existing row for the same day, so re-running a day never
    duplicates. ``delta_vs_yesterday`` is the score change from the most recent
    earlier recorded day, or ``None`` when no prior day exists.

    Returns the full row written: the :func:`compute_daily_score` fields plus
    ``date`` and ``delta_vs_yesterday``.
    """
    metrics = compute_daily_score(instance, workspace_root=workspace_root)

    path = Path(workspace_root) / instance / "betterness.jsonl"
    existing = [r for r in _read_rows(path) if r.get("date") != date]

    prior = [r for r in existing if str(r.get("date", "")) < date]
    delta_vs_yesterday: float | None = None
    if prior:
        prev = max(prior, key=lambda r: str(r.get("date", "")))
        prev_score = prev.get("score")
        if isinstance(prev_score, (int, float)):
            delta_vs_yesterday = metrics["score"] - prev_score

    row = {"date": date, **metrics, "delta_vs_yesterday": delta_vs_yesterday}

    rows = existing + [row]
    rows.sort(key=lambda r: str(r.get("date", "")))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return row


def verdict_line(row: dict) -> str:
    """Render the one-line cycle-end verdict from a recorded row.

    Reports "today is better/worse/the same as yesterday (score X vs Y)" using
    ``delta_vs_yesterday``; falls back to a no-baseline message on day one.
    """
    score = row.get("score", 0.0)
    delta = row.get("delta_vs_yesterday")
    if delta is None:
        return f"today's betterness score is {score:.3f} (no prior day to compare)"
    yesterday = score - delta
    if delta > 0:
        word = "better"
    elif delta < 0:
        word = "worse"
    else:
        word = "the same as"
    return f"today is {word} than yesterday (score {score:.3f} vs {yesterday:.3f})"
