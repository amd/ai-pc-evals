# Copyright(C) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""What an agent's own output says about its tool calls and its context window, per task (#71, #72).

An agents.json entry names the structured output its CLI prints with "trace" (and runs the CLI in that mode):

    "opencode"   `opencode run --format json`: one JSON event per line (tool_use, text, step_finish, error)
    "pi"         `pi -p --mode json`: one JSON event per line (message_end, tool_execution_end, compaction_*)
    "openclaw"   `openclaw agent exec --json`: one JSON envelope at the end (the final answer, a tool summary)
    "hermes"     `hermes chat --format stream-json`: one JSON event per line (tool_use, tool_result, text, result)

Claude Code's stream-json is read the same way ("claude-code"). parse() turns any of them into a Trace: each tool
call with its arguments and whether it failed, the assistant's text, each model request's prompt size, the
compactions and the run's own error. An agent with no trace is still checked from what it printed: raw tool-call
markup, and the servers' "over the context window" errors.

tool_health() and context() turn a Trace into the result.json blocks. Neither ever changes a score.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional

FORMATS = ("opencode", "pi", "openclaw", "hermes", "claude-code")
#: Outputs that never list each call: at most totals (OpenClaw's envelope), or nothing (an agent with no trace).
TOTALS_ONLY = ("openclaw", "printed")
#: The same tool with the same arguments this many times in a row is a loop.
LOOP_MIN = 3
#: Context use at or above this share of the window is flagged in summary.md.
NEAR_FULL = 0.9

#: Tool-call markup a model wrote as text: the server didn't parse it into a call (no --jinja, the wrong
#: --tool-call-parser, a chat template without tools), so the agent saw prose and the tool never ran.
MARKUP = re.compile(
    r"<tool_call>|<\|tool_call\|>|<\|tool[ _▁]call[ _▁]begin\|>|<function=[\w.-]+>|<\|python_tag\|>"
    r"|\[TOOL_CALLS\]|<tool_code>|<\|channel\|>commentary to=functions\.")
#: A whole line that is a bare JSON call: {"name": ..., "arguments": ...} (or "parameters").
BARE_CALL = re.compile(r'^\s*\{\s*"name"\s*:\s*"[\w.-]+"\s*,\s*"(?:arguments|parameters)"\s*:', re.M)

#: A model server refusing a request longer than its window, as the agent printed it. Groups: the request's
#: tokens and the window, where the message gives them.
OVER_WINDOW = [
    # llama-server: "request (56482 tokens) exceeds the available context size (16384 tokens)"
    re.compile(r"request \((?P<tokens>\d+) tokens\) exceeds the available context size \((?P<window>\d+) tokens\)"),
    re.compile(r"exceed_context_size_error"),
    # vLLM 0.29: "This model's maximum context length is 8192 tokens. However, you requested 5 output tokens and
    # your prompt contains at least 8188 input tokens"; older: "However, you requested 40000 tokens"
    re.compile(r"maximum context length is (?P<window>\d+) tokens\..{0,80}?your prompt contains at least "
               r"(?P<tokens>\d+) input tokens"),
    re.compile(r"maximum context length is (?P<window>\d+) tokens\. However, you requested (?P<tokens>\d+) tokens"),
    re.compile(r"maximum context length is (?P<window>\d+) tokens"),
    # SGLang: "The input (40000 tokens) is longer than the model's context length (32768 tokens)"
    re.compile(r"input \((?P<tokens>\d+) tokens\) is longer than the model's context length \((?P<window>\d+) tokens\)"),
    # Agents' own words for it.
    re.compile(r"[Cc]ontext overflow|context_length_exceeded|ContextOverflow|prompt is too long"),
]


@dataclass
class Call:
    tool: str
    args: Any
    error: bool = False
    result: str = ""

    def key(self) -> str:
        return self.tool + " " + json.dumps(self.args, sort_keys=True, ensure_ascii=False, default=str)

    def short(self) -> str:
        args = json.dumps(self.args, ensure_ascii=False, default=str)
        return f"{self.tool} {args[:120]}{'…' if len(args) > 120 else ''}"


