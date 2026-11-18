# Copyright(C) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""tools/check_server.py against fake model servers: each failure the check is for, and a server that passes."""
from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import check_server as cs  # noqa: E402

LLAMA_METRICS = "# HELP llamacpp:prompt_tokens_total x\n# TYPE llamacpp:prompt_tokens_total counter\nllamacpp:prompt_tokens_total 0\n"
CALL = {"choices": [{"finish_reason": "tool_calls", "message": {"role": "assistant", "content": "", "tool_calls": [
    {"id": "1", "type": "function", "function": {"name": "get_weather", "arguments": "{\"city\":\"Paris\"}"}}]}}]}


def text_reply(content: str, finish: str = "stop") -> dict:
    return {"choices": [{"finish_reason": finish, "message": {"role": "assistant", "content": content}}]}


def llama_routes(model: str = "qwen", n_ctx: int = 65536, reply=CALL, metrics: bool = True, owner: str = "llamacpp"):
    """A llama-server: GET path -> (status, body); "POST" -> the chat reply (status, body)."""
    routes = {"/v1/models": (200, {"object": "list", "data": [{"id": model, "owned_by": owner}]}),
              "/props": (200, {"default_generation_settings": {"n_ctx": n_ctx}, "total_slots": 1}),
              "/slots": (200, [{"id": 0, "n_ctx": n_ctx}]),
              "POST": (200, reply)}
    if metrics:
        routes["/metrics"] = (200, LLAMA_METRICS)
    return routes


class Fake:
    """An HTTP server on 127.0.0.1 and a free port, answering from a routes table; records the chat requests."""

    def __init__(self, routes):
        self.routes, self.posts = routes, []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # quiet
                pass

            def reply(self, status, body):
                data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):  # noqa: N802
                self.reply(*fake.routes.get(self.path, (404, "not found")))

            def do_POST(self):  # noqa: N802
                fake.posts.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                self.reply(*fake.routes["POST"])

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.root = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def fake():
    made = []

    def make(routes):
        made.append(Fake(routes))
        return made[-1]

    yield make
    for f in made:
        f.close()


@pytest.fixture(autouse=True)
def no_env(monkeypatch):
    monkeypatch.delenv("KW_LOCAL_BASE_URL", raising=False)
    monkeypatch.delenv("KW_LOCAL_API_KEY", raising=False)
    monkeypatch.setattr(cs.local_servers, "list_servers", lambda: [])


def by_key(checks):
    return {c.key: c for c in checks}


def test_a_server_set_up_right_passes_every_check(fake, capsys):
    srv = fake(llama_routes())
    assert cs.main(["--base-url", srv.root + "/v1", "--model", "qwen"]) == 0
    out = capsys.readouterr().out
    assert "server:     ok (" in out and "context:    ok (65,536 tokens, slot n_ctx)" in out
    assert 'tool calls: ok (get_weather({"city": "Paris"}))' in out and "metrics:    ok (llama-server counters" in out
    assert out.rstrip().endswith("result:     ready for a run")
    sent = srv.posts[0]
    assert sent["model"] == "qwen" and sent["tools"][0]["function"]["name"] == "get_weather"


def test_a_call_written_as_text_fails_with_the_jinja_fix(fake, capsys):
    srv = fake(llama_routes(reply=text_reply('<tool_call>\n{"name": "get_weather", "arguments": {"city": "Paris"}}\n</tool_call>')))
    assert cs.main(["--base-url", srv.root + "/v1", "--model", "qwen"]) == 1
    out = capsys.readouterr().out
    assert "tool calls: FAIL: the model wrote the call as text" in out and "start llama-server with --jinja" in out
    assert "result:     1 check(s) failed" in out


def test_llama_server_with_no_jinja_refuses_the_tool(fake):
    # what llama.cpp b12211 started with --no-jinja answers (on user-hassan, 2026-11-18)
    srv = fake(llama_routes(reply=None))
    srv.routes["POST"] = (500, {"error": {"code": 500, "message": "tools param requires --jinja flag",
                                          "type": "server_error"}})
    c = by_key(cs.run_checks(srv.root + "/v1", "qwen"))["tool calls"]
    assert c.status == "FAIL" and "HTTP 500" in c.text and "requires --jinja flag" in c.text
    assert "start llama-server with --jinja" in c.text


def test_a_plain_text_answer_fails_too(fake):
    srv = fake(llama_routes(reply=text_reply("It is sunny in Paris.")))
    c = by_key(cs.run_checks(srv.root + "/v1", "qwen"))["tool calls"]
    assert c.status == "FAIL" and "answered in text" in c.text and "--jinja" in c.text


def test_a_window_under_64k_fails(fake, capsys):
    srv = fake(llama_routes(n_ctx=32768))
    assert cs.main(["--base-url", srv.root + "/v1", "--model", "qwen"]) == 1
    out = capsys.readouterr().out
    assert "context:    FAIL: 32,768 tokens (slot n_ctx), under 65,536" in out and "-c 65536" in out


