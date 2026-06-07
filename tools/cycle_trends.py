"""Cycle-trends CLI: quantify whether the swarm is improving over time.

Reads every ``workspace/<instance>/cycles/*/workers.json`` snapshot, derives
per-cycle metrics (branches attempted / merged / blocked / merge rate), prints a
chronological table, and ends with a verdict comparing the most recent cycle's
merge rate to the trailing average of all prior cycles. Pure stdlib and fully
deterministic — no LLM call — so it stays fast and unit-testable, complementing
the narrative ``tools/daily_summary.py``.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# A worker counts as "merged" only when it finished and produced a commit; any
# other state (blocked, no commit, unknown) is folded into blocked/rejected.
_COMPLETED = "completed"

# Cycle snapshot dirs are zero-padded numeric (e.g. "001"); anything else (a
# "latest" symlink, stray files) is ignored so the ordering stays numeric.
_CYCLE_DIR_RE = re.compile(r"^(\d+)$")

# Merge-rate deltas smaller than this are treated as "flat" so float noise and
# trivial wobble don't read as real movement.
_FLAT_EPS = 1e-9


def _read_workers(workers_json: Path) -> list[dict]:
    try:
        text = workers_json.read_text()
    except (OSError, UnicodeDecodeError):
        return []
    if not text.strip():
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    return [w for w in data if isinstance(w, dict)]


def compute_cycle_metrics(workers: list[dict]) -> dict:
    """Reduce one cycle's worker entries to merge metrics.

    A branch is "merged" when its status is ``completed`` and it carries a
    truthy commit; everything else is blocked/rejected. ``merge_rate`` guards
    against divide-by-zero, returning 0.0 for an empty cycle.
    """
    attempted = len(workers)
    merged = sum(
        1
        for w in workers
        if str(w.get("status") or "").strip() == _COMPLETED and w.get("commit")
    )
    blocked = attempted - merged
    merge_rate = merged / attempted if attempted else 0.0
    return {
        "attempted": attempted,
        "merged": merged,
        "blocked": blocked,
        "merge_rate": merge_rate,
    }


def discover_instances(workspace: Path) -> list[str]:
    if not workspace.is_dir():
        return []
    names = []
    for child in sorted(workspace.iterdir()):
        if child.is_dir() and (child / "cycles").is_dir():
            names.append(child.name)
    return names


def resolve_instance(workspace: Path, instance: str | None) -> str:
    """Pick the instance to report on, defaulting when exactly one exists.

    Raises ``ValueError`` with an actionable message when the requested instance
    is absent, or when no/ambiguous instances exist and none was specified.
    """
    available = discover_instances(workspace)
    if instance is not None:
        if instance not in available:
            raise ValueError(
                f"instance {instance!r} has no cycle data under {workspace}"
                + (f" (available: {', '.join(available)})" if available else "")
            )
        return instance
    if not available:
        raise ValueError(f"no instances with cycle data found under {workspace}")
    if len(available) > 1:
        raise ValueError(
            "multiple instances found; pass --instance "
            f"(choices: {', '.join(available)})"
        )
    return available[0]


def load_cycles(instance_dir: Path) -> list[dict]:
    """Return per-cycle metrics for an instance, ordered by numeric cycle index.

    Each entry is ``{"cycle": int, ...compute_cycle_metrics fields}``. Cycle
    dirs without a ``workers.json`` are skipped entirely.
    """
    cycles_dir = instance_dir / "cycles"
    if not cycles_dir.is_dir():
        return []
    rows: list[dict] = []
    for child in cycles_dir.iterdir():
        if not child.is_dir():
            continue
        m = _CYCLE_DIR_RE.match(child.name)
        if not m:
            continue
        workers_json = child / "workers.json"
        if not workers_json.is_file():
            continue
        metrics = compute_cycle_metrics(_read_workers(workers_json))
        rows.append({"cycle": int(m.group(1)), **metrics})
    rows.sort(key=lambda r: r["cycle"])
    return rows


def compute_verdict(rates: list[float]) -> dict:
    """Compare the latest merge rate to the mean of all prior cycles.

    Returns ``{"verdict": "better"|"worse"|"flat", "latest", "baseline",
    "delta", "message"}``. With fewer than two cycles there is nothing to
    compare against, so the verdict is "flat" with an explanatory message.
    """
    if len(rates) < 2:
        return {
            "verdict": "flat",
            "latest": rates[-1] if rates else 0.0,
            "baseline": None,
            "delta": 0.0,
            "message": "tomorrow is flat: not enough cycles to compare yet",
        }
    latest = rates[-1]
    prior = rates[:-1]
    baseline = sum(prior) / len(prior)
    delta = latest - baseline
    if delta > _FLAT_EPS:
        verdict = "better"
    elif delta < -_FLAT_EPS:
        verdict = "worse"
    else:
        verdict = "flat"
    message = (
        f"tomorrow is {verdict}: latest merge rate {latest:.0%} vs "
        f"trailing average {baseline:.0%} (delta {delta:+.0%})"
    )
    return {
        "verdict": verdict,
        "latest": latest,
        "baseline": baseline,
        "delta": delta,
        "message": message,
    }


def render_table(rows: list[dict]) -> str:
    header = f"{'cycle':>5}  {'attempted':>9}  {'merged':>6}  {'blocked':>7}  {'merge_rate':>10}"
    lines = [header, "-" * len(header)]
    for r in rows:
        lines.append(
            f"{r['cycle']:>5}  {r['attempted']:>9}  {r['merged']:>6}  "
            f"{r['blocked']:>7}  {r['merge_rate']:>9.0%}"
        )
    return "\n".join(lines)


def build_report(rows: list[dict]) -> str:
    table = render_table(rows)
    verdict = compute_verdict([r["merge_rate"] for r in rows])
    return f"{table}\n\n{verdict['message']}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Report swarm merge-rate trends across cycles."
    )
    parser.add_argument(
        "--workspace",
        type=Path,
        default=Path("workspace"),
        help="workspace root containing <instance>/cycles/ (default: workspace)",
    )
    parser.add_argument(
        "--instance",
        default=None,
        help="instance name; defaults to the only instance when exactly one exists",
    )
    args = parser.parse_args(argv)

    try:
        instance = resolve_instance(args.workspace, args.instance)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    rows = load_cycles(args.workspace / instance)
    if not rows:
        print(
            f"error: no cycle data found for instance {instance!r} under {args.workspace}",
            file=sys.stderr,
        )
        return 1

    print(f"Cycle trends for instance: {instance}\n")
    print(build_report(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