@dataclass
class Trace:
    format: str
    calls: List[Call] = field(default_factory=list)
    #: Tool calls and failures when the agent reports only totals (OpenClaw's envelope), else None.
    call_total: Optional[int] = None
    error_total: Optional[int] = None
    #: The assistant's text, one entry per message or part, in order.
    texts: List[str] = field(default_factory=list)
    #: Each model request's prompt in tokens (uncached + cache reads + cache writes), where the agent reports it.
    prompts: List[int] = field(default_factory=list)
    compactions: int = 0
    #: The model server's or the agent's messages about a request over the context window.
    over_window: List[str] = field(default_factory=list)
    answer: Optional[str] = None
    #: How many events the output held: none means it lists no calls, not that the agent made none.
    events: int = 0
    #: Why the run ended in an error, when the agent's output says so (its exit code may still be 0).
    failure: str = ""

    @property
    def n_calls(self) -> int:
        return self.call_total if self.call_total is not None else len(self.calls)

    @property
    def lists_calls(self) -> bool:
        """Each call is in the trace (not only totals), and the output parsed."""
        return self.format not in TOTALS_ONLY and self.events > 0 and self.call_total is None

    @property
    def n_errors(self) -> int:
        return self.error_total if self.error_total is not None else sum(c.error for c in self.calls)


def _lines(text: str) -> Iterable[Dict[str, Any]]:
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if isinstance(ev, dict):
            yield ev