def test_props_alone_gives_the_window(fake):
    routes = llama_routes(n_ctx=131072)
    del routes["/slots"]  # llama-server started with --no-slots
    c = by_key(cs.run_checks(fake(routes).root + "/v1", "qwen"))["context"]
    assert c.status == "ok" and "131,072" in c.text


def test_no_metrics_fails_and_names_the_flag(fake, capsys):
    srv = fake(llama_routes(metrics=False))
    assert cs.main(["--base-url", srv.root + "/v1", "--model", "qwen"]) == 1
    assert "metrics:    FAIL: no llama-server, vLLM or SGLang counters" in capsys.readouterr().out
    # run_eval.py without --telemetry: printed, not a failure
    c = by_key(cs.run_checks(srv.root + "/v1", "qwen", need_metrics=False))["metrics"]
    assert c.status == "note" and "--metrics" in c.text


def test_a_wrong_model_id_fails_and_lists_the_served_ones(fake, capsys):
    srv = fake(llama_routes(model="qwen3.8-27b"))
    assert cs.main(["--base-url", srv.root + "/v1", "--model", "qwen3.8-72b"]) == 1
    out = capsys.readouterr().out
    assert "server:     FAIL:" in out and "doesn't list the model qwen3.8-72b; it lists qwen3.8-27b" in out
    assert "tool calls: not checked" in out and not srv.posts


def test_an_agents_provider_prefix_matches_the_served_id(fake):
    srv = fake(llama_routes(model="qwen/qwen3.8-27b"))
    checks = cs.run_checks(srv.root + "/v1", "lmstudio/qwen/qwen3.8-27b")
    assert not cs.failed(checks) and srv.posts[0]["model"] == "qwen/qwen3.8-27b"


def test_without_model_the_one_listed_model_is_used_and_several_need_model(fake):
    srv = fake(llama_routes(model="only"))
    assert not cs.failed(cs.run_checks(srv.root + "/v1"))
    routes = llama_routes()
    routes["/v1/models"] = (200, {"data": [{"id": "a", "owned_by": "x"}, {"id": "b", "owned_by": "x"}]})
    c = cs.run_checks(fake(routes).root + "/v1")[0]
    assert c.status == "FAIL" and "lists 2 models (a, b): pass --model" in c.text


def test_nothing_answering_fails_and_skips_the_rest(fake):
    srv = fake(llama_routes())
    url = srv.root + "/v1"
    srv.close()
    checks = cs.run_checks(url, "qwen")
    assert checks[0].status == "FAIL" and "nothing answers at" in checks[0].text
    assert [c.text for c in checks[1:]] == ["not checked"] * 3


def test_a_key_is_sent_and_a_refusal_names_the_variable(fake, monkeypatch):
    routes = llama_routes()
    routes["/v1/models"] = (401, {"error": "unauthorized"})
    c = cs.run_checks(fake(routes).root + "/v1", "qwen")[0]
    assert c.status == "FAIL" and "KW_LOCAL_API_KEY" in c.text


def test_vllm_without_a_tool_parser(fake):
    routes = {"/v1/models": (200, {"data": [{"id": "Qwen/Qwen3-8B", "owned_by": "vllm", "max_model_len": 40960}]}),
              "/metrics": (200, "# TYPE vllm:prompt_tokens_total counter\nvllm:prompt_tokens_total 0\n"),
              "POST": (400, {"error": {"message": '"auto" tool choice requires --enable-auto-tool-choice and '
                                                  "--tool-call-parser to be set"}})}
    checks = by_key(cs.run_checks(fake(routes).root + "/v1", "Qwen/Qwen3-8B"))
    assert checks["server"].text.endswith("vllm, model Qwen/Qwen3-8B")
    assert checks["context"].status == "FAIL" and "40,960 tokens (max_model_len)" in checks["context"].text
    assert "--max-model-len 65536" in checks["context"].text
    assert checks["tool calls"].status == "FAIL" and "HTTP 400" in checks["tool calls"].text
    assert "--enable-auto-tool-choice --tool-call-parser" in checks["tool calls"].text
    assert checks["metrics"].status == "ok" and "vllm counters" in checks["metrics"].text


def test_a_reply_cut_off_while_thinking(fake):
    srv = fake(llama_routes(reply=text_reply("", finish="length")))
    c = by_key(cs.run_checks(srv.root + "/v1", "qwen"))["tool calls"]
    assert c.status == "FAIL" and "2048-token limit" in c.text


