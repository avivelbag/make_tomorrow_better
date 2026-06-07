"""Tests for the merged-branch dedup pass in :mod:`src.ranker`.

All artifacts live under ``tmp_path``; the only git interaction is exercised in
a single real-repo test, while the dedup decision itself is covered through the
pure helpers with injected slug lists (no git, no clock, no network).
"""

from __future__ import annotations

import json
import subprocess

from src import ranker


def _suggestion(title, acceptance=True, body_lines=5):
    accept = "\n".join(f"  - crit {i}" for i in range(3)) if acceptance else ""
    accept_block = f"acceptance:\n{accept}\n" if acceptance else "acceptance: []\n"
    body = "\n".join(f"line {i}" for i in range(body_lines))
    return f"---\ntitle: {title}\nrationale: because\n{accept_block}---\n\n{body}\n"


def _write_suggestions(workspace, files):
    sdir = workspace / "suggestions"
    sdir.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (sdir / name).write_text(text)


# --- pure similarity helpers -------------------------------------------------


def test_slug_tokens_drops_index_numbers_and_stopwords():
    assert ranker.slug_tokens("02-05-add-the-cache-layer") == {"cache", "layer"}


def test_jaccard_basic():
    assert ranker.jaccard({"a", "b"}, {"a", "b"}) == 1.0
    assert ranker.jaccard({"a", "b"}, {"b", "c"}) == 1 / 3
    assert ranker.jaccard(set(), {"a"}) == 0.0


def test_best_duplicate_picks_closest_slug():
    merged = ["03-add-cache-layer", "07-rewrite-logging"]
    score, slug = ranker.best_duplicate("05-cache-layer-improvements", merged)
    assert slug == "03-add-cache-layer"
    assert score > 0.5


def test_best_duplicate_empty_merged_is_zero():
    assert ranker.best_duplicate("anything-here", []) == (0.0, None)


# --- demote_duplicates -------------------------------------------------------


def _entry(file, title, score=0.5):
    return {"file": file, "title": title, "historical_score": score}


def test_demote_pushes_duplicate_last_with_reason():
    ordered = [
        _entry("01-cache-layer.md", "Cache layer"),
        _entry("02-fresh-idea.md", "Fresh idea"),
    ]
    out = ranker.demote_duplicates(ordered, ["09-cache-layer"], threshold=0.6)
    assert out[-1]["file"] == "01-cache-layer.md"
    assert out[-1]["duplicate_of"] == "09-cache-layer"
    assert "likely duplicate" in out[-1]["_reason"]
    assert "_reason" not in out[0]


def test_demote_keeps_novel_order_when_no_match():
    ordered = [_entry("01-a.md", "Alpha"), _entry("02-b.md", "Beta")]
    out = ranker.demote_duplicates(ordered, ["99-unrelated-thing"], threshold=0.6)
    assert [e["file"] for e in out] == ["01-a.md", "02-b.md"]
    assert all("duplicate_of" not in e for e in out)


def test_demote_empty_merged_is_noop():
    ordered = [_entry("01-a.md", "Alpha"), _entry("02-b.md", "Beta")]
    out = ranker.demote_duplicates(ordered, [], threshold=0.6)
    assert out == ordered


def test_demote_preserves_relative_order_within_groups():
    ordered = [
        _entry("01-dup-one.md", "Dup one"),
        _entry("02-novel.md", "Novel"),
        _entry("03-dup-two.md", "Dup two"),
    ]
    merged = ["dup-one", "dup-two"]
    out = ranker.demote_duplicates(ordered, merged, threshold=0.6)
    assert [e["file"] for e in out] == [
        "02-novel.md",
        "01-dup-one.md",
        "03-dup-two.md",
    ]


# --- run() integration -------------------------------------------------------


def test_run_demotes_duplicate_and_ranks_last(tmp_path, monkeypatch):
    _write_suggestions(
        tmp_path,
        {
            "01-cache-layer.md": _suggestion("Cache layer"),
            "02-brand-new-feature.md": _suggestion("Brand new feature"),
        },
    )
    monkeypatch.setattr(
        ranker, "merged_swarm_slugs", lambda *a, **k: ["07-cache-layer"]
    )
    ranked = ranker.run(tmp_path)

    last = ranked[-1]
    assert last["file"] == "01-cache-layer.md"
    assert last["duplicate_of"] == "07-cache-layer"
    assert "likely duplicate" in last["reason"]
    assert ranked[0]["file"] == "02-brand-new-feature.md"
    assert "historical_score" in ranked[0]["reason"]

    on_disk = json.loads((tmp_path / "ranked.json").read_text())
    assert on_disk == ranked
    assert [e["rank"] for e in on_disk] == [1, 2]


def test_run_no_merged_branches_is_noop(tmp_path, monkeypatch):
    _write_suggestions(
        tmp_path,
        {"01-alpha.md": _suggestion("Alpha"), "02-beta.md": _suggestion("Beta")},
    )
    monkeypatch.setattr(ranker, "merged_swarm_slugs", lambda *a, **k: [])
    ranked = ranker.run(tmp_path)
    assert all("duplicate_of" not in e for e in ranked)
    assert [e["file"] for e in ranked] == ["01-alpha.md", "02-beta.md"]


# --- merged_swarm_slugs (git boundary) ---------------------------------------


def test_merged_swarm_slugs_filters_and_strips_prefix(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args):
        subprocess.run(
            ["git", *args], cwd=repo, check=True, capture_output=True, text=True
        )

    git("init", "-b", "main")
    git("config", "user.email", "t@t.t")
    git("config", "user.name", "t")
    (repo / "f.txt").write_text("x")
    git("add", ".")
    git("commit", "-m", "init")
    git("branch", "swarm/01-02-merged-feature")
    git("branch", "plain-branch")

    slugs = ranker.merged_swarm_slugs("main", cwd=repo)
    assert "01-02-merged-feature" in slugs
    assert all(not s.startswith("swarm/") for s in slugs)
    assert "plain-branch" not in slugs


def test_merged_swarm_slugs_non_repo_returns_empty(tmp_path):
    assert ranker.merged_swarm_slugs("main", cwd=tmp_path) == []
