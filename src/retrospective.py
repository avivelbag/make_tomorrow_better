"""Post-cycle retrospective agent.

Reads a cycle's artifacts (reviewer verdicts, worker results, merge/test log)
and writes a short actionable retrospective to ``workspace/cycles/<NNN>/retro.md``.
The synthesis is pure-Python and deterministic — no LLM call — so it is free,
reproducible, and unit-testable. The orchestrator calls :func:`run` after its
merge phase and prints the returned ``summary``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

# Timestamp prefix is optional so the parser tolerates both the raw and the
# timestamped merge-tests.log formats:
#   [22:31:04] === merge swarm/01-foo -> tests rc=0 ===
_MERGE_LINE_RE = re.compile(
    r"===\s*merge\s+(?P<branch>\S+)\s*->\s*tests\s+rc=(?P<rc>\S+?)\s*==="
)

MERGED = "merged"
BLOCKED = "blocked"
REJECTED = "rejected"

_CHANGE_VERDICTS = ("reject", "request-changes")


def _read_text(path: Path) -> str:
    try:
        return path.read_text()
    except (OSError, UnicodeDecodeError):
        return ""


def parse_review_verdicts(reviews_dir: Path) -> dict[str, str]:
    verdicts: dict[str, str] = {}
    if not reviews_dir.is_dir():
        return verdicts
    for f in sorted(reviews_dir.glob("*.md")):
        text = _read_text(f)
        if not text.startswith("---"):
            continue
        end = text.find("\n---", 3)
        if end < 0:
            continue
        meta: dict[str, str] = {}
        for line in text[3:end].splitlines():
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("-"):
                continue
            if ":" in line:
                k, v = line.split(":", 1)
                meta[k.strip()] = v.strip()
        branch = meta.get("branch")
        if branch:
            verdicts[branch] = meta.get("verdict", "unknown")
    return verdicts


def parse_workers(workers_json: Path) -> list[dict]:
    text = _read_text(workers_json)
    if not text.strip():
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    return [w for w in data if isinstance(w, dict)]


def parse_merge_results(merge_log: Path) -> dict[str, bool]:
    results: dict[str, bool] = {}
    text = _read_text(merge_log)
    if not text:
        return results
    for m in _MERGE_LINE_RE.finditer(text):
        results[m.group("branch")] = m.group("rc") == "0"
    return results


def classify_branch(status: str, verdict: str, merge_passed: bool | None) -> str:
    if status == "blocked":
        return BLOCKED
    if verdict in _CHANGE_VERDICTS:
        return REJECTED
    if verdict == "approve":
        # An approved branch whose post-merge tests failed was reverted.
        if merge_passed is False:
            return REJECTED
        return MERGED
    return BLOCKED


def build_branch_outcomes(
    workers: list[dict],
    verdicts: dict[str, str],
    merge_results: dict[str, bool],
) -> list[dict]:
    by_branch: dict[str, dict] = {}
    for w in workers:
        branch = w.get("branch")
        if branch:
            by_branch[branch] = w

    branches = set(by_branch) | set(verdicts)
    outcomes: list[dict] = []
    for branch in sorted(branches):
        w = by_branch.get(branch)
        status = str((w or {}).get("status") or "")
        if w is not None and not w.get("commit") and status not in ("completed", "blocked"):
            status = "blocked"
        verdict = verdicts.get(branch, "")
        merge_passed = merge_results.get(branch)
        outcome = classify_branch(status, verdict, merge_passed)
        outcomes.append(
            {
                "branch": branch,
                "outcome": outcome,
                "verdict": verdict or "(none)",
                "status": status or "(unknown)",
                "summary": str((w or {}).get("summary") or "").strip(),
            }
        )
    return outcomes


def detect_patterns(outcomes: list[dict], merge_results: dict[str, bool]) -> list[str]:
    total = len(outcomes)
    if total == 0:
        return ["No worker branches were produced this cycle."]

    n_merged = sum(1 for o in outcomes if o["outcome"] == MERGED)
    n_blocked = sum(1 for o in outcomes if o["outcome"] == BLOCKED)
    n_rejected = sum(1 for o in outcomes if o["outcome"] == REJECTED)
    n_test_fail = sum(1 for passed in merge_results.values() if passed is False)

    candidates: list[tuple[int, str]] = []
    if n_blocked:
        candidates.append(
            (n_blocked, f"{n_blocked} of {total} workers were blocked and shipped nothing.")
        )
    if n_rejected:
        candidates.append(
            (
                n_rejected,
                f"{n_rejected} of {total} branches were rejected in review or failed the merge gate.",
            )
        )
    if n_test_fail:
        candidates.append(
            (
                n_test_fail,
                f"{n_test_fail} branch(es) merged clean but failed the post-merge test gate.",
            )
        )
    if n_merged:
        candidates.append(
            (n_merged, f"{n_merged} of {total} branches merged successfully.")
        )

    candidates.sort(key=lambda c: c[0], reverse=True)
    patterns = [text for _, text in candidates[:3]]
    if not patterns:
        patterns.append(f"All {total} branches reached an indeterminate state.")
    return patterns


def suggest_tweak(outcomes: list[dict], merge_results: dict[str, bool]) -> str:
    total = len(outcomes) or 1
    n_blocked = sum(1 for o in outcomes if o["outcome"] == BLOCKED)
    n_rejected = sum(1 for o in outcomes if o["outcome"] == REJECTED)
    n_test_fail = sum(1 for passed in merge_results.values() if passed is False)

    if n_blocked >= n_rejected and n_blocked > total / 2:
        return (
            "Tweak the suggester prompt to include more concrete scaffolding and "
            "sharper acceptance criteria — too many workers blocked on ambiguity."
        )
    if n_test_fail:
        return (
            "Tighten the reviewer's test gate: require it to run and cite the full "
            "suite before approving — branches are passing review but failing post-merge."
        )
    if n_rejected:
        return (
            "Strengthen the worker prompt's testing requirements (explicit edge-case "
            "and failure-mode coverage) to cut review rejections next cycle."
        )
    return "Hold the current configuration — this cycle's outcomes were healthy."


def render_retro(
    cycle: int,
    outcomes: list[dict],
    patterns: list[str],
    tweak: str,
) -> str:
    lines = [f"# Cycle {cycle:03d} retrospective", ""]

    lines.append("## Branch verdicts")
    if outcomes:
        for o in outcomes:
            detail = f" — {o['summary']}" if o["summary"] else ""
            lines.append(f"- `{o['branch']}`: **{o['outcome']}**{detail}")
    else:
        lines.append("- (no branches this cycle)")
    lines.append("")

    lines.append("## Top patterns")
    for i, p in enumerate(patterns, 1):
        lines.append(f"{i}. {p}")
    lines.append("")

    lines.append("## Tweak to try next cycle")
    lines.append(f"- {tweak}")
    lines.append("")

    return "\n".join(lines)


def format_summary(cycle: int, outcomes: list[dict], retro_path: Path) -> str:
    counts = {MERGED: 0, BLOCKED: 0, REJECTED: 0}
    for o in outcomes:
        counts[o["outcome"]] = counts.get(o["outcome"], 0) + 1
    return (
        f"[retrospective] cycle {cycle:03d}: "
        f"{counts[MERGED]} merged, {counts[REJECTED]} rejected, "
        f"{counts[BLOCKED]} blocked -> {retro_path}"
    )


def run(
    *,
    workspace: Path,
    cycle: int,
    log_dir: Path | None = None,
) -> dict:
    workspace = Path(workspace)
    if log_dir is None:
        log_dir = workspace / "logs"

    verdicts = parse_review_verdicts(workspace / "reviews")
    workers = parse_workers(workspace / "workers.json")
    merge_results = parse_merge_results(Path(log_dir) / "merge-tests.log")

    outcomes = build_branch_outcomes(workers, verdicts, merge_results)
    patterns = detect_patterns(outcomes, merge_results)
    tweak = suggest_tweak(outcomes, merge_results)

    retro_md = render_retro(cycle, outcomes, patterns, tweak)
    out_dir = workspace / "cycles" / f"{cycle:03d}"
    out_dir.mkdir(parents=True, exist_ok=True)
    retro_path = out_dir / "retro.md"
    retro_path.write_text(retro_md)

    return {
        "path": retro_path,
        "outcomes": outcomes,
        "patterns": patterns,
        "tweak": tweak,
        "summary": format_summary(cycle, outcomes, retro_path),
    }
