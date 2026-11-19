# Copyright(C) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""The setup checks before a long run (pc_checks.py, the judge check), grading any mix of runs in one command,
and a leg's battery.md."""
import json
import subprocess
import sys

import pytest

import battery
import pc_checks
import run_eval

#: The real functions: tests/conftest.py replaces them for every other test.
REAL_JUDGE_READY = run_eval.judge_ready
REAL_WARNINGS = pc_checks.warnings


def _card(tid, passed, **extra):
    return {"id": tid, "passed": passed, "wall_seconds": 60.0, "judge": {"accuracy": 4}, **extra}


def _graded(leg, name, suite, tasks, skipped=None):
    d = leg / name
    d.mkdir(parents=True)
    (d / "scorecard.json").write_text(json.dumps({"suite": suite, "axes": ["accuracy"], "tasks": tasks}))
    if skipped:
        (d / "skipped.json").write_text(json.dumps(skipped))
    return d


def test_battery_counts_the_latest_run_of_each_suite_and_leaves_out_the_warm_ups(tmp_path):
    leg = tmp_path / "gaia-app-m"
    _graded(leg, "everyday-20261117-090000", "everyday", [_card("a", False)])  # older: not counted
    _graded(leg, "everyday-20261118-090000", "everyday",
            [_card("a", True), _card("b", False), _card("c", True, contaminated=["x"]),
             _card("d", False, error_kind="unavailable"), _card("e", None)], skipped={"f": "no pack"})
    _graded(leg, "verify-20261118-080000", "verify", [_card("verify-02", True)])
    _graded(leg, "smoke-20261118-081000", "smoke", [_card("kw-00-smoke", True)])
    text = battery.render(leg)
    assert "everyday-20261118-090000" in text and "everyday-20261117" not in text
    # passed out of graded: the contaminated task fails, did-not-run and the judge's null are counted apart
    assert "| everyday | everyday-20261118-090000 | 1/3 | 0 | 1 | 1 | 1.0 min | 4.0 |" in text
    assert "| verify (not counted) |" in text
    assert "| **Total** | | **2/4** |" in text  # smoke counted, verify not
    assert battery.write(leg) == leg / "battery.md" and (leg / "battery.md").is_file()
    assert battery.main([str(tmp_path / "nothing")]) == 1


def test_disk_warning_names_the_folder(tmp_path):
    assert pc_checks.disk_warnings([tmp_path], min_free_gb=0) == []
    warned = pc_checks.disk_warnings([tmp_path / "not" / "made", tmp_path], min_free_gb=1e9)
    assert len(warned) == 1 and "low disk space" in warned[0]  # one drive, said once


def test_lock_warnings_read_the_policy_the_screen_saver_and_the_screen(monkeypatch):
    values = {("HKEY_LOCAL_MACHINE", "InactivityTimeoutSecs"): "900",
              ("HKEY_CURRENT_USER", "ScreenSaverIsSecure"): "1", ("HKEY_CURRENT_USER", "ScreenSaveActive"): "1",
              ("HKEY_CURRENT_USER", "ScreenSaveTimeOut"): "600"}

    def read(hive, key, name):
        return values.get((hive, name))
    found = pc_checks.lock_warnings(60, read=read, screen_off=lambda s: 10, platform="win32")
    assert len(found) == 3 and "15 min" in found[0] and "10 min" in found[1] and "KW_KEEP_DISPLAY_ON" in found[2]
    assert pc_checks.lock_warnings(5, read=read, screen_off=lambda s: 10, platform="win32") == []
    assert pc_checks.lock_warnings(60, read=read, screen_off=lambda s: 10, platform="linux") == []


def test_library_warning_names_the_missing_packages():
    assert pc_checks.library_warnings(sys.executable, modules=(("json", "json"),)) == []
    warned = pc_checks.library_warnings(sys.executable, modules=(("json", "json"), ("no_such_mod_kw", "no-such")))
    assert len(warned) == 1 and "lacks no-such" in warned[0] and "json" not in warned[0].split("lacks")[1]


def test_the_longest_cap_comes_from_the_task_file():
    assert pc_checks.longest_cap_minutes() >= 30


@pytest.mark.parametrize("envelope, ok", [
    ({"type": "result", "is_error": False, "result": "ok"}, True),
    ({"type": "result", "is_error": True, "result": "Not logged in · Please run /login"}, False),
    ({"type": "result", "is_error": False, "result": ""}, False),
])
def test_the_judge_check_stops_a_run_before_any_agent_time(monkeypatch, envelope, ok):
    monkeypatch.setattr(run_eval.ft, "call_judge", lambda payload, model, env: json.dumps(envelope))
    if ok:
        REAL_JUDGE_READY("opus")
    else:
        with pytest.raises(run_eval.EvalError, match="can't be called.*--no-grade"):
            REAL_JUDGE_READY("opus")