def _text(content: Any) -> str:
    """Text out of a string or a list of content blocks ({"type": "text", "text": ...})."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(c.get("text", "") for c in content if isinstance(c, dict) and isinstance(c.get("text"), str))
    return ""


def _int(x: Any) -> int:
    return int(x) if isinstance(x, (int, float)) and not isinstance(x, bool) else 0


#: The forms that name the refused request's size (or llama-server's error type): safe to look for in a reply,
#: where "maximum context length is 32768 tokens" can be the answer to a question about a model.
STRICT = [rx for rx in OVER_WINDOW if "tokens" in rx.groupindex or "exceed_context" in rx.pattern]


def _over(text: str, trace: Trace, strict: bool = False) -> None:
    """Keep the error around its match: a log line can put it after a long prefix."""
    for rx in STRICT if strict else OVER_WINDOW:
        m = rx.search(text)
        if m:
            trace.over_window.append(" ".join(text[max(0, m.start() - 80):m.end() + 160].split()))
            return


def _opencode(stdout: str) -> Trace:
    t = Trace("opencode")
    for ev in _lines(stdout):
        t.events += 1
        part = ev.get("part") if isinstance(ev.get("part"), dict) else {}
        kind = ev.get("type")
        if kind == "tool_use":
            state = part.get("state") if isinstance(part.get("state"), dict) else {}
            failed = state.get("status") == "error"
            t.calls.append(Call(str(part.get("tool") or ""), state.get("input"), failed,
                                str(state.get("error") if failed else state.get("output") or "")))
        elif kind == "text" and isinstance(part.get("text"), str):
            t.texts.append(part["text"])
        elif kind == "step_finish":
            tok = part.get("tokens") if isinstance(part.get("tokens"), dict) else {}
            cache = tok.get("cache") if isinstance(tok.get("cache"), dict) else {}
            prompt = _int(tok.get("input")) + _int(cache.get("read")) + _int(cache.get("write"))
            if prompt:
                t.prompts.append(prompt)
        elif kind == "error":
            err = ev.get("error")
            msg = json.dumps(err, ensure_ascii=False) if isinstance(err, (dict, list)) else str(err or ev)
            t.failure = " ".join(msg.split())[:300]
            _over(msg, t)
        elif kind in ("compaction", "session.compacted") or part.get("type") == "compaction":
            t.compactions += 1
    t.answer = "\n".join(x for x in t.texts if x.strip())  # what `opencode run` prints in text mode
    return t


def _pi(stdout: str) -> Trace:
    t = Trace("pi")
    calls: Dict[str, Call] = {}
    last: Optional[Dict[str, Any]] = None
    compaction_error = ""
    for ev in _lines(stdout):
        t.events += 1
        kind = ev.get("type")
        if kind == "message_end" and isinstance(ev.get("message"), dict):
            m = ev["message"]
            if m.get("role") == "assistant":
                last = m
                content = m.get("content") if isinstance(m.get("content"), list) else []
                for c in content:
                    if not isinstance(c, dict):
                        continue
                    if c.get("type") == "toolCall":
                        call = Call(str(c.get("name") or ""), c.get("arguments"))
                        calls[str(c.get("id"))] = call
                        t.calls.append(call)
                    elif c.get("type") == "text" and isinstance(c.get("text"), str):
                        t.texts.append(c["text"])
                usage = m.get("usage") if isinstance(m.get("usage"), dict) else {}
                prompt = _int(usage.get("input")) + _int(usage.get("cacheRead")) + _int(usage.get("cacheWrite"))
                if prompt:
                    t.prompts.append(prompt)
                if m.get("stopReason") == "error" and m.get("errorMessage"):
                    _over(str(m["errorMessage"]), t)
            elif m.get("role") == "toolResult":
                call = calls.get(str(m.get("toolCallId")))
                if call is not None:
                    call.error = bool(m.get("isError"))
                    call.result = _text(m.get("content"))
        elif kind == "compaction_end":
            if ev.get("errorMessage"):
                compaction_error = " ".join(str(ev["errorMessage"]).split())[:300]
            elif not ev.get("aborted"):
                t.compactions += 1
    if last is not None:
        t.answer = _text([c for c in last.get("content") or [] if isinstance(c, dict) and c.get("type") == "text"])
        if last.get("stopReason") in ("error", "aborted"):  # `pi -p` exits 1 with nothing on stdout here
            t.answer = ""
            t.failure = compaction_error or " ".join(str(last.get("errorMessage") or last["stopReason"]).split())[:300]
    # A failed compaction the run got past (its last message answered) is not the run's failure: the task is graded
    # on its reply, as in text mode. One it didn't get past is.
    if compaction_error and not t.failure and not (t.answer or "").strip():
        t.failure = compaction_error
    return t


def _openclaw(stdout: str) -> Trace:
    """The envelope is one pretty-printed JSON object after any log lines."""
    t = Trace("openclaw")
    start = stdout.find("{\n")
    start = start if start >= 0 else stdout.find("{")
    try:
        env = json.loads(stdout[start:]) if start >= 0 else None
    except ValueError:
        env = None
    if not isinstance(env, dict):
        t.failure = "no JSON envelope in OpenClaw's output"
        return t
    t.events = 1
    summary = env.get("toolSummary") if isinstance(env.get("toolSummary"), dict) else {}
    if "calls" in summary:
        t.call_total, t.error_total = _int(summary.get("calls")), _int(summary.get("failures"))
    final = env.get("final")
    t.answer = final if isinstance(final, str) else _text(env.get("payloads"))
    if t.answer:
        t.texts.append(t.answer)
    if env.get("ok") is False or env.get("status") not in (None, "ok"):
        t.failure = " ".join(str(env.get("error") or env.get("status") or "not ok").split())[:300]
        _over(t.failure, t)
    return t


def _hermes(stdout: str) -> Trace:
    """tool_use and tool_result events paired by tool_call_id, the reply streamed as text events, and a result
    event with the whole reply and the run's exit code. Token counts are only the run's totals."""
    t = Trace("hermes")
    calls: Dict[str, Call] = {}
    streamed: List[str] = []
    for ev in _lines(stdout):
        t.events += 1
        kind = ev.get("type")
        if kind == "tool_use":
            call = Call(str(ev.get("name") or ""), ev.get("input"))
            calls[str(ev.get("tool_call_id"))] = call
            t.calls.append(call)
        elif kind == "tool_result":
            call = calls.get(str(ev.get("tool_call_id")))
            if call is not None:
                call.error = bool(ev.get("is_error"))
                call.result = str(ev.get("output") or "")
        elif kind == "text" and isinstance(ev.get("text"), str):
            streamed.append(ev["text"])
        elif kind == "result":
            text = ev.get("text") if isinstance(ev.get("text"), str) else "".join(streamed)
            if ev.get("exit_code") not in (0, None):  # a turn that didn't finish: its text is the error, not a reply
                t.failure = " ".join(text.split())[:300] or f"exit code {ev.get('exit_code')}"
                _over(text, t)
                text = ""
            t.answer = text
    if t.answer is None and streamed:  # cut off before the result event (a time cap)
        t.answer = "".join(streamed)
    t.texts.append("".join(streamed))
    return t


def _claude_code(stdout: str) -> Trace:
    t = Trace("claude-code")
    calls: Dict[str, Call] = {}
    last_id = None
    for ev in _lines(stdout):
        t.events += 1
        msg = ev.get("message") if isinstance(ev.get("message"), dict) else {}
        content = msg.get("content") if isinstance(msg.get("content"), list) else []
        if ev.get("type") == "assistant":
            for c in content:
                if not isinstance(c, dict):
                    continue
                if c.get("type") == "tool_use":
                    call = Call(str(c.get("name") or ""), c.get("input"))
                    calls[str(c.get("id"))] = call
                    t.calls.append(call)
                elif c.get("type") == "text" and isinstance(c.get("text"), str):
                    t.texts.append(c["text"])
            usage = msg.get("usage") if isinstance(msg.get("usage"), dict) else {}
            prompt = (_int(usage.get("input_tokens")) + _int(usage.get("cache_read_input_tokens"))
                      + _int(usage.get("cache_creation_input_tokens")))
            # One API response streams as several assistant events with the same usage: count it once.
            if prompt and (msg.get("id") is None or msg.get("id") != last_id):
                t.prompts.append(prompt)
            last_id = msg.get("id")
        elif ev.get("type") == "user":
            for c in content:
                if isinstance(c, dict) and c.get("type") == "tool_result":
                    call = calls.get(str(c.get("tool_use_id")))
                    if call is not None:
                        call.error = bool(c.get("is_error"))
                        call.result = _text(c.get("content"))
        elif ev.get("type") == "system" and ev.get("subtype") == "compact_boundary":
            t.compactions += 1
        elif ev.get("type") == "result" and isinstance(ev.get("result"), str):
            t.answer = ev["result"]
            if ev.get("is_error"):
                _over(ev["result"], t)
    return t


def parse(fmt: str, stdout: str) -> Trace:
    """The agent's structured output (*fmt* one of FORMATS) as a Trace. Never raises: an output that doesn't parse
    is a Trace with no calls and the reason in ``failure``."""
    parser = {"opencode": _opencode, "pi": _pi, "openclaw": _openclaw, "hermes": _hermes,
              "claude-code": _claude_code}.get(fmt)
    if parser is None:
        raise ValueError(f"unknown trace format {fmt!r}: one of {', '.join(FORMATS)}")
    try:
        return parser(stdout)
    except Exception as e:  # noqa: BLE001 - a malformed trace must not lose the task's result
        return Trace(fmt, failure=f"the {fmt} output could not be read: {type(e).__name__}: {e}")


def scan_printed(stdout: str) -> Trace:
    """For an agent with no trace: what its reply shows. Markup, and a refused request's size (strict forms only:
    stdout is the answer); scan_stderr adds what it logged."""
    t = Trace("printed")
    t.texts.append(stdout)
    for line in stdout.splitlines():
        _over(line, t, strict=True)
    return t


#: An agent's log line for a history it compacted, for agents whose structured output doesn't report it.
COMPACTED = [re.compile(r"auto-compaction succeeded")]  # OpenClaw


def scan_stderr(trace: Trace, stderr: str) -> Trace:
    """Add what the agent logged on stderr: window errors, and compactions its trace didn't report."""
    compacted = 0
    for line in stderr.splitlines():
        _over(line, trace)
        compacted += any(rx.search(line) for rx in COMPACTED)
    if not trace.compactions:
        trace.compactions = compacted
    return trace


