# Copyright(C) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""Tool-call health and context use read from the agents' own output (#71, #72).

The files in tests/agent_traces are real outputs, trimmed (long strings cut): OpenCode 2.2.7, Pi 1.5.0, OpenClaw
2026.11.2 and Hermes Agent 0.23.0 on Ubuntu against llama-server b12211 at a 16K context, asked to read a missing file and then
notes.txt ("two-reads"), or three files of about 10K tokens each ("big-reads", "over-window").
"""
import json
import sys
from pathlib import Path

import pytest

import run_eval
import telemetry
from agents import TaskSpec, get_agent
from agents import trace as T
from agents.command import CommandAgent

TRACES = Path(__file__).resolve().parent / "agent_traces"


def read(name):
    return (TRACES / name).read_text(encoding="utf-8")


# ----------------------------------------------------------------- each agent's output
def test_opencode_json_lists_each_call_its_failure_and_each_requests_prompt():
    t = T.parse("opencode", read("opencode-two-reads.jsonl"))
    assert t.answer == "4127" and not t.failure and t.lists_calls
    assert [(c.tool, c.error) for c in t.calls] == [("read", True), ("read", False)]
    assert "not found" in t.calls[0].result.lower() and "4127" in t.calls[1].result
    assert t.prompts == [5577, 6251]  # input + cache reads + cache writes, per step
    h = T.tool_health(t)
    assert (h["source"], h["calls"], h["errors"], h["malformed"], h["loops"], h["last_call"]) == (
        "opencode", 2, 1, 0, [], None)


def test_opencode_answer_is_its_text_parts_as_text_mode_prints_them():
    t = T.parse("opencode", read("opencode-big-reads.jsonl"))
    assert t.answer.startswith("All three files were read in full") and len(t.calls) == 4
    assert T.context(t)["peak_prompt_tokens"] == 7355 and T.context(t)["requests"] == 3


def test_pi_json_pairs_each_call_with_its_result():
    t = T.parse("pi", read("pi-two-reads.jsonl"))
    assert t.answer == "4127" and not t.failure
    assert [(c.tool, c.args, c.error) for c in t.calls] == [("read", {"path": "missing.txt"}, True),
                                                            ("read", {"path": "notes.txt"}, False)]
    assert t.prompts == [1529, 1709]


def test_pi_over_the_window_is_a_failure_with_the_rejected_size_and_the_compaction():
    """`pi -p` exits 1 with nothing on stdout here; in JSON mode it exits 0, so the trace has to say it failed."""
    t = T.parse("pi", read("pi-over-window.jsonl"))
    assert t.answer == "" and t.failure.startswith("Context overflow recovery failed")
    c = T.context(t)
    assert (c["window"], c["over_window"], c["largest_rejected_tokens"], c["compactions"]) == (16384, 2, 56599, 1)
    assert c["peak_prompt_tokens"] == 56599 and c["peak_share"] > 3  # the rejected request is the peak
    assert T.context_line("t", c) == ("t: 2 requests over the window (largest 56.6K of 16.4K), peak context 56.6K of "
                                      "16.4K (345%), history compacted once")


def test_openclaw_gives_totals_only_so_its_calls_are_not_listed():
    t = T.parse("openclaw", read("openclaw-two-reads.json"))
    assert t.answer == "4127" and (t.n_calls, t.n_errors) == (2, 0) and not t.lists_calls
    h = T.tool_health(t)
    assert h["calls"] == 2 and "totals only" in h["note"]


def test_openclaw_logs_its_overflow_and_compaction_on_stderr_once_per_request():
    t = T.scan_stderr(T.parse("openclaw", read("openclaw-over-window.json")), read("openclaw-over-window.stderr"))
    c = T.context(t)
    # Four log lines describe the one rejected request.
    assert (c["over_window"], c["largest_rejected_tokens"], c["window"], c["compactions"]) == (1, 29662, 16384, 1)
    assert t.answer.startswith("Code word on the last line")


def test_hermes_stream_json_pairs_calls_and_results_and_takes_the_whole_reply():
    t = T.parse("hermes", read("hermes-two-reads.jsonl"))
    assert t.answer == "4127" and not t.failure and t.lists_calls
    assert [(c.tool, c.args, c.error) for c in t.calls] == [("read_file", {"path": "missing.txt"}, True),
                                                            ("read_file", {"path": "notes.txt"}, False)]
    assert t.prompts == []  # only the run's totals


def test_a_hermes_turn_that_didnt_finish_is_a_failure_not_a_reply():
    """Hermes Agent 0.23.0 printed this message as its reply, in text mode with exit code 0."""
    t = T.parse("hermes", read("hermes-unfinished-turn.jsonl"))
    assert t.answer == "" and t.failure == "Worker execution is unknown."


def test_claude_codes_stream_gives_calls_errors_prompts_and_compactions():
    def ev(**k):
        return json.dumps(k)
    stream = "\n".join([
        ev(type="assistant", message={"id": "m1", "usage": {"input_tokens": 10, "cache_read_input_tokens": 900},
                                      "content": [{"type": "tool_use", "id": "a", "name": "Read", "input": {"p": 1}}]}),
        # The same API response, streamed as a second event: counted once.
        ev(type="assistant", message={"id": "m1", "usage": {"input_tokens": 10, "cache_read_input_tokens": 900},
                                      "content": [{"type": "text", "text": "reading"}]}),
        ev(type="user", message={"content": [{"type": "tool_result", "tool_use_id": "a", "is_error": True,
                                              "content": "no such file"}]}),
        ev(type="system", subtype="compact_boundary"),
        ev(type="assistant", message={"id": "m2", "usage": {"input_tokens": 50, "cache_creation_input_tokens": 70},
                                      "content": [{"type": "text", "text": "done"}]}),
        ev(type="result", result="done")])
    t = T.parse("claude-code", stream)
    assert [(c.tool, c.error, c.result) for c in t.calls] == [("Read", True, "no such file")]
    assert t.prompts == [910, 120] and t.compactions == 1 and t.answer == "done"


@pytest.mark.parametrize("fmt", T.FORMATS)
def test_output_that_isnt_the_format_lists_nothing_and_never_raises(fmt):
    t = T.parse(fmt, "Traceback (most recent call last):\n  oops\n")
    assert t.calls == [] and not t.lists_calls and t.n_calls == 0
    with pytest.raises(ValueError, match="unknown trace format"):
        T.parse("nope", "")


# ----------------------------------------------------------------- what is flagged
@pytest.mark.parametrize("text", [
    '<tool_call>\n{"name": "read", "arguments": {"path": "a"}}\n</tool_call>',  # Qwen/Hermes, unparsed
    '<|tool_call|>{"name": "read"}',
    "<|tool_calls_section_begin|><|tool_call_begin|>functions.read:0",  # Kimi
    "<｜tool▁call▁begin｜>".replace("｜", "|"),  # DeepSeek
    "<function=read>\n<parameter=path>a</parameter>\n</function>",  # Qwen3-Coder XML
    '<|python_tag|>{"name": "read"}',  # Llama
    '[TOOL_CALLS][{"name": "read", "arguments": {}}]',  # Mistral
    "<|channel|>commentary to=functions.read <|constrain|>json",  # gpt-oss
    'I will now read it.\n{"name": "read", "parameters": {"path": "a"}}',  # a bare JSON call
])
def test_tool_call_markup_left_in_the_text_is_one_malformed_call(text):
    assert len(T.malformed([text])) == 1


def test_prose_and_json_that_isnt_a_call_are_not_malformed():
    assert T.malformed(['The config is {"name": "x", "size": 3}.', "Use the read tool on notes.txt.",
                        '```json\n{"name": "report", "rows": 3}\n```']) == []


def test_three_identical_calls_in_a_row_are_a_loop_two_are_not():
    calls = [T.Call("read", {"path": "a"})] * 3 + [T.Call("read", {"path": "b"})] + [T.Call("read", {"path": "a"})] * 2
    assert T.loops(calls) == [{"tool": "read", "times": 3, "call": 'read {"path": "a"}'}]
    assert T.loops([T.Call("ls", {}), T.Call("cat", {}), T.Call("ls", {})]) == []
    # Argument order doesn't make two calls different.
    assert T.loops([T.Call("f", {"a": 1, "b": 2}), T.Call("f", {"b": 2, "a": 1}), T.Call("f", {"a": 1, "b": 2})])


def test_the_last_call_is_kept_only_for_a_run_stopped_at_its_cap():
    t = T.Trace("pi", calls=[T.Call("bash", {"command": "sleep 999"})], events=1)
    assert T.tool_health(t, timed_out=True)["last_call"] == 'bash {"command": "sleep 999"}'
    assert T.tool_health(t, timed_out=False)["last_call"] is None


def test_an_agent_without_a_trace_is_still_checked_from_what_it_printed():
    t = T.scan_stderr(T.scan_printed('<tool_call>{"name": "read"}</tool_call>'),
                      "Error: This model's maximum context length is 8192 tokens. However, you requested 5 output "
                      "tokens and your prompt contains at least 8188 input tokens, for a total of at least 8193 tokens.")
    h, c = T.tool_health(t), T.context(t)
    assert (h["calls"], h["errors"], h["malformed"]) == (None, None, 1)  # vLLM 0.29's wording
    assert (c["window"], c["largest_rejected_tokens"], c["over_window"]) == (8192, 8188, 1)
    sglang = T.context(T.scan_stderr(T.scan_printed(""), "The input (40000 tokens) is longer than the model's "
                                                         "context length (32768 tokens)."))
    assert (sglang["window"], sglang["largest_rejected_tokens"]) == (32768, 40000)


def test_a_reply_that_talks_about_context_length_is_not_a_refused_request():
    reply = "Qwen3's maximum context length is 32768 tokens; a context overflow makes it truncate."
    assert T.context(T.scan_printed(reply))["over_window"] == 0
    assert T.context(T.scan_stderr(T.scan_printed(""), reply))["over_window"] == 1  # logged by the agent: it is


def test_summary_lines_say_nothing_when_nothing_is_off():
    assert T.health_line("t", T.tool_health(T.parse("opencode", ""))) == ""
    assert T.context_line("t", {"peak_prompt_tokens": 50_000, "window": 64_000, "peak_share": 0.78}) == ""
    assert T.context_line("t", {"peak_prompt_tokens": 63_100, "window": 64_000, "peak_share": 0.986,
                                "compactions": 2}) == "t: peak context 63.1K of 64.0K (99%), history compacted twice"
    h = {"calls": 9, "errors": 2, "malformed": 1, "loops": [{"tool": "read", "times": 6}], "last_call": None}
    assert T.health_line("t", h) == "t: 1 malformed tool call, 2 of 9 tool calls failed, loop (read x6)"


def test_the_servers_window_and_peak_complete_the_agents_view():
    agent = T.context(T.parse("pi", read("pi-two-reads.jsonl")))
    merged = T.with_server(agent, {"window": 16384, "peak_prompt_tokens": 1800, "requests": 3, "reason": None})
    assert (merged["window"], merged["peak_prompt_tokens"], merged["requests"], merged["peak_share"]) == (
        16384, 1800, 2, 0.11)
    assert T.with_server(agent, None) == agent


# ----------------------------------------------------------------- the command agent
def task(folder):
    return TaskSpec(id="t", prompt="Do it", folder=folder, timeout_s=60, needs=[])


def fake_cli(tmp_path, name, code=0):
    """A CLI that prints a recorded trace (and its stderr) and exits *code*."""
    script = tmp_path / "fake_cli.py"
    stderr = TRACES / name.replace(".json", ".stderr").replace(".jsonl", ".stderr")
    script.write_text("import sys, pathlib\n"
                      f"sys.stdout.write(pathlib.Path({str(TRACES / name)!r}).read_text(encoding='utf-8'))\n"
                      f"e = pathlib.Path({str(stderr)!r})\n"
                      "sys.stderr.write(e.read_text(encoding='utf-8') if e.is_file() else '')\n"
                      f"sys.exit({code})\n", encoding="utf-8")
    return [sys.executable, str(script)]


def test_a_traced_command_agent_answers_from_its_trace_and_lists_its_calls(tmp_path):
    folder = tmp_path / "task"
    folder.mkdir()
    r = CommandAgent("pi", {"cmd": fake_cli(tmp_path, "pi-two-reads.jsonl"), "answer": "trace", "trace": "pi"}).run(
        task(folder), tmp_path / "log")
    assert r.answer == "4127" and not r.error and r.tool_calls == 2
    assert r.extra["tool_health"]["errors"] == 1 and r.extra["context"]["peak_prompt_tokens"] == 1709
    events = [json.loads(x)["event"] for x in Path(r.extra["calls_log"]).read_text(encoding="utf-8").splitlines()]
    assert [(e["type"], e["tool"]) for e in events] == [("tool_call", "read"), ("tool_result", "read")] * 2
    assert events[0]["args"] == {"path": "missing.txt"} and "4127" in events[3]["data"]


def test_a_cli_that_exits_0_after_a_failed_run_is_failed_by_its_trace(tmp_path):
    folder = tmp_path / "task"
    folder.mkdir()
    r = CommandAgent("pi", {"cmd": fake_cli(tmp_path, "pi-over-window.jsonl"), "answer": "trace", "trace": "pi"}).run(
        task(folder), tmp_path / "log")
    assert r.error.startswith("pi: Context overflow recovery failed") and r.error_kind == "failed" and r.answer == ""


def test_totals_only_or_an_unreadable_trace_writes_no_calls_log(tmp_path):
    folder = tmp_path / "task"
    folder.mkdir()
    oc = CommandAgent("openclaw", {"cmd": fake_cli(tmp_path, "openclaw-over-window.json"), "answer": "trace",
                                   "trace": "openclaw"}).run(task(folder), tmp_path / "log")
    assert oc.answer.startswith("Code word") and oc.tool_calls == 1 and "calls_log" not in oc.extra
    assert oc.extra["context"]["compactions"] == 1 and oc.extra["context"]["over_window"] == 1
    # A failed OpenClaw run's envelope has no toolSummary: still no list of calls, and the counts are unknown.
    failed = T.parse("openclaw", '{"ok": false, "status": "error", "final": "", "error": {"message": "400 request '
                                 '(8743 tokens) exceeds the available context size (8192 tokens)"}}')
    assert not failed.lists_calls and T.tool_health(failed)["calls"] is None and failed.answer == ""
    assert failed.failure and T.context(failed)["largest_rejected_tokens"] == 8743
    garbled = CommandAgent("pi", {"cmd": [sys.executable, "-c", "print('not json')"], "answer": "trace",
                                  "trace": "pi"}).run(task(folder), tmp_path / "log2")
    assert "calls_log" not in garbled.extra and garbled.tool_calls is None and garbled.answer == ""


def test_a_run_stopped_at_its_cap_keeps_its_trace_and_last_call(tmp_path):
    """The CLI printed two calls, then hung: what it printed before the cap is still read."""
    folder = tmp_path / "task"
    folder.mkdir()
    script = tmp_path / "hang.py"
    script.write_text("import sys, time, pathlib\n"
                      f"sys.stdout.write(pathlib.Path({str(TRACES / 'pi-two-reads.jsonl')!r}).read_text(encoding='utf-8'))\n"
                      "sys.stdout.flush()\ntime.sleep(60)\n", encoding="utf-8")
    spec = TaskSpec(id="t", prompt="Do it", folder=folder, timeout_s=3, needs=[])
    r = CommandAgent("pi", {"cmd": [sys.executable, str(script)], "answer": "trace", "trace": "pi"}).run(
        spec, tmp_path / "log")
    assert r.timed_out and r.error.startswith("stopped at the")
    assert r.extra["tool_health"]["last_call"] == 'read {"path": "notes.txt"}' and r.tool_calls == 2


def test_an_untraced_agent_still_gets_both_blocks(tmp_path):
    folder = tmp_path / "task"
    folder.mkdir()
    r = CommandAgent("plain", {"cmd": [sys.executable, "-c", "print('<tool_call>{}</tool_call>')"]}).run(
        task(folder), tmp_path / "log")
    assert r.extra["tool_health"]["malformed"] == 1 and r.extra["tool_health"]["calls"] is None
    assert r.tool_calls is None and "calls_log" not in r.extra


def test_an_entry_with_a_bad_trace_is_refused():
    with pytest.raises(ValueError, match="'trace' must be one of"):
        CommandAgent("x", {"cmd": ["x"], "trace": "xml"})
    with pytest.raises(ValueError, match="needs a 'trace' format"):
        CommandAgent("x", {"cmd": ["x"], "answer": "trace"})


@pytest.mark.parametrize("name, flags", [("opencode", ["--format", "json"]), ("pi", ["--mode", "json"]),
                                         ("openclaw", ["--json"]), ("hermes", ["--format", "stream-json"])])
def test_the_traced_entries_run_their_cli_in_json_mode(name, flags):
    spec = get_agent(name, "local/m").spec
    cmd = spec["cmd"]
    assert spec["trace"] == name and spec["answer"] == "trace"
    assert any(cmd[i:i + len(flags)] == flags for i in range(len(cmd)))


# ----------------------------------------------------------------- the runner
def test_the_audit_reads_a_traced_agents_calls(tmp_path, monkeypatch):
    log = tmp_path / "calls.jsonl"
    log.write_text(json.dumps({"event": {"type": "tool_call", "tool": "read", "args": {"path": "/x/answers/keys/a"}}})
                   + "\n" + json.dumps({"event": {"type": "tool_result", "tool": "read", "data": "hello"}}) + "\n",
                   encoding="utf-8")
    assert run_eval.audit_calls(log, ["answers/keys"]) and not run_eval.audit_calls(log, ["kw-keys"])
    assert run_eval.calls_results(log).strip() == "hello"
    assert run_eval.audit_calls(None, ["x"]) == [] and run_eval.calls_results(tmp_path / "gone.jsonl") == ""

    from agents.base import AgentResult

    monkeypatch.setattr(run_eval, "answer_audit", lambda *a: [])
    monkeypatch.setattr(run_eval, "protected_markers", lambda: ["answers/keys"])
    traced = AgentResult(answer="", wall_seconds=1, model="m", transcript=str(tmp_path / "pi.log"), extra={"calls_log": str(log)})
    assert run_eval.audit_task(tmp_path, "t", tmp_path, traced) and "audit" not in traced.extra
    totals = AgentResult(answer="", wall_seconds=1, model="m", transcript=str(tmp_path / "openclaw.log"))
    assert run_eval.audit_task(tmp_path, "t", tmp_path, totals) == []
    assert totals.extra["audit"] == "not audited: this agent's log does not list its tool calls"
    lost = AgentResult(answer="", wall_seconds=1, model="m", extra={"calls_log": str(tmp_path / "gone.jsonl")})
    run_eval.audit_task(tmp_path, "t", tmp_path, lost)
    assert lost.extra["audit"] == "not audited: the agent's tool-call log is missing"


def test_the_summary_lists_tool_call_and_context_trouble_only_where_there_is_some(tmp_path, monkeypatch):
    run_dir = tmp_path / "runs" / "pi-qwen" / "r1"
    graded = tmp_path / "graded" / "pi-qwen" / "r1"
    graded.mkdir(parents=True)
    monkeypatch.setenv("KW_GRADED_HOME", str(tmp_path / "graded"))
    (graded / "scorecard.json").write_text(json.dumps({
        "harness": "pi", "model": "qwen", "suite": "everyday", "axes": [],
        "tasks": [{"id": tid, "passed": True, "why": "", "wall_seconds": 1.0, "answer_set": "public", "judge": {}}
                  for tid in ("a", "b")]}), encoding="utf-8")
    results = {"a": {"tool_health": {"calls": 4, "errors": 0, "malformed": 2, "loops": []},
                     "context": {"window": 32768, "peak_prompt_tokens": 31000, "peak_share": 0.946, "compactions": 0}},
               "b": {"tool_health": {"calls": 4, "errors": 0, "malformed": 0, "loops": []},
                     "context": {"window": 32768, "peak_prompt_tokens": 9000, "peak_share": 0.275}}}
    for tid, r in results.items():
        (run_dir / ".records" / tid).mkdir(parents=True)
        (run_dir / ".records" / tid / "result.json").write_text(json.dumps(r), encoding="utf-8")
    text = run_eval.summarize(run_dir, {"framework": "x", "tasks": "t", "commit": "c", "dirty": False})
    assert "Tool calls that went wrong (from the agent's output; not scored):\n- a: 2 malformed tool calls\n" in text
    assert "Context window (not scored):\n- a: peak context 31.0K of 32.8K (95%)\n" in text
    assert "- b:" not in text


def test_a_run_records_both_blocks_and_merges_the_servers_context(tmp_path, monkeypatch, capsys):
    from agents.base import Agent, AgentResult

    class Fake(Agent):
        name = "fake"

        def run(self, task, log_dir):
            t = T.parse("pi", read("pi-over-window.jsonl"))
            return AgentResult(answer="", wall_seconds=3.0, model="m",
                               extra={"tool_health": T.tool_health(t), "context": T.context(t)})

    class Tel:
        def start(self):
            return self

        def stop(self):
            return {"server": {}, "gpu": {}, "faults": {},
                    "context": {"window": 16384, "peak_prompt_tokens": 1600, "requests": 1, "reason": None}}

    monkeypatch.setattr(run_eval, "EVAL_HOME", tmp_path / "home")
    monkeypatch.setattr(run_eval, "get_agent", lambda name, model: Fake("m"))
    monkeypatch.setattr(run_eval.telemetry, "Telemetry", Tel)
    monkeypatch.setattr(run_eval.telemetry, "line", lambda block: "telemetry: x")
    run_dir = run_eval.run("fake", "m", "verify", ["verify-02"], tmp_path / "out", tmp_path / "fx", "m",
                           do_grade=False, measure=True)
    result = json.loads((run_dir / ".records" / "verify-02" / "result.json").read_text(encoding="utf-8"))
    assert result["context"]["window"] == 16384 and result["context"]["peak_prompt_tokens"] == 56599
    assert result["tool_health"]["source"] == "pi"
    assert "verify-02: 2 requests over the window" in capsys.readouterr().out


# ----------------------------------------------------------------- the server side (--telemetry)
LLAMA = {"pid": 1, "kind": "llama-server", "port": 8080}


def fake_http(monkeypatch, answers):
    """engine_info.fetch answers (port, path) with the next of *answers*' replies, the last one from then on."""
    def fetch(port, path):
        replies = answers.get((port, path))
        if not replies:
            return None
        return json.dumps(replies.pop(0) if len(replies) > 1 else replies[0])
    monkeypatch.setattr(telemetry.engine_info, "fetch", fetch)


