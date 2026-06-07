"""Tests for the daily betterness metric (src/improvement_metric.py)."""

from __future__ import annotations

import json

import pytest

from src import improvement_metric as im


def _write_cycle(instance_dir, cycle, branches):
    """Seed one cycle snapshot.

    ``branches`` is a list of ``(branch, summary, verdict)`` tuples; each
    produces one worker entry in ``workers.json`` and one matching review file
    (frontmatter branch == worker branch, mirroring real snapshots).
    """
    cdir = instance_dir / "cycles" / cycle
    reviews = cdir / "reviews"
    reviews.mkdir(parents=True, exist_ok=True)
    workers = [
        {"branch": b, "status": "completed", "commit": "c", "summary": s}
        for b, s, _ in branches
    ]
    (cdir / "workers.json").write_text(json.dumps(workers))
    for b, _, verdict in branches:
        slug = b.replace("/", "-")
        (reviews / f"{slug}.md").write_text(
            f"---\nbranch: {b}\nverdict: {verdict}\n---\n\nbody\n"
        )


def test_empty_history_returns_neutral_baseline(tmp_path):
    metrics = im.compute_daily_score("inst", workspace_root=tmp_path)
    assert metrics == {
        "merge_rate": 0.0,
        "branches_merged": 0,
        "tests_added": 0,
        "score": im.NEUTRAL_BASELINE,
    }


def test_compute_score_aggregates_cycles(tmp_path):
    inst = tmp_path / "inst"
    _write_cycle(
        inst,
        "001",
        [
            ("swarm/01-a", "did a thing, plus 12 unit tests; suite passes", "approve"),
            ("swarm/01-b", "no test count mentioned here", "reject"),
        ],
    )
    metrics = im.compute_daily_score("inst", workspace_root=tmp_path)
    assert metrics["branches_merged"] == 1
    assert metrics["merge_rate"] == pytest.approx(0.5)
    assert metrics["tests_added"] == 12
    expected = (
        0.5 * im._W_MERGE_RATE
        + (12 / im._TESTS_NORM) * im._W_TESTS
        + (1 / im._BRANCHES_NORM) * im._W_BRANCHES
    )
    assert metrics["score"] == pytest.approx(expected)


def test_multiple_cycles_sum(tmp_path):
    inst = tmp_path / "inst"
    _write_cycle(inst, "001", [("swarm/01-a", "5 tests", "approve")])
    _write_cycle(inst, "002", [("swarm/02-b", "7 tests", "approve")])
    metrics = im.compute_daily_score("inst", workspace_root=tmp_path)
    assert metrics["branches_merged"] == 2
    assert metrics["tests_added"] == 12
    assert metrics["merge_rate"] == pytest.approx(1.0)


def test_normalization_caps_terms_at_one(tmp_path):
    inst = tmp_path / "inst"
    branches = [(f"swarm/01-{i}", "added 200 deterministic tests", "approve") for i in range(20)]
    _write_cycle(inst, "001", branches)
    metrics = im.compute_daily_score("inst", workspace_root=tmp_path)
    assert metrics["merge_rate"] == pytest.approx(1.0)
    assert metrics["score"] == pytest.approx(1.0)


def test_malformed_workers_json_is_tolerated(tmp_path):
    inst = tmp_path / "inst"
    cdir = inst / "cycles" / "001"
    (cdir / "reviews").mkdir(parents=True, exist_ok=True)
    (cdir / "workers.json").write_text("{not valid json")
    metrics = im.compute_daily_score("inst", workspace_root=tmp_path)
    assert metrics["branches_merged"] == 0
    assert metrics["merge_rate"] == 0.0
    assert metrics["score"] == pytest.approx(0.0)


def test_record_first_day_has_null_delta(tmp_path):
    inst = tmp_path / "inst"
    _write_cycle(inst, "001", [("swarm/01-a", "5 tests", "approve")])
    row = im.record_daily_score("inst", date="2026-06-06", workspace_root=tmp_path)
    assert row["date"] == "2026-06-06"
    assert row["delta_vs_yesterday"] is None
    path = inst / "betterness.jsonl"
    assert path.is_file()
    assert len(path.read_text().splitlines()) == 1


def test_record_two_days_produces_correct_delta(tmp_path):
    inst = tmp_path / "inst"
    _write_cycle(inst, "001", [("swarm/01-a", "5 tests", "reject")])
    day1 = im.record_daily_score("inst", date="2026-06-06", workspace_root=tmp_path)

    _write_cycle(inst, "002", [("swarm/02-b", "5 tests", "approve")])
    day2 = im.record_daily_score("inst", date="2026-06-07", workspace_root=tmp_path)

    assert day2["delta_vs_yesterday"] == pytest.approx(day2["score"] - day1["score"])
    assert day2["delta_vs_yesterday"] > 0
    rows = [json.loads(line) for line in (inst / "betterness.jsonl").read_text().splitlines()]
    assert [r["date"] for r in rows] == ["2026-06-06", "2026-06-07"]


def test_same_day_rerun_overwrites_not_duplicates(tmp_path):
    inst = tmp_path / "inst"
    _write_cycle(inst, "001", [("swarm/01-a", "5 tests", "reject")])
    im.record_daily_score("inst", date="2026-06-06", workspace_root=tmp_path)

    # More work lands the same day, then the metric re-runs for that date.
    _write_cycle(inst, "002", [("swarm/02-b", "5 tests", "approve")])
    second = im.record_daily_score("inst", date="2026-06-06", workspace_root=tmp_path)

    rows = [json.loads(line) for line in (inst / "betterness.jsonl").read_text().splitlines()]
    assert len(rows) == 1
    assert rows[0]["branches_merged"] == 1
    assert rows[0]["score"] == pytest.approx(second["score"])
    assert second["delta_vs_yesterday"] is None


def test_same_day_rerun_keeps_delta_against_prior_day(tmp_path):
    inst = tmp_path / "inst"
    _write_cycle(inst, "001", [("swarm/01-a", "5 tests", "reject")])
    im.record_daily_score("inst", date="2026-06-06", workspace_root=tmp_path)
    _write_cycle(inst, "002", [("swarm/02-b", "5 tests", "approve")])
    im.record_daily_score("inst", date="2026-06-07", workspace_root=tmp_path)

    rerun = im.record_daily_score("inst", date="2026-06-07", workspace_root=tmp_path)
    assert rerun["delta_vs_yesterday"] is not None
    rows = (inst / "betterness.jsonl").read_text().splitlines()
    assert len(rows) == 2


def test_verdict_line_better_worse_and_baseline():
    base = im.verdict_line({"score": 0.4, "delta_vs_yesterday": None})
    assert "no prior day" in base
    better = im.verdict_line({"score": 0.6, "delta_vs_yesterday": 0.2})
    assert "better" in better and "0.600" in better and "0.400" in better
    worse = im.verdict_line({"score": 0.3, "delta_vs_yesterday": -0.1})
    assert "worse" in worse
