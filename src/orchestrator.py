"""Cycle orchestrator.

Drives a swarm cycle: refresh the review-lessons file, merge the reviewed
branches, then run the post-cycle retrospective and print its summary. Only the
lessons and retrospective phases are wired up here; the merge phase is a thin
seam other cycle work hangs off of.
"""

from __future__ import annotations

from pathlib import Path

from src import lessons, retrospective


class Orchestrator:
    def __init__(self, workspace: Path, config: dict | None = None):
        self.workspace = Path(workspace)
        self.config = config or {}

    def _refresh_lessons(self) -> Path | None:
        """Rebuild ``_lessons.md`` from accumulated reviews before workers run.

        Refreshing at the top of the cycle means the lessons distilled from
        every prior cycle's ``request-changes`` / ``reject`` reviews are current
        when the next batch of workers is prompted. Returns the written path, or
        ``None`` when lessons are disabled in config.
        """
        if not self.config.get("lessons_enabled", True):
            return None
        return lessons.write_lessons(self.workspace)

    def _merge_reviewed(self, cycle: int) -> dict:
        return {}

    def _run_retrospective(self, cycle: int) -> dict | None:
        if not self.config.get("retrospective_enabled", True):
            return None
        result = retrospective.run(workspace=self.workspace, cycle=cycle)
        print(result["summary"])
        return result

    def run_cycle(self, cycle: int) -> dict:
        lessons_path = self._refresh_lessons()
        merged = self._merge_reviewed(cycle)
        retro = self._run_retrospective(cycle)
        return {"merged": merged, "retro": retro, "lessons": lessons_path}
