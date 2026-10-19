"""Results are recorded as scoreboards and reported as two tables: by use case first, then per task."""
import json
import subprocess
import sys

import results_tables as rt

REPO = rt.KW.parent
AXES = {a: 4 for a in rt.AXES}


def board(path, agent, arm, cloud, tasks, not_run=()):
    path.write_text(json.dumps({"agent": agent, "source": "test", "date": "2026-10-19", "eval": None, "arms": {arm: {
        "model": "m", "machine": "Ryzen AI Max+ 395", "cloud": cloud, "complete": not not_run,
        "summary": {"not_run": list(not_run)}, "tasks": tasks}}}), encoding="utf-8")
    return path


def graded_run(tmp_path):
    """A graded run folder as tools/run_eval.py leaves it (scorecard, run file, eval version, skipped)."""
    d = tmp_path / "kw-graded" / "claude-code-opus" / "knowledgework-1200"
    d.mkdir(parents=True)
    card = {"harness": "claude-code", "model": "opus", "suite": "knowledgework", "axes": list(rt.AXES), "tasks": [
        {"id": "kw-01-multihop-research", "passed": True, "wall_seconds": 41.0, "cost_usd": 0.12, "judge": AXES},
        {"id": "kw-03-term-sheet-memo", "passed": None, "wall_seconds": 90.0, "cost_usd": 0.5, "judge": {"error": "x"}},
        {"id": "dev-gc-injected-note", "passed": False, "wall_seconds": 30.0, "cost_usd": 0.2, "judge": AXES},
    ], "unrecorded": ["kw-06-expense-audit"]}
    (d / "scorecard.json").write_text(json.dumps(card), encoding="utf-8")
    (d / "kw-run.json").write_text("{}", encoding="utf-8")
    (d / "eval-version.json").write_text(json.dumps({"framework": "9.9.9", "tasks": "abc123"}), encoding="utf-8")
    (d / "skipped.json").write_text(json.dumps({"kw-14-downloads-folder": "inputs missing"}), encoding="utf-8")
    return d


def test_two_tables_cloud_cost_not_run_and_not_graded(tmp_path, capsys):
    cloud = board(tmp_path / "c.json", "Cloudy", "cloud", True, {
        "kw-01-multihop-research": {"pass": True, "quality": 4.6, "wall_s": 41, "cost_usd": 0.12},
        "kw-02-signage-bom": {"pass": None, "quality": None, "wall_s": 100, "cost_usd": 0.5},
        "dev-gc-injected-note": {"pass": True, "quality": 5.0, "wall_s": 20, "cost_usd": 0.05}})
    local = board(tmp_path / "l.json", "Homey", "local", False, {
        "kw-01-multihop-research": {"pass": False, "quality": 3.0, "wall_s": 240, "cost_usd": 0.0}},
        not_run=["kw-02-signage-bom"])
    assert rt.main([str(cloud), str(local), "--out", str(tmp_path / "t.md")]) == 0
    md = capsys.readouterr().out
    assert (tmp_path / "t.md").read_text(encoding="utf-8") == md
    t1, t2 = md.index("Table 1"), md.index("Table 2")
    assert t1 < t2
    summary, tasks = md[t1:t2], md[t2:]
    for row in ("| Research (2) |", "| Software development (1) |", "| Trust & safety (1) |", "| **All** (3) |"):
        assert row in summary, row
    assert summary.index("Trust & safety") < summary.index("**All**")
    assert "| Research (2) | 1/2 · Q 4.6 · 1m 10s · $0.62 | 0/1 · Q 3.0 · 4m 00s |" in summary
    assert "| **All** (3) | 2/3 · Q 4.8 · 2m 41s · $0.67 | 0/1 · Q 3.0 · 4m 00s |" in summary
    assert "Not run, **Homey local** (2): dev-gc-injected-note, kw-02-signage-bom." in summary
    assert "| kw-01-multihop-research | medium | ✅ 4.6 · 41s · $0.12 | ❌ 3.0 · 4m 00s |" in tasks
    assert "| kw-02-signage-bom | hard | — 1m 40s · $0.50 | ⏳ |" in tasks
    assert tasks.index("**Research**") < tasks.index("**Software development**")


