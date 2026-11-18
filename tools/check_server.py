# Copyright(C) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""Is the local model server set up for an agent run? Run it once before the first run on a new setup.

usage: python tools/check_server.py [--base-url URL] [--model ID]
       python tools/run_eval.py ... --check-server        (the same checks before the first task)

One ``key: value`` line per check, with the fix when one fails:

    server      the server answers at the URL and its /models lists the model
    context     the window one request gets is at least 65,536 tokens (llama-server: a slot's n_ctx from /slots,
                else /props; vLLM and SGLang: max_model_len; Lemonade: its llama-server's, else its ctx_size)
    tool calls  one chat request with one tool comes back as a structured tool call, not as text
    metrics     /metrics answers with llama-server's, vLLM's or SGLang's counters (only --telemetry needs it;
                for Lemonade, the llama-server it runs)

The URL is --base-url, else $KW_LOCAL_BASE_URL (where the local agents are pointed), else the one model server
running on this PC, found as telemetry finds it (tools/local_servers.py; vLLM and SGLang also on their default
ports). The model is --model (an agent's provider/model, such as lmstudio/qwen/qwen3.8-27b, matches the server's
qwen/qwen3.8-27b), else the one model the server lists (Lemonade: the one it has loaded). A server that needs a key
gets $KW_LOCAL_API_KEY. Exit 0 when no check fails, 1 otherwise.

It installs, starts and loads nothing: a model Lemonade lists but hasn't loaded fails the check instead of being
loaded (with a small default window) by the test request.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import local_servers  # noqa: E402
import telemetry  # noqa: E402
from agents import trace as trace_mod  # noqa: E402

#: Below this, OpenCode stops without a word when the window fills and Hermes refuses to start a turn.
MIN_CONTEXT = 65_536
TIMEOUT_S = 10
#: The tool-call request: a thinking model may reason for a while before it calls.
CHAT_TIMEOUT_S = 300
CHAT_MAX_TOKENS = 2048
#: The OpenAI base path of each kind of server, after its host and port.
BASE_PATHS = {"lemonade": "/api/v1"}
LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1", "0.0.0.0")
#: /models' owned_by -> kind of server.
OWNERS = {"llamacpp": "llama-server", "vllm": "vllm", "sglang": "sglang", "lemonade": "lemonade"}
TOOL = {"type": "function", "function": {
    "name": "get_weather", "description": "Get the current weather in a city.",
    "parameters": {"type": "object", "properties": {"city": {"type": "string", "description": "The city"}},
                   "required": ["city"]}}}
PROMPT = "What is the weather in Paris right now? Use the get_weather tool."
#: How to fix each failing check, by kind of server.
TOOL_FIX = {"llama-server": "start llama-server with --jinja (recent builds have it on unless --no-jinja is given)",
            "vllm": "start vLLM with --enable-auto-tool-choice --tool-call-parser <the model's parser>",
            "sglang": "start SGLang with --tool-call-parser <the model's parser>",
            "lemonade": "Lemonade starts its llama-server with --jinja: check that the model can call tools"}
CONTEXT_FIX = {"llama-server": "restart llama-server with -c 65536 or more",
               "vllm": "restart vLLM with --max-model-len 65536 or more",
               "sglang": "restart SGLang with --context-length 65536 or more",
               "lemonade": "reload it with lemonade load {model} --ctx-size 65536"}
METRICS_FIX = {"llama-server": "start llama-server with --metrics", "sglang": "start SGLang with --enable-metrics",
               "vllm": telemetry.NO_METRICS["vllm"], "lemonade": "Lemonade's llama-server has no /metrics"}
#: When the server's kind is unknown: every server's fix.
ANY = "llama-server: {}; vLLM: {}; SGLang: {}"


@dataclass
class Check:
    key: str
    status: str  # "ok", "FAIL", or "note" (printed, not a failure: not checked, unknown, not needed)
    text: str

    @property
    def value(self) -> str:
        return f"ok ({self.text})" if self.status == "ok" else (
            f"FAIL: {self.text}" if self.status == "FAIL" else self.text)

    def line(self) -> str:
        """``key: value``, values aligned as run_eval.py prints its lines."""
        return f"{(self.key + ':').ljust(11)} {self.value}"


def _fix(table: Dict[str, str], kind: Optional[str], **fill: str) -> str:
    if kind in table:
        return table[kind].format(**fill)
    return ANY.format(table["llama-server"], table["vllm"], table["sglang"])


