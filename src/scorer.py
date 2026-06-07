"""Outcome-based suggestion scorer.

Gives the ranker a memory it otherwise lacks. Past cycles are mined from
``workspace/cycles/<NNN>/`` — each archived ``workers.json`` says which branch
merged, and each archived ``suggestions/*.md`` is the text that produced it.
We extract a few cheap, deterministic *traits* from every suggestion (title
keywords, whether it carried acceptance bullets, a coarse size bucket) and
tally, per trait, how often suggestions carrying it went on to merge.

The resulting trait -> (merge_count, total_count) table is persisted to
``workspace/_shared/suggestion_outcomes.json`` so it accumulates across cycles.
Scoring a fresh suggestion is then the mean merge-rate of its traits, falling
back to ``0.5`` (neutral) whenever there is no relevant history — so a cold
start neither helps nor hurts a suggestion. No embeddings, no LLM: the signal
is pure-Python and unit-testable.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

NEUTRAL_SCORE = 0.5

OUTCOMES_RELPATH = Path("_shared") / "suggestion_outcomes.json"

# Common English / domain filler that carries no ranking signal.
_STOPWORDS = frozenset(
    {
        "the", "and", "for", "with", "from", "into", "that", "this", "are",
        "use", "using", "add", "adds", "new", "via", "per", "all", "its",
        "their", "when", "each", "over", "out", "not", "but", "any", "can",
    }
)

_WORD_RE = re.compile(r"[a-z][a-z0-9]{2,}")

_LEADING_INDEX_RE = re.compile(r"^\d+[-_]")


def _read_text(path: Path) -> str:
    try:
        return path.read_text()
    except (OSError, UnicodeDecodeError):
        return ""


def split_frontmatter(text: str) -> tuple[str, str]:
    """Return ``(frontmatter, body)`` for a ``---`` delimited suggestion.

    Tolerates a missing or malformed header by returning an empty
    frontmatter and the whole text as the body.
    """
    if not text.startswith("---"):
        return "", text
    end = text.find("\n---", 3)
    if end < 0:
        return "", text
    frontmatter = text[3:end]
    body_start = text.find("\n", end + 1)
    body = text[body_start + 1 :] if body_start >= 0 else ""
    return frontmatter, body


def parse_suggestion(text: str) -> dict:
    """Parse a suggestion into ``{title, has_acceptance, body}``.

    ``has_acceptance`` is true only when the frontmatter declares an
    ``acceptance:`` key followed by at least one ``-`` bullet.
    """
    frontmatter, body = split_frontmatter(text)
    title = ""
    has_acceptance = False
    in_acceptance = False
    for raw in frontmatter.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if in_acceptance:
            if stripped.startswith("-"):
                has_acceptance = True
                continue
            # A new top-level key ends the acceptance block.
            if re.match(r"^\S.*:", stripped):
                in_acceptance = False
        if stripped.lower().startswith("acceptance:"):
            in_acceptance = True
            rest = stripped.split(":", 1)[1].strip()
            if rest and rest != "[]":
                has_acceptance = True
            continue
        if not title and stripped.lower().startswith("title:"):
            title = stripped.split(":", 1)[1].strip().strip("'\"")
    return {"title": title, "has_acceptance": has_acceptance, "body": body}


def _size_bucket(body: str) -> str:
    n = sum(1 for line in body.splitlines() if line.strip())
    if n < 20:
        return "size:small"
    if n < 60:
        return "size:medium"
    return "size:large"


def extract_traits(text: str) -> set[str]:
    """Derive the deterministic trait set used for scoring a suggestion.

    Traits are title keywords (``kw:<word>``), an acceptance-presence flag,
    and a coarse body-size bucket. Keeping the vocabulary small keeps each
    trait's sample size meaningful with only a handful of past cycles.
    """
    parsed = parse_suggestion(text)
    traits: set[str] = set()
    for word in _WORD_RE.findall(parsed["title"].lower()):
        if word not in _STOPWORDS:
            traits.add(f"kw:{word}")
    traits.add("has_acceptance" if parsed["has_acceptance"] else "no_acceptance")
    traits.add(_size_bucket(parsed["body"]))
    return traits


def _slug_of(stem: str) -> str:
    return _LEADING_INDEX_RE.sub("", stem)


def _branch_merged(worker: dict) -> bool:
    return str(worker.get("status") or "") == "completed" and bool(worker.get("commit"))


def _match_branch(stem: str, branches: dict[str, dict]) -> dict | None:
    """Find the worker record for a suggestion file stem.

    Branches are named ``swarm/<cycle>-<NN>-<slug>`` while suggestion files
    are ``<NN>-<slug>.md``; match on the full stem first, then on the bare
    slug so a differing index prefix still links.
    """
    slug = _slug_of(stem)
    for branch, worker in branches.items():
        if branch.endswith(stem):
            return worker
    for branch, worker in branches.items():
        if slug and slug in branch:
            return worker
    return None


def build_outcome_table(cycles_dir: Path) -> dict[str, list[int]]:
    """Tally trait -> ``[merge_count, total_count]`` across archived cycles.

    Each ``cycles/<NNN>/`` is expected to hold a ``workers.json`` and a
    ``suggestions/`` dir; missing or malformed pieces are skipped rather than
    raising, so a partially-written archive never breaks ranking.
    """
    table: dict[str, list[int]] = {}
    cycles_dir = Path(cycles_dir)
    if not cycles_dir.is_dir():
        return table

    for cycle_dir in sorted(cycles_dir.iterdir()):
        suggestions_dir = cycle_dir / "suggestions"
        if not suggestions_dir.is_dir():
            continue
        branches = _load_workers(cycle_dir / "workers.json")
        for sug in sorted(suggestions_dir.glob("*.md")):
            worker = _match_branch(sug.stem, branches)
            if worker is None:
                continue
            merged = 1 if _branch_merged(worker) else 0
            for trait in extract_traits(_read_text(sug)):
                entry = table.setdefault(trait, [0, 0])
                entry[0] += merged
                entry[1] += 1
    return table


def _load_workers(workers_json: Path) -> dict[str, dict]:
    text = _read_text(workers_json)
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, list):
        return {}
    out: dict[str, dict] = {}
    for w in data:
        if isinstance(w, dict) and w.get("branch"):
            out[str(w["branch"])] = w
    return out


def score_traits(traits: set[str], table: dict[str, list[int]]) -> float:
    """Mean historical merge-rate of the traits present in ``table``.

    Returns ``NEUTRAL_SCORE`` when none of the traits have any history, so an
    unseen suggestion is neither rewarded nor penalised.
    """
    rates: list[float] = []
    for trait in traits:
        entry = table.get(trait)
        if entry and entry[1] > 0:
            rates.append(entry[0] / entry[1])
    if not rates:
        return NEUTRAL_SCORE
    return sum(rates) / len(rates)


def score_suggestion(text: str, table: dict[str, list[int]]) -> float:
    return score_traits(extract_traits(text), table)


def build_table(workspace: Path, *, persist: bool = True) -> dict[str, list[int]]:
    """Rebuild the outcome table from all archived cycles and cache it.

    Rebuilding from the full ``cycles/`` history each call is what makes the
    persisted file accumulate — every past cycle is re-counted, so the table
    only grows as more cycles are archived.
    """
    workspace = Path(workspace)
    table = build_outcome_table(workspace / "cycles")
    if persist:
        out_path = workspace / OUTCOMES_RELPATH
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(table, indent=2, sort_keys=True))
    return table


def score_entries(entries: list[dict], workspace: Path, *, table: dict | None = None) -> list[dict]:
    """Append a ``historical_score`` to each ranked-input entry in place.

    ``entries`` are ranker inputs of the form ``{"file": "<path>", ...}``; the
    referenced suggestion is read relative to ``workspace/suggestions`` when the
    path is not already absolute. Entries are returned for convenience.
    """
    workspace = Path(workspace)
    if table is None:
        table = build_table(workspace, persist=False)
    suggestions_dir = workspace / "suggestions"
    for entry in entries:
        rel = entry.get("file", "")
        path = Path(rel)
        if not path.is_absolute():
            path = suggestions_dir / path.name
        entry["historical_score"] = round(score_suggestion(_read_text(path), table), 4)
    return entries
