# Copyright(C) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""Comparing two graded runs (with a gate), stability over repeats, and judge controls."""
import json

import compare
import judge_controls
import stability
from kwengine import schema


def card(tmp_path, name, results, judge="claude-opus-5-5", suite="developer"):
    d = tmp_path / "graded" / "claude-code-x" / name
    d.mkdir(parents=True)
    tasks = [{"id": t, "passed": p, "judge": {"accuracy": q, "work_quality": q}} for t, (p, q) in results.items()]
    (d / "scorecard.json").write_text(json.dumps({"harness": "claude-code", "model": "x", "suite": suite,
                                                  "judge_model": judge, "tasks": tasks}), encoding="utf-8")
    return str(d)


def test_compare_classifies_each_task_and_the_gate_counts_a_vanished_task(tmp_path, capsys):
    base = card(tmp_path, "a", {"t1": (True, 5), "t2": (False, 2), "t3": (True, 4), "t4": (True, 4)})
    new = card(tmp_path, "b", {"t1": (False, 3), "t2": (True, 4), "t3": (True, 5), "t5": (True, 5)})
    diff = compare.compare(compare.load(base), compare.load(new))
    assert [r[0] for r in diff["regressed"]] == ["t1"] and [r[0] for r in diff["improved"]] == ["t2"]
    assert [r[0] for r in diff["vanished"]] == ["t4"] and [r[0] for r in diff["added"]] == ["t5"]
    assert compare.main([base, new]) == 0
    assert compare.main([base, new, "--gate"]) == 1
    same = card(tmp_path, "c", {"t1": (True, 5), "t2": (False, 2), "t3": (True, 4), "t4": (True, 4)})
    assert compare.main([base, same, "--gate"]) == 0


def test_compare_warns_when_the_judge_differs(tmp_path):
    a = compare.load(card(tmp_path, "a", {"t1": (True, 5)}))
    b = compare.load(card(tmp_path, "b", {"t1": (True, 5)}, judge="claude-opus-5"))
    assert any("different judges" in w for w in compare.warnings(a, b))


def test_stability_labels_flaky_and_the_worst_run_counts(tmp_path):
    runs = [card(tmp_path, f"r{i}", {"t1": (True, 5), "t2": (i != 2, 4), "t3": (False, 2)}) for i in (1, 2, 3)]
    s = stability.summarize([compare.load(r) for r in runs])
    assert s["t1"]["status"] == "stable-pass" and s["t2"]["status"] == "flaky" and s["t3"]["status"] == "stable-fail"
    assert s["t2"]["passed"] == 2 and s["t2"]["runs"] == 3
    text = stability.report(runs)
    assert "1 stable-pass · 1 flaky · 1 stable-fail" in text and "1/3 pass" in text


def _stated_task():
    return schema.parse_task({
        "id": "dev-x", "category": "software_dev", "prompt": "Answer in {workdir}.", "inputs": [],
        "deliverables": [], "check": "stated", "must_establish": ["Says 42."], "genuine_answer": "It is 42.",
        "wrong_answers": ["It is 41.", "I can't tell."], "max_minutes": 5}, schema.TASKS_FILE)


def test_controls_send_the_reference_each_wrong_answer_and_an_empty_reply(monkeypatch, tmp_path):
    task = _stated_task()
    planned = judge_controls.controls_for(task, "(no input files)")
    assert [(a.answer, want, label) for a, want, label in planned] == [
        ("It is 42.", True, "reference"), ("It is 41.", False, "wrong 1"), ("I can't tell.", False, "wrong 2"),
        ("", False, "empty")]

    def judge(attempts, model, env):  # a judge that is fooled by the second wrong answer
        return {a.key: {"answers_correctly": a.answer in ("It is 42.", "I can't tell.")} for a in attempts}
    monkeypatch.setattr(judge_controls.grade, "judge_batch", judge)
    report = judge_controls.run([task], "m", tmp_path, batch=2, env={})
    assert [(r["control"], r["ok"]) for r in report["controls"]] == [
        ("reference", True), ("wrong 1", True), ("wrong 2", False), ("empty", True)]
    assert len(report["failed"]) == 1


