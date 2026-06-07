"""Tests for the outcome-based suggestion scorer and ranker.

All artifacts live under ``tmp_path`` so the suite is deterministic and leaks
nothing; no network, no LLM, no clock dependence.
"""

from __future__ import annotations

import json

from src import ranker, scorer


def _suggestion(title, acceptance=True, body_lines=5):
    accept = "\n".join(f"  - crit {i}" for i in range(3)) if acceptance else ""
    accept_block = f"acceptance:\n{accept}\n" if acceptance else "acceptance: []\n"
    body = "\n".join(f"line {i}" for i in range(body_lines))
    return f"---\ntitle: {title}\nrationale: because\n{accept_block}---\n\n{body}\n"


def _seed_cycle(workspace, cycle, suggestions, workers):
    cdir = workspace / "cycles" / f"{cycle:03d}"
    sdir = cdir / "suggestions"
    sdir.mkdir(parents=True)
    for name, text in suggestions.items():
        (sdir / name).write_text(text)
    (cdir / "workers.json").write_text(json.dumps(workers))
    return cdir


def test_extract_traits_keywords_acceptance_and_size():
    traits = scorer.extract_traits(_suggestion("Add test harness", body_lines=5))
    assert "kw:test" in traits
    assert "kw:harness" in traits
    assert "has_acceptance" in traits
    assert "size:small" in traits
    # Stopwords are dropped.
    assert "kw:add" not in traits


def test_size_buckets_scale_with_body_length():
    small = scorer.extract_traits(_suggestion("x", body_lines=5))
    medium = scorer.extract_traits(_suggestion("x", body_lines=30))
    large = scorer.extract_traits(_suggestion("x", body_lines=100))
    assert "size:small" in small
    assert "size:medium" in medium
    assert "size:large" in large


def test_missing_acceptance_yields_no_acceptance_trait():
    traits = scorer.extract_traits(_suggestion("plain idea", acceptance=False))
    assert "no_acceptance" in traits
    assert "has_acceptance" not in traits


def test_neutral_score_when_no_history():
    # Empty table -> every suggestion is neutral.
    assert scorer.score_suggestion(_suggestion("test thing"), {}) == scorer.NEUTRAL_SCORE


def test_build_table_links_suggestions_to_merged_branches(tmp_path):
    workspace = tmp_path / "ws"
    _seed_cycle(
        workspace,
        1,
        {
            "01-test-runner.md": _suggestion("Add test runner"),
            "02-docs-cleanup.md": _suggestion("Cleanup docs"),
        },
        [
            {"branch": "swarm/01-01-test-runner", "status": "completed", "commit": "aaa"},
            {"branch": "swarm/01-02-docs-cleanup", "status": "blocked", "commit": None},
        ],
    )

    table = scorer.build_table(workspace)

    # The merged suggestion's 'test' keyword merged 1/1; the blocked one 0/1.
    assert table["kw:test"] == [1, 1]
    assert table["kw:docs"] == [0, 1]

    # Persisted for accumulation across cycles.
    persisted = json.loads((workspace / scorer.OUTCOMES_RELPATH).read_text())
    assert persisted["kw:test"] == [1, 1]


def test_score_reflects_learned_merge_rate(tmp_path):
    workspace = tmp_path / "ws"
    _seed_cycle(
        workspace,
        1,
        {"01-test-runner.md": _suggestion("Add test runner")},
        [{"branch": "swarm/01-01-test-runner", "status": "completed", "commit": "aaa"}],
    )
    table = scorer.build_table(workspace)
    # A new suggestion sharing the proven 'test' trait scores above neutral.
    assert scorer.score_suggestion(_suggestion("test something"), table) > scorer.NEUTRAL_SCORE


def test_build_table_tolerates_malformed_and_missing(tmp_path):
    workspace = tmp_path / "ws"
    # No cycles dir at all.
    assert scorer.build_table(workspace, persist=False) == {}

    cdir = workspace / "cycles" / "001"
    (cdir / "suggestions").mkdir(parents=True)
    (cdir / "suggestions" / "01-x.md").write_text(_suggestion("x test"))
    (cdir / "workers.json").write_text("{ not valid json")
    # Malformed workers.json -> no links, empty table, no crash.
    assert scorer.build_table(workspace, persist=False) == {}


def test_score_entries_appends_field(tmp_path):
    workspace = tmp_path / "ws"
    sdir = workspace / "suggestions"
    sdir.mkdir(parents=True)
    (sdir / "01-a.md").write_text(_suggestion("alpha"))
    entries = [{"file": "01-a.md"}]
    scorer.score_entries(entries, workspace, table={})
    assert entries[0]["historical_score"] == scorer.NEUTRAL_SCORE


def test_ranker_writes_historical_score_to_ranked_json(tmp_path):
    workspace = tmp_path / "ws"
    sdir = workspace / "suggestions"
    sdir.mkdir(parents=True)
    (sdir / "01-a.md").write_text(_suggestion("alpha test"))
    (sdir / "02-b.md").write_text(_suggestion("beta docs"))

    ranked = ranker.run(workspace)

    on_disk = json.loads((workspace / "ranked.json").read_text())
    assert on_disk == ranked
    assert len(ranked) == 2
    for entry in ranked:
        assert "historical_score" in entry
        assert 0.0 <= entry["historical_score"] <= 1.0
    assert [e["rank"] for e in ranked] == [1, 2]


def test_ranker_orders_by_historical_score(tmp_path):
    workspace = tmp_path / "ws"
    # One past cycle teaches that 'test' merges and 'flaky' does not.
    _seed_cycle(
        workspace,
        1,
        {
            "01-test.md": _suggestion("Add test"),
            "02-flaky.md": _suggestion("flaky thing"),
        },
        [
            {"branch": "swarm/01-01-test", "status": "completed", "commit": "aaa"},
            {"branch": "swarm/01-02-flaky", "status": "blocked", "commit": None},
        ],
    )
    sdir = workspace / "suggestions"
    sdir.mkdir(parents=True)
    (sdir / "01-proven.md").write_text(_suggestion("test again"))
    (sdir / "02-risky.md").write_text(_suggestion("flaky again"))

    ranked = ranker.run(workspace)
    assert ranked[0]["file"] == "01-proven.md"
    assert ranked[0]["historical_score"] > ranked[1]["historical_score"]


def test_build_prompt_mentions_tiebreaker_and_scores():
    entries = [{"file": "01-a.md", "title": "alpha", "historical_score": 0.75}]
    prompt = ranker.build_prompt(entries)
    assert "tie" in prompt.lower()
    assert "historical_score=0.75" in prompt
