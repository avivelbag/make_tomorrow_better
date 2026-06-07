"""Suggestion ranker.

Turns the per-cycle batch of ``workspace/suggestions/*.md`` into a priority
order written to ``workspace/ranked.json``. Before ordering, every entry is
annotated with a :mod:`src.scorer` ``historical_score`` — the mined merge-rate
of suggestions sharing its traits — and that score is surfaced in the ranking
prompt as an explicit tiebreaker. The deterministic ordering implemented here
(higher historical score first, filename as the final tiebreak) is also the
fallback used when no LLM ranker is wired in, keeping output reproducible.
"""

from __future__ import annotations

import json
from pathlib import Path

from src import scorer

TIEBREAK_INSTRUCTION = (
    "Each suggestion carries a historical_score in [0, 1]: the fraction of past "
    "suggestions sharing its traits that merged (0.5 means no history). Rank on "
    "merit first; use historical_score only to break ties between otherwise "
    "equally-strong suggestions, preferring the higher score."
)


def _read_text(path: Path) -> str:
    try:
        return path.read_text()
    except (OSError, UnicodeDecodeError):
        return ""


def load_entries(workspace: Path) -> list[dict]:
    """Read each suggestion file into a ``{file, title}`` entry."""
    suggestions_dir = Path(workspace) / "suggestions"
    entries: list[dict] = []
    if not suggestions_dir.is_dir():
        return entries
    for path in sorted(suggestions_dir.glob("*.md")):
        parsed = scorer.parse_suggestion(_read_text(path))
        entries.append({"file": path.name, "title": parsed["title"]})
    return entries


def build_prompt(entries: list[dict]) -> str:
    """Render the ranking prompt, exposing each entry's historical score."""
    lines = [TIEBREAK_INSTRUCTION, "", "Suggestions:"]
    for entry in entries:
        lines.append(
            f"- {entry['file']} (historical_score="
            f"{entry.get('historical_score', scorer.NEUTRAL_SCORE)}): "
            f"{entry.get('title', '')}".rstrip()
        )
    return "\n".join(lines)


def run(workspace: Path) -> list[dict]:
    """Score, order, and persist the suggestion ranking.

    Writes ``workspace/ranked.json`` as a list of
    ``{rank, file, reason, historical_score}`` ordered by descending
    historical score with the filename as a stable final tiebreak.
    """
    workspace = Path(workspace)
    entries = load_entries(workspace)
    scorer.score_entries(entries, workspace)

    ordered = sorted(
        entries, key=lambda e: (-e["historical_score"], e["file"])
    )
    ranked = [
        {
            "rank": i,
            "file": e["file"],
            "reason": (
                f"historical_score={e['historical_score']} "
                "(outcome-based tiebreaker)"
            ),
            "historical_score": e["historical_score"],
        }
        for i, e in enumerate(ordered, 1)
    ]

    (workspace / "ranked.json").write_text(json.dumps(ranked, indent=2))
    return ranked
