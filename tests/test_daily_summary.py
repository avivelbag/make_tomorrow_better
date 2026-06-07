"""Tests for the daily git-activity summary CLI.

All git and ``claude -p`` calls are injected as fake runners, so the suite is
fully deterministic, offline, and leaks no files outside ``tmp_path``.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from tools import daily_summary as ds

_RS = "\x1e"
_FS = "\x1f"


def _record(commit_hash, author, date, subject, numstat_lines):
    header = _RS + _FS.join([commit_hash, author, date, subject])
    return "\n".join([header, *numstat_lines])


def _log(*records):
    return "".join(records)


def test_default_since_is_midnight():
    now = datetime(2026, 6, 6, 14, 37, 22)
    assert ds.default_since(now) == "2026-06-06T00:00:00"


def test_resolve_since_passthrough():
    now = datetime(2026, 6, 6, 14, 0, 0)
    assert ds.resolve_since("2 days ago", now) == "2 days ago"


@pytest.mark.parametrize("value", [None, "", "   "])
def test_resolve_since_blank_falls_back_to_midnight(value):
    now = datetime(2026, 6, 6, 9, 15, 0)
    assert ds.resolve_since(value, now) == "2026-06-06T00:00:00"


def test_parse_git_log_happy_path():
    raw = _log(
        _record("a" * 40, "Dev", "Mon", "add feature", ["10\t2\tsrc/a.py", "3\t0\tsrc/b.py"]),
        _record("b" * 40, "Dev", "Mon", "fix bug", ["1\t1\tsrc/a.py"]),
    )
    result = ds.parse_git_log(raw)
    assert len(result["commits"]) == 2
    assert result["commits"][0]["subject"] == "add feature"
    # src/a.py appears twice but counts once toward unique files changed.
    assert result["files_changed"] == 2
    assert result["insertions"] == 14
    assert result["deletions"] == 3


def test_parse_git_log_empty_input():
    result = ds.parse_git_log("")
    assert result == {
        "commits": [],
        "files_changed": 0,
        "insertions": 0,
        "deletions": 0,
    }


def test_parse_git_log_binary_files_count_as_zero():
    raw = _log(_record("c" * 40, "Dev", "Tue", "add image", ["-\t-\tlogo.png"]))
    result = ds.parse_git_log(raw)
    assert result["insertions"] == 0
    assert result["deletions"] == 0
    assert result["files_changed"] == 1
    assert result["commits"][0]["files"][0]["path"] == "logo.png"


def test_parse_git_log_subject_with_separators():
    # A subject containing tabs and pipes must not corrupt the field split.
    raw = _log(_record("d" * 40, "Dev", "Wed", "weird\tsubject | with chars", ["2\t1\tx.py"]))
    result = ds.parse_git_log(raw)
    assert result["commits"][0]["subject"] == "weird\tsubject | with chars"
    assert result["insertions"] == 2


def test_parse_git_log_skips_malformed_header():
    # A record whose header lacks the four required fields is dropped.
    bad = _RS + "only-one-field\nnot-a-numstat-line"
    result = ds.parse_git_log(bad)
    assert result["commits"] == []


def test_collect_git_log_builds_expected_command():
    captured = {}

    def fake(args):
        captured["args"] = list(args)
        return ""

    ds.collect_git_log("2026-06-06T00:00:00", runner=fake)
    args = captured["args"]
    assert args[:3] == ["git", "log", "--since=2026-06-06T00:00:00"]
    assert "--no-merges" in args
    assert "--numstat" in args


def test_generate_narrative_no_commits_skips_llm():
    called = {"n": 0}

    def fake(args):
        called["n"] += 1
        return "should not run"

    activity = ds.parse_git_log("")
    out = ds.generate_narrative("since", activity, runner=fake)
    assert called["n"] == 0
    assert "Nothing" in out or "nothing" in out


def test_generate_narrative_calls_claude():
    raw = _log(_record("e" * 40, "Dev", "Thu", "do work", ["1\t0\tx.py"]))
    activity = ds.parse_git_log(raw)

    def fake(args):
        assert args[0] == "claude"
        assert args[1] == "-p"
        return "  Hey future you, you shipped x.py.  \n"

    out = ds.generate_narrative("since", activity, runner=fake)
    assert out == "Hey future you, you shipped x.py."


def test_generate_narrative_degrades_on_error():
    raw = _log(_record("f" * 40, "Dev", "Fri", "do work", ["1\t0\tx.py"]))
    activity = ds.parse_git_log(raw)

    def boom(args):
        raise FileNotFoundError("claude not installed")

    out = ds.generate_narrative("since", activity, runner=boom)
    assert out.startswith("(narrative unavailable")


def test_build_summary_end_to_end():
    raw = _log(_record("0" * 40, "Dev", "Sat", "ship it", ["5\t1\tsrc/a.py"]))

    def git_runner(args):
        return raw

    def claude_runner(args):
        return "memo body"

    out = ds.build_summary(
        "2026-06-06T00:00:00",
        git_runner=git_runner,
        claude_runner=claude_runner,
    )
    assert "Commits: 1" in out
    assert "Tomorrow memo" in out
    assert "memo body" in out


def test_main_uses_injected_now_for_default(monkeypatch, capsys):
    captured = {}

    def git_runner(args):
        captured["args"] = list(args)
        return ""

    monkeypatch.setattr(ds, "_run_git", git_runner)
    monkeypatch.setattr(ds, "_run_claude", lambda args: "")

    rc = ds.main([], now=datetime(2026, 6, 6, 23, 59, 59))
    assert rc == 0
    assert "--since=2026-06-06T00:00:00" in captured["args"]