def test_the_judge_check_reports_a_missing_cli(monkeypatch):
    def missing(*a):
        raise FileNotFoundError("The judge needs the Claude Code CLI on PATH")
    monkeypatch.setattr(run_eval.ft, "call_judge", missing)
    with pytest.raises(run_eval.EvalError, match="Claude Code CLI on PATH"):
        REAL_JUDGE_READY("opus")
    monkeypatch.setattr(run_eval.ft, "call_judge", lambda *a: (_ for _ in ()).throw(subprocess.TimeoutExpired("c", 1)))
    with pytest.raises(run_eval.EvalError, match="TimeoutExpired"):
        REAL_JUDGE_READY("opus")


def _run(leg_dir, name, suite, tasks, recorded=True, prepared="2026-11-18T09:00:00+0000"):
    run = leg_dir / name
    (run / ".records").mkdir(parents=True)
    (run / "kw-run.json").write_text(json.dumps({"suite": suite, "prepared_at": prepared,
                                                 "tasks": {t: {} for t in tasks}}))
    for tid in tasks if recorded else []:
        (run / ".records" / tid).mkdir()
        (run / ".records" / tid / "result.json").write_text(json.dumps({"harness": "claude-code"}))
    return run


def test_a_leg_stands_for_the_latest_recorded_run_of_each_suite(tmp_path, monkeypatch):
    monkeypatch.setattr(run_eval, "EVAL_HOME", tmp_path)
    leg = tmp_path / "runs" / "gaia-app-m"
    _run(leg, "everyday-20261117-090000", "everyday", ["a"], prepared="2026-11-17T09:00:00+0000")
    new = _run(leg, "everyday-20261118-090000", "everyday", ["a"])
    _run(leg, "everyday-20261119-090000", "everyday", ["a"], recorded=False)  # stopped before any task
    verify = _run(leg, "verify-20261118-080000", "verify", ["verify-02"])
    assert sorted(run_eval.grade_targets([leg])) == sorted([new.resolve(), verify.resolve()])
    assert sorted(run_eval.grade_targets([run_eval.Path("gaia-app-m")])) == sorted([new.resolve(), verify.resolve()])
    assert run_eval.grade_targets([new, new]) == [new.resolve()]
    with pytest.raises(run_eval.EvalError, match="is not a run folder"):
        run_eval.grade_targets([tmp_path / "typo"])
    with pytest.raises(run_eval.EvalError, match="no run with a recorded task"):
        run_eval.grade_targets([tmp_path])


def test_runs_of_several_suites_grade_in_one_command_one_suite_at_a_time(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("KW_GRADED_HOME", str(tmp_path / "graded"))
    runs = [_run(tmp_path / "runs" / leg, f"{suite}-1", suite, tasks)
            for leg, suite, tasks in (("leg-a", "verify", ["verify-02"]), ("leg-a", "portable", ["pc-01"]),
                                      ("leg-b", "portable", ["pc-01"]))]
    monkeypatch.setattr(run_eval, "check_turns", lambda d: [])
    monkeypatch.setattr(run_eval, "build", lambda ids, *a: None)
    monkeypatch.setattr(run_eval, "inputs_drift", lambda run_dir, suite, ids, fixtures: {})
    calls = []

    def grade(run_dir, suite, fixtures, judge_model, only=None, with_runs=()):
        calls.append((suite, run_dir, list(with_runs)))
        for d in [run_dir, *with_runs]:
            run_eval.graded_dir(d).mkdir(parents=True, exist_ok=True)
            tid = "verify-02" if suite == "verify" else "pc-01"
            (run_eval.graded_dir(d) / "scorecard.json").write_text(json.dumps({
                "harness": "claude-code", "model": "m", "suite": suite, "axes": [], "unrecorded": [],
                "tasks": [{**_card(tid, True), "judge": {"cost_usd": 0.25}}]}))
    monkeypatch.setattr(run_eval, "grade", grade)
    _, not_graded = run_eval.regrade(runs, tmp_path, "m")
    assert not not_graded
    assert calls == [("verify", runs[0], []), ("portable", runs[1], [runs[2]])]  # the legs of one suite together
    out = capsys.readouterr().out
    assert "verify, 1 task(s), $0.25" in out and "portable, 2 task(s), $0.50" in out