def malformed(texts: Iterable[str]) -> List[str]:
    """Raw tool-call markup in the assistant's text: one short excerpt per call. Only a call's opening marker
    counts, and a bare JSON call right after one is that call's body."""
    found = []
    for text in texts:
        opens = [m.start() for m in MARKUP.finditer(text)]
        bare = [m.start() for m in BARE_CALL.finditer(text)
                if not any(0 <= m.start() - o <= 200 for o in opens)]
        found += [" ".join(text[i:i + 100].split()) for i in sorted(opens + bare)]
    return found


def loops(calls: List[Call], n: int = LOOP_MIN) -> List[Dict[str, Any]]:
    """Runs of *n* or more identical calls in a row (same tool, same arguments)."""
    out, i = [], 0
    while i < len(calls):
        j = i
        while j + 1 < len(calls) and calls[j + 1].key() == calls[i].key():
            j += 1
        if j - i + 1 >= n:
            out.append({"tool": calls[i].tool, "times": j - i + 1, "call": calls[i].short()})
        i = j + 1
    return out


def tool_health(trace: Trace, timed_out: bool = False) -> Dict[str, Any]:
    """The result.json ``tool_health`` block. ``calls``/``errors`` are None when the agent's output doesn't list
    its tool calls; ``malformed`` is always checked (in the trace's text, or in what the agent printed)."""
    known = trace.lists_calls or trace.call_total is not None
    bad = malformed(trace.texts)
    block: Dict[str, Any] = {
        "source": trace.format,
        "calls": trace.n_calls if known else None,
        "errors": trace.n_errors if known else None,
        "malformed": len(bad),
        "malformed_examples": bad[:3],
        "loops": loops(trace.calls),
        "last_call": trace.calls[-1].short() if timed_out and trace.calls else None,
    }
    if trace.call_total is not None:
        block["note"] = "totals only: this agent's output doesn't list each call, so loops can't be seen"
    return block


