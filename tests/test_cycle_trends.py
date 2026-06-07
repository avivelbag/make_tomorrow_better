"""Tests for the cycle-trends CLI.

All fixtures live under ``tmp_path`` so the suite is deterministic and leaks
nothing outside the temp dir; no network or LLM calls are involved.
"""

from __future__ import annotations

import json

import pytest

from tools import cycle_trends as ct


def _worker(branch, status, commit):
    return {"branch": branch, "status": status, "commit": commit, "summary": "s"}


def _write_cycle(instance_dir, index, workers):
    cdir = instance_dir / "cycles" / f"{index:03d}"
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / "workers.json").write_text(json.dumps(workers))
    return cdir


def _seed_instance(workspace, name, cycles):
    inst = workspace / name
    for index, workers in cycles.items():
        _write_cycle(inst, index, workers)
    return inst


# --- compute_cycle_metrics ---------------------------------------------------


def test_metrics_counts_merged_and_blocked():
    workers = [
        _worker("a", "completed", "aaa"),
        _worker("b", "completed", "bbb"),
        _worker("c", "blocked", None),
        _worker("d", "completed", None),  # completed but no commit -> not merged
    ]
    m = ct.compute_cycle_metrics(workers)
    assert m == {"attempted": 4, "merged": 2, "blocked": 2, "merge_rate": 0.5}


def test_metrics_empty_cycle_no_divide_by_zero():
    m = ct.compute_cycle_metrics([])
    assert m == {"attempted": 0, "merged": 0, "blocked": 0, "merge_rate": 0.0}


def test_metrics_all_merged():
    workers = [_worker("a", "completed", "x"), _worker("b", "completed", "y")]
    assert ct.compute_cycle_metrics(workers)["merge_rate"] == 1.0


# --- compute_verdict ---------------------------------------------------------


def test_verdict_better_when_latest_above_baseline():
    v = ct.compute_verdict([0.2, 0.4, 0.9])
    assert v["verdict"] == "better"
    assert "better" in v["message"]


def test_verdict_worse_when_latest_below_baseline():
    v = ct.compute_verdict([0.8, 0.8, 0.2])
    assert v["verdict"] == "worse"


def test_verdict_flat_when_equal_to_baseline():
    v = ct.compute_verdict([0.5, 0.5, 0.5])
    assert v["verdict"] == "flat"
    assert v["baseline"] == pytest.approx(0.5)


def test_verdict_single_cycle_is_flat_with_message():
    v = ct.compute_verdict([0.7])
    assert v["verdict"] == "flat"
    assert v["baseline"] is None
    assert "not enough cycles" in v["message"]


def test_verdict_no_cycles_is_flat():
    v = ct.compute_verdict([])
    assert v["verdict"] == "flat"
    assert v["latest"] == 0.0


# --- load_cycles -------------------------------------------------------------


def test_load_cycles_sorted_numerically_and_skips_non_numeric(tmp_path):
    inst = _seed_instance(
        tmp_path,
        "make-tomorrow-better",
        {
            2: [_worker("a", "completed", "x")],
            10: [_worker("b", "blocked", None)],
            1: [_worker("c", "completed", "y"), _worker("d", "blocked", None)],
        },
    )
    # Decoy non-numeric dir and a cycle dir missing workers.json are ignored.
    (inst / "cycles" / "latest").mkdir()
    (inst / "cycles" / "003").mkdir()

    rows = ct.load_cycles(inst)
    assert [r["cycle"] for r in rows] == [1, 2, 10]
    assert rows[0]["merge_rate"] == 0.5


def test_load_cycles_missing_dir_returns_empty(tmp_path):
    assert ct.load_cycles(tmp_path / "nope") == []


def test_load_cycles_tolerates_malformed_workers_json(tmp_path):
    inst = tmp_path / "inst"
    cdir = inst / "cycles" / "001"
    cdir.mkdir(parents=True)
    (cdir / "workers.json").write_text("{not json")
    rows = ct.load_cycles(inst)
    assert rows == [{"cycle": 1, "attempted": 0, "merged": 0, "blocked": 0, "merge_rate": 0.0}]


# --- resolve_instance --------------------------------------------------------


def test_resolve_instance_defaults_to_only_instance(tmp_path):
    _seed_instance(tmp_path, "solo", {1: [_worker("a", "completed", "x")]})
    assert ct.resolve_instance(tmp_path, None) == "solo"


def test_resolve_instance_named(tmp_path):
    _seed_instance(tmp_path, "one", {1: [_worker("a", "completed", "x")]})
    _seed_instance(tmp_path, "two", {1: [_worker("b", "completed", "y")]})
    assert ct.resolve_instance(tmp_path, "two") == "two"


def test_resolve_instance_ambiguous_raises(tmp_path):
    _seed_instance(tmp_path, "one", {1: [_worker("a", "completed", "x")]})
    _seed_instance(tmp_path, "two", {1: [_worker("b", "completed", "y")]})
    with pytest.raises(ValueError, match="multiple instances"):
        ct.resolve_instance(tmp_path, None)


def test_resolve_instance_missing_named_raises(tmp_path):
    _seed_instance(tmp_path, "one", {1: [_worker("a", "completed", "x")]})
    with pytest.raises(ValueError, match="no cycle data"):
        ct.resolve_instance(tmp_path, "ghost")


def test_resolve_instance_none_available_raises(tmp_path):
    with pytest.raises(ValueError, match="no instances"):
        ct.resolve_instance(tmp_path, None)


# --- main / CLI --------------------------------------------------------------


def test_main_happy_path_prints_table_and_verdict(tmp_path, capsys):
    _seed_instance(
        tmp_path,
        "mtb",
        {
            1: [_worker("a", "completed", "x"), _worker("b", "blocked", None)],
            2: [_worker("c", "completed", "y"), _worker("d", "completed", "z")],
        },
    )
    rc = ct.main(["--workspace", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Cycle trends for instance: mtb" in out
    assert "merge_rate" in out
    assert "tomorrow is better" in out


def test_main_no_cycle_data_exits_nonzero(tmp_path, capsys):
    rc = ct.main(["--workspace", str(tmp_path)])
    err = capsys.readouterr().err
    assert rc == 2
    assert "no instances" in err


def test_main_named_instance_without_data_exits_nonzero(tmp_path, capsys):
    _seed_instance(tmp_path, "real", {1: [_worker("a", "completed", "x")]})
    rc = ct.main(["--workspace", str(tmp_path), "--instance", "ghost"])
    err = capsys.readouterr().err
    assert rc == 2
    assert "ghost" in err


def test_main_instance_with_empty_cycles_dir_exits_one(tmp_path, capsys):
    # cycles/ exists (so instance is discovered) but holds no parseable cycle.
    (tmp_path / "inst" / "cycles").mkdir(parents=True)
    rc = ct.main(["--workspace", str(tmp_path)])
    err = capsys.readouterr().err
    assert rc == 1
    assert "no cycle data found" in err