def is_local(url: str) -> bool:
    return (urllib.parse.urlsplit(url).hostname or "") in LOCAL_HOSTS


def root(url: str) -> str:
    """http://host:port of a base URL."""
    parts = urllib.parse.urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def request(url: str, body: Optional[Dict[str, Any]] = None, key: Optional[str] = None,
            timeout: float = TIMEOUT_S) -> Tuple[Optional[int], str]:
    """HTTP status and body of a GET (or a JSON POST), or (None, why) when nothing answered. A server on this PC is
    asked directly, never through a proxy that HTTP_PROXY names."""
    headers = {"Content-Type": "application/json"} if body is not None else {}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(url, json.dumps(body).encode() if body is not None else None, headers)
    handlers = [urllib.request.ProxyHandler({})] if is_local(url) else []
    try:
        with urllib.request.build_opener(*handlers).open(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")
    except (OSError, ValueError) as e:  # refused, timed out, a malformed URL
        return None, str(getattr(e, "reason", None) or e)


def get_json(url: str, key: Optional[str] = None) -> Any:
    status, text = request(url, key=key)
    try:
        return json.loads(text) if status == 200 else None
    except ValueError:  # an HTML page (Lemonade's app answers paths it doesn't serve)
        return None


def find_base_url(servers: Optional[List[Dict[str, Any]]] = None) -> Tuple[Optional[str], str]:
    """The OpenAI base URL of the one model server running on this PC, and where it came from (or why there is none).
    A llama-server started with --embeddings serves an embedding model, and one in Lemonade's install is Lemonade's
    own: neither is the server an agent talks to."""
    servers = local_servers.list_servers() if servers is None else servers
    servers = [s for s in servers if not s.get("embeddings") and s.get("port")]
    if any(s["kind"] == "lemonade" for s in servers):
        servers = [s for s in servers if not (s["kind"] == "llama-server" and "lemonade" in (s.get("path") or "").lower())]
    found = list({s["port"]: s for s in reversed(servers)}.values())[::-1]  # one per port: Ollama's app and server
    if not found:
        found = [s for s, _ in telemetry.probe()]  # vLLM or SGLang in Docker Desktop, or psutil missing
    if len(found) == 1:
        s = found[0]
        return f"http://127.0.0.1:{s['port']}{BASE_PATHS.get(s['kind'], '/v1')}", f"the {s['kind']} running here"
    if not found:
        return None, "no model server is running on this PC: start one, or pass --base-url (or set KW_LOCAL_BASE_URL)"
    return None, (f"several model servers are running ({', '.join(local_servers.label(s) for s in found)}): "
                  "pass --base-url (or set KW_LOCAL_BASE_URL) to say which one the agent uses")


def match_model(wanted: str, ids: List[str]) -> Optional[str]:
    """The listed id that is ``wanted``, or that ``wanted`` ends with after a slash (an agent's provider/model)."""
    if wanted in ids:
        return wanted
    tails = [i for i in ids if wanted.endswith("/" + i)]
    return max(tails, key=len) if tails else None


def slot_window(base_root: str) -> Tuple[Optional[int], Optional[int]]:
    """A llama-server slot's n_ctx (/slots, else /props) and its number of slots."""
    slots = get_json(base_root + "/slots")
    props = get_json(base_root + "/props")
    total = props.get("total_slots") if isinstance(props, dict) else None
    if isinstance(slots, list) and slots and isinstance(slots[0], dict) and slots[0].get("n_ctx"):
        return int(slots[0]["n_ctx"]), total or len(slots)
    gen = (props or {}).get("default_generation_settings") if isinstance(props, dict) else None
    n = gen.get("n_ctx") if isinstance(gen, dict) else None
    return (int(n) if n else None), total


def check_context(kind: Optional[str], base: str, model: str, entry: Dict[str, Any],
                  loaded: Optional[Dict[str, Any]]) -> Check:
    n, where, slots = entry.get("max_model_len"), "max_model_len", None
    if not n and loaded:  # Lemonade: its llama-server's slot, else what it was loaded with
        backend = loaded.get("backend_url")
        n, slots = slot_window(root(backend)) if backend else (None, None)
        where = "its llama-server's slot n_ctx"
        if not n:
            n, where = (loaded.get("recipe_options") or {}).get("ctx_size"), "its ctx_size"
    if not n and kind in (None, "llama-server"):
        n, slots = slot_window(root(base))
        where = "slot n_ctx"
    if not n:
        return Check("context", "note", f"unknown: {base} doesn't report its window (only llama-server, vLLM, SGLang "
                                        f"and Lemonade are read): make sure it is {MIN_CONTEXT:,} tokens or more")
    n = int(n)
    if n >= MIN_CONTEXT:
        return Check("context", "ok", f"{n:,} tokens, {where}")
    shared = (f"; it has {slots} slots, and without --kv-unified each gets its share of -c"
              if slots and slots > 1 and kind != "lemonade" else "")
    return Check("context", "FAIL", f"{n:,} tokens ({where}), under {MIN_CONTEXT:,}: OpenCode stops without a word "
                                    f"when it fills and Hermes won't start; {_fix(CONTEXT_FIX, kind, model=model)}{shared}")


def check_tool_call(kind: Optional[str], base: str, model: str, key: Optional[str]) -> Check:
    body = {"model": model, "messages": [{"role": "user", "content": PROMPT}], "tools": [TOOL],
            "max_tokens": CHAT_MAX_TOKENS}
    status, text = request(base + "/chat/completions", body, key, timeout=CHAT_TIMEOUT_S)
    fix = _fix(TOOL_FIX, kind)
    if status is None:
        return Check("tool calls", "FAIL", f"no reply to a chat request in {CHAT_TIMEOUT_S} s ({text})")
    if status != 200:
        return Check("tool calls", "FAIL", f"the server refused a request with a tool (HTTP {status}: "
                                           f"{' '.join(text.split())[:200]}): {fix}")
    try:
        choice = json.loads(text)["choices"][0]
        message = choice.get("message") or {}
    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
        return Check("tool calls", "FAIL", f"the reply isn't an OpenAI chat completion: {' '.join(text.split())[:200]}")
    calls = message.get("tool_calls") or []
    if calls:
        fn = (calls[0] or {}).get("function") or {}
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except ValueError:
            return Check("tool calls", "FAIL", f"{fn.get('name')} was called with arguments that aren't JSON: "
                                               f"{str(fn.get('arguments'))[:120]}")
        return Check("tool calls", "ok", f"{fn.get('name')}({json.dumps(args, ensure_ascii=False)})")
    content = message.get("content") or ""
    if trace_mod.MARKUP.search(content) or trace_mod.BARE_CALL.search(content):
        return Check("tool calls", "FAIL", f"the model wrote the call as text, which the server didn't turn into a "
                                           f"tool call: {fix}")
    if choice.get("finish_reason") == "length":
        return Check("tool calls", "FAIL", f"the reply reached its {CHAT_MAX_TOKENS}-token limit without calling "
                                           "the tool (still thinking?): run the check again, or try another model")
    return Check("tool calls", "FAIL", f"the model answered in text instead of calling the tool: {fix}, or use a "
                                       "model that can call tools")


def check_metrics(kind: Optional[str], base: str, loaded: Optional[Dict[str, Any]], needed: bool) -> Check:
    why = "only --telemetry needs it"
    if not is_local(base):
        return Check("metrics", "note", f"not checked: --telemetry measures only a model server on this PC ({why})")
    where = [root(base)] + ([root(loaded["backend_url"])] if loaded and loaded.get("backend_url") else [])
    for i, at in enumerate(where):
        status, text = request(at + "/metrics")
        found = telemetry.metrics_kind(text) if status == 200 else None
        if found:
            return Check("metrics", "ok", f"{found} counters at {at}/metrics" + (", Lemonade's llama-server" if i else ""))
    fix = _fix(METRICS_FIX, kind)
    if needed:
        return Check("metrics", "FAIL", f"no llama-server, vLLM or SGLang counters at {root(base)}/metrics: {fix} ({why})")
    return Check("metrics", "note", f"none at {root(base)}/metrics ({why}: {fix})")


def lemonade_loaded(base: str, model: str) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    """Lemonade's entry for ``model`` among its loaded models (None when it isn't loaded), and every loaded name."""
    health = get_json(base + "/health") or get_json(root(base) + "/api/v1/health") or {}
    entries = [e for e in health.get("all_models_loaded") or [] if isinstance(e, dict)] if isinstance(health, dict) else []
    names = [e.get("model_name") for e in entries if e.get("model_name")]
    return next((e for e in entries if e.get("model_name") == model), None), names


def run_checks(base_url: Optional[str] = None, model: Optional[str] = None, key: Optional[str] = None,
               need_metrics: bool = True) -> List[Check]:
    """Every check, in order; a check that can't run once an earlier one failed is a note."""
    key = key if key is not None else os.environ.get("KW_LOCAL_API_KEY")
    where = "--base-url" if base_url else ""
    if not base_url and os.environ.get("KW_LOCAL_BASE_URL"):
        base_url, where = os.environ["KW_LOCAL_BASE_URL"], "KW_LOCAL_BASE_URL"
    if not base_url:
        base_url, where = find_base_url()
        if not base_url:
            return [Check("server", "FAIL", where)]
    base = base_url.rstrip("/")
    skipped = [Check(k, "note", "not checked") for k in ("context", "tool calls", "metrics")]
    status, text = request(base + "/models", key=key)
    if status is None:
        return [Check("server", "FAIL", f"nothing answers at {base} ({where}: {text}): start the model server, or "
                                        "point --base-url (KW_LOCAL_BASE_URL) at it")] + skipped
    if status in (401, 403):
        return [Check("server", "FAIL", f"{base} refused the request (HTTP {status}): set KW_LOCAL_API_KEY to its "
                                        "key")] + skipped
    try:
        data = json.loads(text)["data"] if status == 200 else None
        entries = {str(d["id"]): d for d in data}
    except (ValueError, KeyError, TypeError):
        entries = None
    if not entries:
        return [Check("server", "FAIL", f"{base}/models answered HTTP {status} with no model list: is {base} the "
                                        "server's OpenAI base URL (most end in /v1; Lemonade's is /api/v1)?")] + skipped
    kinds = {OWNERS.get(str(d.get("owned_by"))) for d in entries.values()} - {None}
    kind = kinds.pop() if len(kinds) == 1 else None
    if kind is None and is_local(base):  # LM Studio, Ollama: their owned_by doesn't say
        port = urllib.parse.urlsplit(base).port
        kind = next((s["kind"] for s in local_servers.list_servers() if s.get("port") == port), None)
    ids = list(entries)
    loaded_names = lemonade_loaded(base, "")[1] if kind == "lemonade" else []
    shown = ", ".join(ids[:8]) + (f" and {len(ids) - 8} more" if len(ids) > 8 else "")
    if model:
        chosen = match_model(model, ids)
        if not chosen:
            return [Check("server", "FAIL", f"{base} doesn't list the model {model}; it lists {shown}: pass the id it "
                                            "lists")] + skipped
    elif len(ids) == 1 or len(loaded_names) == 1:  # Lemonade lists every model it has downloaded
        chosen = ids[0] if len(ids) == 1 else loaded_names[0]
    else:
        return [Check("server", "FAIL", f"{base} lists {len(ids)} models ({shown}): pass --model to say which one "
                                        "the agent uses")] + skipped
    loaded = None
    if kind == "lemonade":
        loaded, loaded_names = lemonade_loaded(base, chosen)
        if not loaded:
            now = ", ".join(loaded_names) or "none"
            return [Check("server", "FAIL", f"Lemonade at {base} has {chosen} but hasn't loaded it (loaded: {now}); "
                                            f"this check loads nothing: lemonade load {chosen} --ctx-size 65536")] + skipped
    return [Check("server", "ok", f"{base}, {kind or 'an OpenAI-compatible server'}, model {chosen}"),
            check_context(kind, base, chosen, entries[chosen], loaded),
            check_tool_call(kind, base, chosen, key),
            check_metrics(kind, base, loaded, need_metrics)]


def failed(checks: List[Check]) -> List[Check]:
    return [c for c in checks if c.status == "FAIL"]


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base-url", help="the server's OpenAI base URL, e.g. http://127.0.0.1:8080/v1 (default: "
                                       "$KW_LOCAL_BASE_URL, else the one model server running here)")
    ap.add_argument("--model", help="the model id the agent asks for (default: the one model the server lists)")
    args = ap.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")
    checks = run_checks(args.base_url, args.model)
    for c in checks:
        print(c.line(), flush=True)
    bad = failed(checks)
    print(f"{'result:'.ljust(11)} " + (f"{len(bad)} check(s) failed" if bad else "ready for a run"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
