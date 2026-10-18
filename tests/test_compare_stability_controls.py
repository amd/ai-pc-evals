"""Ported from gaia: comparing two graded runs (with a gate), stability over repeats, and judge controls."""
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
        "id": "dev-x", "category": "software_engineering", "prompt": "Answer in {workdir}.", "inputs": [],
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