def test_compare_counts_contaminated_as_a_fail_and_unrecorded_and_ungraded_tasks_as_neither(tmp_path):
    base = card(tmp_path, "a", {"t1": (True, 5), "t2": (True, 5), "t3": (True, 5), "t4": (True, 5)})
    new = card(tmp_path, "b", {"t1": (True, 5), "t2": (None, 0), "t4": (True, 5)})
    data = json.loads((tmp_path / "graded" / "claude-code-x" / "b" / "scorecard.json").read_text(encoding="utf-8"))
    data["tasks"][0]["contaminated"] = ["read secrets/answers.enc"]
    data["unrecorded"] = ["t3"]
    (tmp_path / "graded" / "claude-code-x" / "b" / "scorecard.json").write_text(json.dumps(data), encoding="utf-8")
    diff = compare.compare(compare.load(base), compare.load(new))
    assert [r[0] for r in diff["regressed"]] == ["t1"]  # contaminated
    # Prepared but never recorded: not run, no verdict; a gate still fails on it, since BASE passed it.
    assert [r[0] for r in diff["ungraded"]] == ["t2", "t3"] and not diff["vanished"]


def test_compare_totals_leave_out_tasks_not_run_or_not_graded_but_still_list_and_gate_on_them(tmp_path, capsys):
    base = card(tmp_path, "a", {"t1": (True, 5), "t2": (True, 5), "t3": (True, 5)})
    new = card(tmp_path, "b", {"t1": (True, 5)})
    path = tmp_path / "graded" / "claude-code-x" / "b" / "scorecard.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["unrecorded"], data["not_graded"] = ["t2"], ["t3"]
    path.write_text(json.dumps(data), encoding="utf-8")
    assert compare.main([base, new, "--gate"]) == 1  # BASE passed t2 and t3, which NEW didn't measure
    out = capsys.readouterr().out
    # As grade.render and results_tables count it: 1 of the 1 graded task, the other two listed apart.
    assert "Base 3/3 passed · new 1/1 passed (2 not run or not graded, not counted)" in out
    assert "| t2 |" in out and "| t3 |" in out


def test_stability_ignores_a_run_the_judge_could_not_grade(tmp_path):
    runs = [card(tmp_path, f"r{i}", {"t1": (None if i == 2 else True, 5), "t2": (None, 0)}) for i in (1, 2, 3)]
    s = stability.summarize([compare.load(r) for r in runs])
    # Two graded passes and one ungraded run is not a stable pass: the third could have failed.
    assert s["t1"]["status"] == "not fully graded (2 of 3)" and s["t1"]["runs"] == 2
    assert s["t2"]["status"] == "not graded"
    text = stability.report(runs)
    assert "not graded" in text and "0 stable-pass" in text and "1 not fully graded" in text


def test_stability_one_graded_pass_among_ungraded_repeats_is_not_stable(tmp_path):
    runs = [card(tmp_path, f"r{i}", {"t1": (True if i == 1 else None, 5), "t2": ((i != 2) if i < 3 else None, 4)})
            for i in (1, 2, 3)]
    s = stability.summarize([compare.load(r) for r in runs])
    assert s["t1"]["status"] == "not fully graded (1 of 3)"
    assert s["t2"]["status"] == "flaky"  # a pass and a fail already: flaky whatever the third said
    assert "0/2 pass" in stability.report(runs)


def test_judge_controls_cover_both_tasks_too():
    stated = _stated_task()
    both = schema.parse_task({
        "id": "dev-y", "category": "software_dev", "prompt": "Answer in {workdir}.", "inputs": [],
        "deliverables": [], "check": "both", "probe": ["assert True"], "must_establish": ["Says 42."],
        "genuine_answer": "It is 42.", "wrong_answers": ["It is 41."], "max_minutes": 5}, schema.TASKS_FILE)
    mech = schema.parse_task({
        "id": "dev-z", "category": "software_dev", "prompt": "Answer in {workdir}.", "inputs": [],
        "deliverables": [], "check": "mechanical", "probe": ["assert True"], "max_minutes": 5}, schema.TASKS_FILE)
    by_id = {t.id: t for t in (stated, both, mech)}
    assert [t.id for t in judge_controls.judged_tasks(by_id, ["dev-x", "dev-y", "dev-z"])] == ["dev-x", "dev-y"]


def _versioned(tmp_path, name, results, version):
    d = card(tmp_path, name, results)
    if version:
        folder = tmp_path / "graded" / "claude-code-x" / name
        (folder / "eval-version.json").write_text(json.dumps({"framework": "0.6.0", "tasks": version}), encoding="utf-8")
    return d


