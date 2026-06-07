"""Cycle orchestrator.

Drives a swarm cycle: merge the reviewed branches, then run the post-cycle
retrospective and print its summary. Only the retrospective phase is wired up
here; the merge phase is a thin seam other cycle work hangs off of.
"""

from __future__ import annotations

from pathlib import Path

from src import retrospective


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

    def run_cycle(self, cycle: int) -> dict:
        merged = self._merge_reviewed(cycle)
        retro = self._run_retrospective(cycle)
        return {"merged": merged, "retro": retro}
