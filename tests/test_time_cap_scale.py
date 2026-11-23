# Copyright(C) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""run_eval.py --time-cap-scale: every task's cap times a factor, recorded with the run and warned about when two
runs that used different factors are compared."""
import argparse
import json

import pytest

import compare
import results_tables as rt
import run_eval

VERSION = {"framework": "x", "tasks": "t", "commit": "c", "dirty": False}


@pytest.mark.parametrize("bad", ["0", "-1", "nan", "inf", "two"])
def test_the_scale_must_be_a_number_above_zero(bad):
    with pytest.raises(argparse.ArgumentTypeError):
        run_eval.scale_factor(bad)
    assert run_eval.scale_factor("0.5") == 0.5


def test_the_cap_is_the_tasks_own_times_the_scale_and_says_so():
    assert run_eval.cap_seconds(30) == 1800 and run_eval.cap_text(30) == "30 min cap"
    assert run_eval.cap_seconds(30, 2.5) == 4500 and run_eval.cap_text(30, 2.5) == "75 min cap: 30 min × 2.5"
    assert run_eval.cap_seconds(5, 0.02) == 6 and run_eval.cap_text(5, 0.02) == "6s cap: 5 min × 0.02"
    assert run_eval.cap_seconds(1, 0.001) == 1  # never 0, which would stop the agent before it starts
    with pytest.raises(SystemExit):
        run_eval.main(["--agent", "claude-code", "--suite", "smoke", "--time-cap-scale", "0"])


def test_a_scaled_run_gives_each_task_the_scaled_cap_and_records_it(tmp_path, monkeypatch, capsys):
    from agents.base import Agent, AgentResult

    given = []

    class Fake(Agent):
        name = "fake"

        def run(self, task, log_dir):
            given.append(task.timeout_s)
            return AgentResult(answer="", wall_seconds=6.0, model="m", timed_out=True, error_kind="failed",
                               error=f"stopped at the {task.timeout_s}s time cap")

    monkeypatch.setattr(run_eval, "EVAL_HOME", tmp_path / "home")
    monkeypatch.setattr(run_eval, "get_agent", lambda name, model: Fake("m"))
    minutes = next(t for t in run_eval.suite_tasks("verify") if t["id"] == "verify-02")["max_minutes"]
    run_dir = run_eval.run("fake", "m", "verify", ["verify-02"], tmp_path / "out", tmp_path / "fx", "m",
                           do_grade=False, time_cap_scale=0.02)
    cap = run_eval.cap_seconds(minutes, 0.02)
    assert given == [cap]
    assert json.loads((run_dir / "kw-run.json").read_text(encoding="utf-8"))["time_cap_scale"] == 0.02
    result = json.loads((run_dir / ".records" / "verify-02" / "result.json").read_text(encoding="utf-8"))
    assert result["time_cap_scale"] == 0.02 and result["time_cap_s"] == cap and result["timed_out"]
    out = capsys.readouterr().out
    assert "time caps: each task's own × 0.02 (--time-cap-scale)" in " ".join(out.split())
    assert f"\nverify-02 (running, {cap}s cap: {minutes} min × 0.02)" in out


def test_an_unscaled_run_reads_as_before(tmp_path, monkeypatch, capsys):
    from agents.base import Agent, AgentResult

    class Fake(Agent):
        name = "fake"

        def run(self, task, log_dir):
            return AgentResult(answer="", wall_seconds=1.0, model="m")

    monkeypatch.setattr(run_eval, "EVAL_HOME", tmp_path / "home")
    monkeypatch.setattr(run_eval, "get_agent", lambda name, model: Fake("m"))
    run_dir = run_eval.run("fake", "m", "verify", ["verify-02"], tmp_path / "out", tmp_path / "fx", "m",
                           do_grade=False)
    out = capsys.readouterr().out
    assert "time caps:" not in out and " min cap)" in out
    assert json.loads((run_dir / "kw-run.json").read_text(encoding="utf-8"))["time_cap_scale"] == 1


def graded_run(tmp_path, monkeypatch, name, scale=None):
    """A run folder and its graded folder with one passed task; *scale* written to kw-run.json when given."""
    run_dir = tmp_path / "runs" / "pi-qwen" / name
    graded = tmp_path / "graded" / "pi-qwen" / name
    graded.mkdir(parents=True)
    (run_dir / ".records" / "a").mkdir(parents=True)
    monkeypatch.setenv("KW_GRADED_HOME", str(tmp_path / "graded"))
    monkeypatch.setattr(compare, "GRADED", tmp_path / "graded")
    monkeypatch.setattr(compare, "RUNS", tmp_path / "runs")
    run = {"suite": "everyday", "tasks": {"a": {}}}
    if scale is not None:
        run["time_cap_scale"] = scale
    (run_dir / "kw-run.json").write_text(json.dumps(run), encoding="utf-8")
    (graded / "scorecard.json").write_text(json.dumps({
        "harness": "pi", "model": "qwen", "suite": "everyday", "axes": [], "judge_model": "j",
        "tasks": [{"id": "a", "passed": True, "why": "", "wall_seconds": 1.0, "answer_set": "public", "judge": {}}]}),
        encoding="utf-8")
    (run_dir / ".records" / "a" / "result.json").write_text("{}", encoding="utf-8")
    return run_dir, graded


def test_the_summary_says_the_caps_were_scaled_and_keeps_the_scale_with_the_scorecard(tmp_path, monkeypatch):
    run_dir, graded = graded_run(tmp_path, monkeypatch, "r1", scale=3)
    text = run_eval.summarize(run_dir, VERSION)
    assert "**Time caps × 3** (--time-cap-scale): each task had 3 times its own time cap" in text
    assert json.loads((graded / "scorecard.json").read_text(encoding="utf-8"))["time_cap_scale"] == 3
    assert "time caps: × 3 each task's own (--time-cap-scale)" in " ".join(run_eval.plain(text).split())
    plain_run, plain_graded = graded_run(tmp_path, monkeypatch, "r2")  # a run made before the option: scale 1
    assert "Time caps" not in run_eval.summarize(plain_run, VERSION)
    assert "time_cap_scale" not in json.loads((plain_graded / "scorecard.json").read_text(encoding="utf-8"))


def test_compare_and_the_tables_warn_when_the_scales_differ(tmp_path, monkeypatch):
    scaled, scaled_graded = graded_run(tmp_path, monkeypatch, "r1", scale=2)
    run_eval.summarize(scaled, VERSION)
    plain_run, plain_graded = graded_run(tmp_path, monkeypatch, "r2", scale=1)
    # Read from the graded folder alone (the scorecard), as when the run folder is on another PC.
    a, b = compare.load(str(scaled_graded)), compare.load(str(plain_graded))
    assert a["time_cap_scale"] == 2 and b["time_cap_scale"] == 1
    assert any(w.startswith("different time caps (each task's own × 2 vs × 1") for w in compare.warnings(a, b))
    assert not any("time caps" in w for w in compare.warnings(b, compare.load(str(plain_run))))
    exps = [rt.from_graded(f"{scaled_graded}=scaled"), rt.from_graded(f"{plain_graded}=plain")]
    text = rt.render(exps)
    assert "⚠️ **Time caps differ** (run_eval.py --time-cap-scale)" in text and "**scaled** × 2; **plain** × 1" in text
    board = rt.scoreboard(exps)
    assert board["arms"]["scaled"]["time_cap_scale"] == 2 and "time_cap_scale" not in board["arms"]["plain"]
    path = tmp_path / "board.json"
    path.write_text(json.dumps(board), encoding="utf-8")
    assert [e["time_cap_scale"] for e in rt.from_scoreboard(path)] == [2, 1]
    assert "Time caps differ" not in rt.render([exps[1], rt.from_graded(f"{plain_graded}=again")])