def test_the_gate_fails_when_a_base_pass_is_ungraded_in_new_or_new_has_nothing_graded(tmp_path, capsys):
    base = _versioned(tmp_path, "a", {"t1": (True, 5), "t2": (False, 2)}, "v1")
    lost = _versioned(tmp_path, "b", {"t1": (None, 0), "t2": (False, 2)}, "v1")
    assert compare.main([base, lost]) == 0
    assert compare.main([base, lost, "--gate"]) == 1
    assert "passed in BASE but not graded in NEW" in capsys.readouterr().err
    nothing = _versioned(tmp_path, "c", {"t1": (None, 0), "t2": (None, 0)}, "v1")
    assert compare.main([base, nothing, "--gate"]) == 1
    assert "NEW has no graded task" in capsys.readouterr().err
    fixed = _versioned(tmp_path, "d", {"t1": (True, 5), "t2": (None, 0)}, "v1")  # a BASE fail ungraded: no loss
    assert compare.main([base, fixed, "--gate"]) == 0


def test_the_gate_fails_on_different_task_fingerprints_unless_allowed(tmp_path, capsys):
    base = _versioned(tmp_path, "a", {"t1": (True, 5)}, "v1")
    new = _versioned(tmp_path, "b", {"t1": (True, 5)}, "v2")
    assert any("different task versions" in w for w in compare.warnings(compare.load(base), compare.load(new)))
    assert compare.main([base, new, "--gate"]) == 1
    assert "task versions differ" in capsys.readouterr().err
    assert compare.main([base, new, "--gate", "--allow-version-drift"]) == 0


def test_compare_reads_the_version_from_the_graded_folder_and_warns_when_a_side_has_none(tmp_path):
    base = compare.load(_versioned(tmp_path, "a", {"t1": (True, 5)}, "v1"))
    assert base["tasks_version"] == "v1"  # eval-version.json beside the scorecard, no run dir
    new = compare.load(_versioned(tmp_path, "b", {"t1": (True, 5)}, None))
    w = compare.warnings(base, new)
    assert any(x.startswith("NEW has no eval-version.json") for x in w)
    assert not any(x.startswith("BASE has no") for x in w)


def test_compare_shows_each_sides_model_time_when_it_was_measured(tmp_path, capsys, monkeypatch):
    sides = []
    for name, model_s in (("a", 20.0), ("b", None)):
        d = tmp_path / "graded" / "pi-x" / name
        (d / ".records" / "t1").mkdir(parents=True)
        (d / "kw-run.json").write_text("{}", encoding="utf-8")
        (d / "scorecard.json").write_text(json.dumps({"harness": "pi", "model": "x", "suite": "developer", "tasks": [
            {"id": "t1", "passed": True, "wall_seconds": 25.0, "judge": {"accuracy": 4}}]}), encoding="utf-8")
        if model_s is not None:
            (d / ".records" / "t1" / "result.json").write_text(
                json.dumps({"telemetry": {"server": {"model_s": model_s}}}), encoding="utf-8")
        sides.append(str(d))
    assert compare.main(sides) == 0
    assert "base 80% · 5 s outside; new —" in capsys.readouterr().out
    # A run dir outside KW_EVAL_HOME, graded into the graded home: its own records are read.
    run = tmp_path / "elsewhere" / "pi-x" / "c"
    (run / ".records" / "t1").mkdir(parents=True)
    (tmp_path / "graded" / "pi-x" / "c").mkdir()
    (tmp_path / "graded" / "pi-x" / "c" / "scorecard.json").write_text(json.dumps({"tasks": [
        {"id": "t1", "passed": True, "wall_seconds": 25.0, "judge": {}}]}), encoding="utf-8")
    (run / "kw-run.json").write_text("{}", encoding="utf-8")
    (run / ".records" / "t1" / "result.json").write_text(json.dumps({"telemetry": {"server": {"model_s": 10.0}}}),
                                                          encoding="utf-8")
    monkeypatch.setattr(compare, "GRADED", tmp_path / "graded")
    assert compare.load(str(run))["tasks"]["t1"]["model_s"] == 10.0
    assert compare.model_time([(10.0, 10.4), (None, 3.0)]) == {"share": 1.04, "median_outside_s": 0.0, "tasks": 1}
