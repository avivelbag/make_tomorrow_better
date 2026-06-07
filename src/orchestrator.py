"""Cycle orchestrator.

Drives a swarm cycle: merge the reviewed branches, then run the post-cycle
retrospective and print its summary. Only the retrospective phase is wired up
here; the merge phase is a thin seam other cycle work hangs off of.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from src import improvement_metric, retrospective


class Orchestrator:
    def __init__(self, workspace: Path, config: dict | None = None):
        self.workspace = Path(workspace)
        self.config = config or {}

    def _merge_reviewed(self, cycle: int) -> dict:
        return {}

    def _run_retrospective(self, cycle: int) -> dict | None:
        if not self.config.get("retrospective_enabled", True):
            return None
        result = retrospective.run(workspace=self.workspace, cycle=cycle)
        print(result["summary"])
        return result

    def _record_betterness(self, cycle: int) -> dict | None:
        # Record today's betterness score and log whether today beat yesterday.
        # self.workspace is the instance dir, so split it into root + name for
        # the workspace_root/<instance>/cycles layout the metric reads.
        try:
            row = improvement_metric.record_daily_score(
                self.workspace.name,
                date=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                workspace_root=self.workspace.parent,
            )
        except Exception as e:  # noqa: BLE001
            print(f"[betterness] cycle {cycle:03d}: failed (non-fatal): {e!r}")
            return None
        print(f"[betterness] cycle {cycle:03d}: {improvement_metric.verdict_line(row)}")
        return row

    def run_cycle(self, cycle: int) -> dict:
        merged = self._merge_reviewed(cycle)
        retro = self._run_retrospective(cycle)
        # Betterness is a cycle-end signal that depends on the snapshot the
        # retrospective phase produces, so it is skipped when that phase is off.
        better = self._record_betterness(cycle) if retro is not None else None
        return {"merged": merged, "retro": retro, "betterness": better}