def context(trace: Trace, window: Optional[int] = None) -> Dict[str, Any]:
    """The result.json ``context`` block from the agent's side: the largest prompt it sent, the window when a
    server error or the caller (*window*, from the server) gives it, rejections and compactions.

    An agent logs one rejection in several lines, so rejections are counted by the request sizes the errors
    name (one when none names a size)."""
    tokens, windows = set(), []
    for s in trace.over_window:
        for rx in OVER_WINDOW:
            m = rx.search(s)
            if m and m.groupdict().get("tokens"):
                tokens.add(int(m.group("tokens")))
            if m and m.groupdict().get("window"):
                windows.append(int(m.group("window")))
    window = window or (max(windows) if windows else None)
    sent = trace.prompts + sorted(tokens)  # a rejected request was still sent, and is the one that mattered
    peak = max(sent) if sent else None
    return {
        "window": window,
        "peak_prompt_tokens": peak,
        "peak_share": round(peak / window, 3) if peak and window else None,
        "requests": len(trace.prompts) or None,
        "over_window": len(tokens) or int(bool(trace.over_window)),
        "largest_rejected_tokens": max(tokens) if tokens else None,
        "compactions": trace.compactions,
    }


def with_server(block: Dict[str, Any], server: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """The agent's context block with what --telemetry read from the model server (its window, and llama-server's
    largest prompt): the server's window wins, the larger peak wins, and the agent's request count."""
    if not server:
        return block
    out = dict(block)
    out["window"] = server.get("window") or block.get("window")
    peaks = [p for p in (block.get("peak_prompt_tokens"), server.get("peak_prompt_tokens")) if p]
    out["peak_prompt_tokens"] = max(peaks) if peaks else None
    out["requests"] = block.get("requests") or server.get("requests")
    out["peak_share"] = (round(out["peak_prompt_tokens"] / out["window"], 3)
                         if out["peak_prompt_tokens"] and out["window"] else None)
    if server.get("reason"):
        out["server_reason"] = server["reason"]
    return out


def health_line(task_id: str, block: Dict[str, Any]) -> str:
    """summary.md's line for a task whose tool calls went wrong; "" when nothing is off."""
    bits = []
    if block.get("malformed"):
        bits.append(f"{block['malformed']} malformed tool call{'s' * (block['malformed'] != 1)}")
    if block.get("errors"):
        bits.append(f"{block['errors']} of {block['calls']} tool calls failed")
    for lp in block.get("loops") or []:
        bits.append(f"loop ({lp['tool']} x{lp['times']})")
    if block.get("last_call"):
        bits.append(f"last call before the cap: {block['last_call']}")
    return f"{task_id}: " + ", ".join(bits) if bits else ""


def context_line(task_id: str, block: Dict[str, Any]) -> str:
    """summary.md's line for a task that came close to (or went over) the window; "" otherwise."""
    bits = []
    peak, window = block.get("peak_prompt_tokens"), block.get("window")
    share = peak / window if peak and window else block.get("peak_share")
    if block.get("over_window"):
        rejected = block.get("largest_rejected_tokens")
        bits.append(f"{block['over_window']} request{'s' * (block['over_window'] != 1)} over the window"
                    + (f" (largest {_k(rejected)}" + (f" of {_k(window)})" if window else ")") if rejected else ""))
    if share is not None and share >= NEAR_FULL:
        bits.append(f"peak context {_k(peak)} of {_k(window)} ({share:.0%})")
    if block.get("compactions"):
        n = block["compactions"]
        bits.append(f"history compacted {'once' if n == 1 else 'twice' if n == 2 else f'{n} times'}")
    return f"{task_id}: " + ", ".join(bits) if bits else ""


def calls_brief(block: Dict[str, Any]) -> str:
    """The console's tool-call line when nothing is off: "2 (0 failed)", "" when the calls aren't known."""
    return "" if block.get("calls") is None else f"{block['calls']} ({block.get('errors') or 0} failed)"


def context_brief(block: Dict[str, Any]) -> str:
    """The console's context line when nothing is off: "peak 3.1K of 65.5K (5%)", "" when nothing was measured."""
    peak, window = block.get("peak_prompt_tokens"), block.get("window")
    if not peak:
        return ""
    return f"peak {_k(peak)}" + (f" of {_k(window)} ({peak / window:.0%})" if window else "")


def _k(n: Optional[int]) -> str:
    return "?" if n is None else f"{n / 1000:.1f}K" if n >= 1000 else str(n)
