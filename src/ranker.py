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
import re
import subprocess
from pathlib import Path

from src import scorer

TIEBREAK_INSTRUCTION = (
    "Each suggestion carries a historical_score in [0, 1]: the fraction of past "
    "suggestions sharing its traits that merged (0.5 means no history). Rank on "
    "merit first; use historical_score only to break ties between otherwise "
    "equally-strong suggestions, preferring the higher score."
)

SWARM_PREFIX = "swarm/"

# Above this Jaccard token overlap a suggestion is treated as a likely
# duplicate of an already-merged branch and demoted to the bottom of the rank.
DUP_THRESHOLD = 0.6

_SLUG_SPLIT_RE = re.compile(r"[^a-z0-9]+")

# Tokens that carry no dedup signal: the cycle/index numbers baked into branch
# names and the per-suggestion file index, plus generic filler.
_DEDUP_STOPWORDS = frozenset(
    {
        "the", "and", "for", "with", "from", "into", "that", "this", "are",
        "use", "using", "add", "adds", "new", "via", "per", "all", "its",
        "their", "when", "each", "over", "out", "not", "but", "any", "can",
    }
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


def slug_tokens(text: str) -> set[str]:
    """Split free text or a slug into a comparable token set.

    Lowercases, splits on any run of non-alphanumerics, then drops short
    tokens (the bare ``02``/``05`` index numbers in branch names land here)
    and generic stopwords so only content words remain.
    """
    return {
        tok
        for tok in _SLUG_SPLIT_RE.split(text.lower())
        if len(tok) >= 3 and tok not in _DEDUP_STOPWORDS
    }


def jaccard(a: set[str], b: set[str]) -> float:
    """Jaccard similarity of two token sets; ``0.0`` if either is empty."""
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def best_duplicate(text: str, merged_slugs: list[str]) -> tuple[float, str | None]:
    """Best slug-token similarity of ``text`` against any merged slug.

    Returns ``(score, slug)`` for the closest merged slug, or ``(0.0, None)``
    when there are no merged slugs to compare against. Pure: no git, no I/O,
    so the dedup decision can be unit-tested with an injected slug list.
    """
    tokens = slug_tokens(text)
    best_score = 0.0
    best_slug: str | None = None
    for slug in merged_slugs:
        score = jaccard(tokens, slug_tokens(slug))
        if score > best_score:
            best_score, best_slug = score, slug
    return best_score, best_slug


def merged_swarm_slugs(base_branch: str, *, cwd: Path | None = None) -> list[str]:
    """List slugs of ``swarm/*`` branches already merged into ``base_branch``.

    Isolates the only git dependency in this module: shells out to
    ``git branch --merged``, keeps the ``swarm/``-prefixed branches, and strips
    that prefix. Any git failure (not a repo, bad ref) yields an empty list so
    dedup degrades to a harmless no-op rather than breaking ranking.
    """
    try:
        out = subprocess.run(
            ["git", "branch", "--merged", base_branch],
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return []
    slugs: list[str] = []
    for line in out.splitlines():
        name = line.replace("*", "", 1).strip()
        if name.startswith(SWARM_PREFIX):
            slugs.append(name[len(SWARM_PREFIX) :])
    return slugs


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


def demote_duplicates(
    ordered: list[dict],
    merged_slugs: list[str],
    *,
    threshold: float = DUP_THRESHOLD,
) -> list[dict]:
    """Stably push likely-duplicate entries to the bottom of the ranking.

    Each entry is compared (file slug + title) against ``merged_slugs``. Those
    above ``threshold`` are moved to the end, preserving relative order within
    both the kept and demoted groups, and annotated with a dedup ``reason`` and
    a ``duplicate_of`` slug. Non-duplicates keep their incoming order untouched,
    so this never reshuffles among net-new suggestions. With an empty
    ``merged_slugs`` it is a pure no-op pass-through (ranks reassigned only).
    """
    kept: list[dict] = []
    demoted: list[dict] = []
    for entry in ordered:
        text = f"{entry.get('file', '')} {entry.get('title', '')}"
        score, slug = best_duplicate(text, merged_slugs)
        if score >= threshold and slug is not None:
            demoted.append(
                {
                    **entry,
                    "duplicate_of": slug,
                    "_reason": (
                        f"likely duplicate of merged branch '{slug}' "
                        f"(slug similarity {round(score, 2)}); demoted"
                    ),
                }
            )
        else:
            kept.append(entry)
    return kept + demoted


def run(
    workspace: Path,
    *,
    base_branch: str = "main",
    repo: Path | None = None,
) -> list[dict]:
    """Score, order, dedup, and persist the suggestion ranking.

    Orders by descending historical score (filename as stable tiebreak), then
    demotes suggestions that closely duplicate an already-merged ``swarm/*``
    branch to the bottom — keeping them visible rather than dropping them.
    ``repo`` overrides the git working directory for the merged-branch lookup
    (defaults to ``workspace``). Writes ``workspace/ranked.json`` as a list of
    ``{rank, file, reason, historical_score}`` (duplicates also carry
    ``duplicate_of``).
    """
    workspace = Path(workspace)
    entries = load_entries(workspace)
    scorer.score_entries(entries, workspace)

    ordered = sorted(
        entries, key=lambda e: (-e["historical_score"], e["file"])
    )
    merged_slugs = merged_swarm_slugs(base_branch, cwd=repo or workspace)
    ordered = demote_duplicates(ordered, merged_slugs)

    ranked = [
        {
            "rank": i,
            "file": e["file"],
            "reason": e.get(
                "_reason",
                f"historical_score={e['historical_score']} "
                "(outcome-based tiebreaker)",
            ),
            "historical_score": e["historical_score"],
            **({"duplicate_of": e["duplicate_of"]} if "duplicate_of" in e else {}),
        }
        for i, e in enumerate(ordered, 1)
    ]

    (workspace / "ranked.json").write_text(json.dumps(ranked, indent=2))
    return ranked