def test_the_window_is_read_from_each_server(monkeypatch):
    fake_http(monkeypatch, {(8080, "/slots"): [[{"id": 0, "n_ctx": 16384}]],
                            (8000, "/v1/models"): [{"data": [{"id": "q", "max_model_len": 8192}]}]})
    assert telemetry.context_window(LLAMA) == (16384, None)
    assert telemetry.context_window({"kind": "vllm", "port": 8000}) == (8192, None)
    fake_http(monkeypatch, {(8080, "/props"): [{"default_generation_settings": {"n_ctx": 32768}}]})
    assert telemetry.context_window(LLAMA) == (32768, None)  # /slots off (--no-slots)
    fake_http(monkeypatch, {})
    assert telemetry.context_window(LLAMA)[0] is None and telemetry.context_window(None)[1] == "no model server found"
    assert "isn't read" in telemetry.context_window({"kind": "lm-studio", "port": 1234})[1]


def test_the_slot_watcher_counts_only_this_tasks_requests(monkeypatch):
    before = [{"id": 0, "id_task": 7, "n_prompt_tokens": 60000}, {"id": 1, "id_task": 3, "n_prompt_tokens": 900}]
    during = [{"id": 0, "id_task": 7, "n_prompt_tokens": 60000}, {"id": 1, "id_task": 12, "n_prompt_tokens": 2319}]
    after = [{"id": 0, "id_task": 15, "n_prompt_tokens": 4100}, {"id": 1, "id_task": 12, "n_prompt_tokens": 2319}]
    fake_http(monkeypatch, {(8080, "/slots"): [before, during, after]})
    w = telemetry.SlotWatcher(8080, interval_s=3600).start()
    w.poll()
    w.stop()  # polls once more: the request that ended since the last tick
    block = telemetry.context_block(LLAMA, 16384, None, w)
    assert block == {"window": 16384, "peak_prompt_tokens": 4100, "requests": 2, "reason": None}


def test_a_server_that_started_during_the_task_still_gives_its_window(monkeypatch):
    """Lemonade starts its llama-server on the first request: none was there when the task began."""
    found = iter([(None, None, "no llama-server, vLLM or SGLang running"), (LLAMA, None, "no /metrics")])
    monkeypatch.setattr(telemetry, "find_server", lambda: next(found))
    monkeypatch.setattr(telemetry, "pick_sampler", lambda pid, interval: (None, "no GPU"))
    monkeypatch.setattr(telemetry, "read_faults", lambda start, end: telemetry.fault_block([], None, "n/a"))
    fake_http(monkeypatch, {(8080, "/slots"): [[{"id": 0, "n_ctx": 65536}]]})
    block = telemetry.Telemetry().start().stop()
    assert block["context"]["window"] == 65536


def test_no_slots_endpoint_is_a_reason_not_a_crash(monkeypatch):
    fake_http(monkeypatch, {})
    w = telemetry.SlotWatcher(8080).start()
    w.stop()
    assert "doesn't answer /slots" in telemetry.context_block(LLAMA, None, "x", w)["reason"]
    assert "doesn't report each request" in telemetry.context_block({"kind": "vllm"}, 8192, None, None)["reason"]