def lemonade(fake, backend_ctx=65536, loaded=True, model="Qwen3.6-35B-A3B-GGUF"):
    backend = fake(llama_routes(model="x.gguf", n_ctx=backend_ctx))
    entry = {"model_name": model, "backend_url": backend.root + "/v1", "recipe_options": {"ctx_size": backend_ctx}}
    health = {"status": "ok", "all_models_loaded": [entry] if loaded else []}
    models = {"data": [{"id": "Gemma-4-E4B-it-GGUF", "owned_by": "lemonade"}, {"id": model, "owned_by": "lemonade"}]}
    routes = {"/api/v1/models": (200, models), "/api/v1/health": (200, health), "/v1/models": (200, models),
              "/v1/health": (200, health), "/props": (200, "<!doctype html><html>Lemonade App</html>"),
              "/metrics": (200, "# TYPE lemonade_server_up gauge\nlemonade_server_up 1\n"), "POST": (200, CALL)}
    return fake(routes)


@pytest.mark.parametrize("path", ["/api/v1", "/v1"])
def test_lemonade_reads_its_llama_servers_window_and_metrics(fake, path):
    srv = lemonade(fake)
    checks = by_key(cs.run_checks(srv.root + path))  # no --model: the one it has loaded
    assert not cs.failed(list(checks.values()))
    assert "model Qwen3.6-35B-A3B-GGUF" in checks["server"].text
    assert checks["context"].text == "65,536 tokens, its llama-server's slot n_ctx"
    assert checks["metrics"].text.endswith("Lemonade's llama-server")


def test_lemonade_loaded_with_a_small_window_fails(fake):
    c = by_key(cs.run_checks(lemonade(fake, backend_ctx=32768).root + "/api/v1", "Qwen3.6-35B-A3B-GGUF"))["context"]
    assert c.status == "FAIL" and "lemonade load Qwen3.6-35B-A3B-GGUF --ctx-size 65536" in c.text


def test_a_model_lemonade_has_not_loaded_fails_without_loading_it(fake):
    srv = lemonade(fake, loaded=False)
    checks = cs.run_checks(srv.root + "/api/v1", "Qwen3.6-35B-A3B-GGUF")
    assert checks[0].status == "FAIL" and "hasn't loaded it (loaded: none)" in checks[0].text
    assert not srv.posts  # a chat request would have made Lemonade load it


def test_the_url_comes_from_kw_local_base_url(fake, monkeypatch):
    srv = fake(llama_routes())
    monkeypatch.setenv("KW_LOCAL_BASE_URL", srv.root + "/v1")
    assert by_key(cs.run_checks(model="qwen"))["server"].status == "ok"


def test_the_url_comes_from_the_one_server_running():
    lemond = {"pid": 1, "kind": "lemonade", "port": 13305, "path": "C:/x/LemonadeServer.exe"}
    backend = {"pid": 2, "kind": "llama-server", "port": 8001, "path": r"C:\Users\u\.cache\lemonade\bin\llama-server.exe"}
    embed = {"pid": 3, "kind": "llama-server", "port": 8002, "path": "llama-server", "embeddings": True}
    assert cs.find_base_url([lemond, backend, embed])[0] == "http://127.0.0.1:13305/api/v1"
    assert cs.find_base_url([{"pid": 4, "kind": "llama-server", "port": 8080, "path": "llama-server"}])[0] \
        == "http://127.0.0.1:8080/v1"
    url, why = cs.find_base_url([{"pid": 4, "kind": "llama-server", "port": 8080, "path": "a"},
                                 {"pid": 5, "kind": "vllm", "port": 8000, "path": "vllm"}])
    assert url is None and "several model servers" in why and "--base-url" in why


def test_no_server_running(monkeypatch):
    monkeypatch.setattr(cs.telemetry, "probe", lambda: [])
    checks = cs.run_checks()
    assert len(checks) == 1 and checks[0].status == "FAIL" and "no model server is running" in checks[0].text


def test_run_eval_check_server_stops_before_the_first_task(tmp_path, monkeypatch, capsys):
    import run_eval
    from agents.base import Agent

    class Fake(Agent):
        name = "fake"

        def run(self, task, log_dir):
            raise AssertionError("no task may run after a failed check")

    monkeypatch.setattr(run_eval, "EVAL_HOME", tmp_path / "home")
    monkeypatch.setattr(run_eval, "get_agent", lambda name, model: Fake(model))
    monkeypatch.setattr(cs, "run_checks", lambda model=None, need_metrics=True: [
        cs.Check("server", "ok", f"http://x/v1, llama-server, model {model}"),
        cs.Check("tool calls", "FAIL", "start llama-server with --jinja"),
        cs.Check("metrics", "note" if not need_metrics else "FAIL", "none")])
    with pytest.raises(run_eval.EvalError, match="isn't ready for this run"):
        run_eval.run("fake", "haiku", "verify", ["verify-02"], tmp_path / "out", tmp_path / "fx", "m",
                     do_grade=False, check_server=True)
    out = capsys.readouterr().out
    assert "server:     ok (http://x/v1, llama-server, model haiku)" in out
    assert "tool calls: FAIL: start llama-server with --jinja" in out and "metrics:    none" in out
    assert not (tmp_path / "out").exists()  # no task was prepared
