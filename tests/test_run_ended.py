# Copyright(C) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""How an agent's run ended (result.json's "ended"), recorded apart from its grade, with the agent's own last
error: a crash's cause is often only in the agent's log ("opencode exited 1" says nothing)."""
import json
import sys
from pathlib import Path

import pytest

import run_eval
from agents import TaskSpec
from agents import trace as T
from agents.base import AgentResult, ended, ended_text
from agents.command import CommandAgent

TRACES = Path(__file__).parent / "agent_traces"
#: What llama-server answers a request with an image when it was started without --mmproj.
MMPROJ = "image input is not supported - hint: if this is unexpected, you may need to provide the mmproj"


def result(**kw):
    return AgentResult(answer="", wall_seconds=1.0, model="m", **kw)


@pytest.mark.parametrize("kw, how, text", [
    ({"exit_code": 0}, "finished", "finished"),
    ({"timed_out": True, "error": "stopped at the 600s time cap", "error_kind": "failed"}, "timed out",
     "timed out: stopped at the 600s time cap"),
    ({"exit_code": 1, "error": "opencode exited 1", "error_kind": "failed", "last_error": MMPROJ}, "crashed",
     f"crashed (exit 1): {MMPROJ}"),
    ({"exit_code": 1, "error": "opencode exited 1", "error_kind": "failed"}, "crashed",
     "crashed (exit 1): opencode exited 1"),
    ({"exit_code": 0, "error": "pi: Context overflow", "error_kind": "failed"}, "ended with an error",
     "ended with an error (exit 0): pi: Context overflow"),
    ({"exit_code": 0, "error": "error_max_turns", "error_kind": "failed", "end": "out of steps"}, "out of steps",
     "out of steps: error_max_turns"),
    ({"error": "stalled: the agent stopped to ask a human", "error_kind": "failed", "end": "stalled"}, "stalled",
     "stalled: stalled: the agent stopped to ask a human"),
    ({"error": "opencode: not found", "error_kind": "unavailable"}, "could not run", "could not run: opencode: not found"),
])
def test_how_a_run_ended_is_one_word_and_the_agents_own_error(kw, how, text):
    end = ended(result(**kw))
    assert end["how"] == how and ended_text(end) == text
    assert ended_text({}) == ""


def test_opencodes_error_event_gives_the_servers_message_not_the_json_around_it():
    # Recorded from OpenCode 2.2.7 against a server that answered every request 500 with llama-server's error.
    t = T.parse("opencode", (TRACES / "opencode-server-error.jsonl").read_text(encoding="utf-8"))
    assert t.failure == f"{MMPROJ} (HTTP 500, after 10 retries)"
    old = T.parse("opencode", json.dumps({"type": "error", "error": {"name": "APIError",
                                                                     "data": {"message": "boom", "statusCode": 400}}}))
    assert old.failure == "boom (HTTP 400)"  # OpenCode 1.x
    odd = T.parse("opencode", json.dumps({"type": "error", "error": {"name": "UnknownError"}}))
    assert odd.failure == '{"name": "UnknownError"}'


def test_the_last_error_is_the_traces_else_the_last_error_line_on_stderr():
    assert T.last_error(T.Trace("x", failure="from the trace"), "Error: on stderr") == "from the trace"
    stderr = "INFO starting\nError: model server refused the request\nWARN shutting down\n"
    assert T.last_error(T.Trace("x"), stderr) == "Error: model server refused the request"
    assert T.last_error(T.Trace("x"), "all good\n") == ""


def test_a_command_agent_that_exits_1_reports_the_error_from_its_trace(tmp_path):
    script = tmp_path / "fake_cli.py"
    script.write_text("import sys, pathlib\n"
                      f"sys.stdout.write(pathlib.Path({str(TRACES / 'opencode-server-error.jsonl')!r})"
                      ".read_text(encoding='utf-8'))\nsys.exit(1)\n", encoding="utf-8")
    folder = tmp_path / "task"
    folder.mkdir()
    r = CommandAgent("opencode", {"cmd": [sys.executable, str(script)], "answer": "trace", "trace": "opencode"}).run(
        TaskSpec(id="t", prompt="Do it", folder=folder, timeout_s=60, needs=[]), tmp_path / "log")
    assert r.error.endswith("exited 1") and r.exit_code == 1
    assert ended_text(ended(r)) == f"crashed (exit 1): {MMPROJ} (HTTP 500, after 10 retries)"


def test_a_run_that_crashed_after_writing_its_file_records_how_it_ended(tmp_path, monkeypatch, capsys):
    """The agent wrote the deliverable, then exited 1 with an error on stderr."""
    script = tmp_path / "crash.py"
    script.write_text("import pathlib, sys\n"
                      "pathlib.Path('weekly_build_times.csv').write_text('week,minutes\\n1,2\\n')\n"
                      "print('INFO done writing', file=sys.stderr)\n"
                      "print('Error: 500 image input is not supported', file=sys.stderr)\n"
                      "sys.exit(1)\n", encoding="utf-8")
    agent = CommandAgent("crasher", {"cmd": [sys.executable, str(script)]})
    monkeypatch.setattr(run_eval, "EVAL_HOME", tmp_path / "home")
    monkeypatch.setattr(run_eval, "get_agent", lambda name, model: agent)
    run_dir = run_eval.run("crasher", "m", "verify", ["verify-02"], tmp_path / "out", tmp_path / "fx", "m",
                           do_grade=False)
    data = json.loads((run_dir / ".records" / "verify-02" / "result.json").read_text(encoding="utf-8"))
    assert data["ended"] == {"how": "crashed", "exit_code": 1, "message": "Error: 500 image input is not supported"}
    assert data["error"].endswith("exited 1")  # the runner's own record is unchanged; grading reads "ended"
    out = " ".join(capsys.readouterr().out.split())
    assert "ended: crashed (exit 1): Error: 500 image input is not supported" in out
    assert (Path(json.loads((run_dir / "kw-run.json").read_text(encoding="utf-8"))["tasks"]["verify-02"]["workdir"])
            / "weekly_build_times.csv").is_file()


def test_the_summary_lists_the_runs_that_did_not_end_cleanly_and_counts_them(tmp_path, monkeypatch):
    run_dir = tmp_path / "runs" / "oc-qwen" / "r1"
    graded = tmp_path / "graded" / "oc-qwen" / "r1"
    graded.mkdir(parents=True)
    monkeypatch.setenv("KW_GRADED_HOME", str(tmp_path / "graded"))
    (graded / "scorecard.json").write_text(json.dumps({
        "harness": "opencode", "model": "qwen", "suite": "portable", "axes": [],
        "tasks": [{"id": tid, "passed": True, "why": "", "wall_seconds": 1.0, "answer_set": "public", "judge": {}}
                  for tid in ("a", "b", "c")]}), encoding="utf-8")
    ends = {"a": {"how": "crashed", "exit_code": 1, "message": MMPROJ}, "b": {"how": "finished", "exit_code": 0,
                                                                              "message": ""}, "c": {}}
    for tid, end in ends.items():
        (run_dir / ".records" / tid).mkdir(parents=True)
        (run_dir / ".records" / tid / "result.json").write_text(json.dumps({"ended": end} if end else {}),
                                                                 encoding="utf-8")
    text = run_eval.summarize(run_dir, {"framework": "x", "tasks": "t", "commit": "c", "dirty": False})
    assert f"{run_eval.ENDED}\n- a: crashed (exit 1): {MMPROJ}\n" in text and "- b:" not in text
    assert "Not ended cleanly: 1 of 3 agent runs" in text
    console = run_eval.plain(text)
    block = console.split("\na\n", 1)[1].split("\n\n", 1)[0]
    assert "ended:" in block and "crashed (exit 1)" in block