def test_graded_run_renders_and_round_trips_through_a_scoreboard(tmp_path, capsys):
    d = graded_run(tmp_path)
    out = tmp_path / "scoreboard" / "claude-code.json"
    assert rt.main(["--from-graded", str(d), "--machine", "Ryzen AI Max+ 395, 128 GB", "--scoreboard-out", str(out)]) == 0
    direct = capsys.readouterr().out
    assert "| kw-03-term-sheet-memo | hard | — 1m 30s · $0.50 |" in direct      # not graded
    assert "| kw-06-expense-audit | hard | ❌ |" in direct                     # never recorded: a fail
    assert "| kw-14-downloads-folder | hard | ⏳ |" in direct                  # skipped: not run
    assert "| Trust & safety (2) | 0/1 · Q 4.0 · 30s · $0.20 |" in direct     # injected note; kw-14 (risk:scale) not run

    sb = json.loads(out.read_text(encoding="utf-8"))
    assert sb["agent"] == "claude-code" and sb["eval"] == {"version": "9.9.9", "tasks": "abc123"}
    arm = sb["arms"]["claude-code opus"]
    assert arm["cloud"] is True and arm["complete"] is False and arm["machine"] == "Ryzen AI Max+ 395, 128 GB"
    assert arm["summary"]["not_run"] == ["kw-14-downloads-folder"] and arm["summary"]["passed"] == 1
    assert arm["tasks"]["kw-01-multihop-research"] == {"pass": True, "quality": 4.0, "wall_s": 41.0, "cost_usd": 0.12}
    assert arm["tasks"]["kw-03-term-sheet-memo"]["pass"] is None
    assert b"\r\n" not in out.read_bytes()

    assert rt.main([str(out)]) == 0
    assert capsys.readouterr().out == direct


def test_local_graded_run_shows_no_cost_unless_marked_cloud(tmp_path, capsys):
    d = graded_run(tmp_path)
    card = json.loads((d / "scorecard.json").read_text(encoding="utf-8"))
    for t in card["tasks"]:
        t["cost_usd"] = None
    (d / "scorecard.json").write_text(json.dumps(card), encoding="utf-8")
    rt.main(["--from-graded", f"{d}=Local"])
    assert "$" not in capsys.readouterr().out
    rt.main(["--from-graded", f"{d}=Local", "--cloud", "Local"])
    assert "$—" in capsys.readouterr().out


def test_published_perplexity_scoreboard_still_renders():
    out = subprocess.run([sys.executable, str(REPO / "tools" / "results_tables.py"),
                          str(REPO / "scoreboard" / "perplexity-computer.json")],
                         capture_output=True, text=True, encoding="utf-8", check=True).stdout
    assert out.index("Table 1") < out.index("Table 2")
    assert "| **All** (16) | 12/16 · Q 4.4 · 52m 48s · $19.78 | 1/1 · Q 4.8 · 3m 57s |" in out


def test_recording_another_run_keeps_the_files_other_arms(tmp_path, capsys):
    d = graded_run(tmp_path)
    out = tmp_path / "claude-code.json"
    rt.main(["--from-graded", f"{d}=claude-code opus", "--scoreboard-out", str(out)])
    rt.main(["--from-graded", f"{d}=claude-code sonnet", "--scoreboard-out", str(out)])
    capsys.readouterr()
    assert set(json.loads(out.read_text(encoding="utf-8"))["arms"]) == {"claude-code opus", "claude-code sonnet"}


def test_perplexity_tables_wrapper_renders_its_legs():
    import tables

    row = {"passed": True, "deliverable": 50.0, "total": 55, "usd": 0.33, "quality": 5.0}
    md = tables.render({"kw-01-multihop-research": {"perplexity-cloud": row, "perplexity-local": dict(row, usd=0.0)}})
    assert md.index("Table 1") < md.index("Table 2")
    assert "| kw-01-multihop-research | medium | ✅ 5.0 · 55s · $0.33 | ✅ 5.0 · 55s | ⏳ | ⏳ |" in md
