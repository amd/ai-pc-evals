# Copyright(C) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""The openai judge backend (#70): any OpenAI-compatible /chat/completions endpoint, OpenAI's own by default."""
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from kwengine import _support as ft
from kwengine import grade
from test_judge_backend import GRADE, _fixed_ids
from test_kwengine import GOOD_SUMMARY, SMOKE, _no_credentials_or_keys, _prepared, _record  # noqa: F401

JUDGE_KEY = "sk-proj-" + "j" * 40


@pytest.fixture()
def endpoint(monkeypatch):
    """A local OpenAI-compatible server: each request is recorded and answered by the next of ``replies``
    (status, body, headers), the last one from then on."""
    state = {"requests": [], "replies": []}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["requests"].append({"path": self.path, "auth": self.headers.get("Authorization"), "body": body})
            replies = state["replies"]
            status, payload, headers = replies.pop(0) if len(replies) > 1 else replies[0]
            if callable(payload):
                payload = payload(body)
            data = payload.encode() if isinstance(payload, str) else json.dumps(payload).encode()
            self.send_response(status)
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(ft, "_pause", lambda s: None)
    state["env"] = {ft.JUDGE_BACKEND_ENV: "openai",
                    ft.OPENAI_BASE_URL_ENV: f"http://127.0.0.1:{server.server_address[1]}/v1"}
    yield state
    server.shutdown()


def reply(content, finish="stop", model="served-model"):
    return {"model": model, "choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": finish}]}


def test_openai_is_a_backend_with_openais_api_by_default_and_its_key_kept_apart():
    assert "openai" in ft.JUDGE_BACKENDS
    with pytest.raises(ValueError, match="KW_JUDGE_API_KEY"):  # OpenAI's API needs a key
        ft.resolve_judge_backend({ft.JUDGE_BACKEND_ENV: "openai"})
    with pytest.raises(ValueError, match="KW_JUDGE_API_KEY"):  # and OPENAI_API_KEY is not read: an agent may use it
        ft.resolve_judge_backend({ft.JUDGE_BACKEND_ENV: "openai", "OPENAI_API_KEY": JUDGE_KEY})
    assert ft.resolve_judge_backend({ft.JUDGE_BACKEND_ENV: "openai", ft.OPENAI_KEY_ENV: JUDGE_KEY}) == ("openai", "")
    # Another server needs none (a local vLLM, llama-server, LM Studio...).
    assert ft.resolve_judge_backend({ft.JUDGE_BACKEND_ENV: "openai",
                                     ft.OPENAI_BASE_URL_ENV: "http://127.0.0.1:8080/v1"})[0] == "openai"


@pytest.mark.parametrize("model", ["claude-opus-5-5", "opus", "sonnet[1m]", ""])
def test_the_openai_backend_has_no_default_model(model):
    with pytest.raises(ValueError, match="--judge-model"):
        ft.check_judge_model(model, "openai")
    ft.check_judge_model(model or "opus", "claude-code")  # the other backends keep their Claude default
    ft.check_judge_model("gpt-5.2", "openai")
    ft.check_judge_model("glm-4.7", "openai")


def test_the_endpoint_gets_what_the_other_backends_get_and_the_reply_parses_the_same(endpoint):
    endpoint["replies"] = [(200, reply('{"a1": {"accuracy": 5}}'), None)]
    env = {**endpoint["env"], ft.OPENAI_KEY_ENV: JUDGE_KEY}
    raw = ft.call_judge("THE PAYLOAD", "gpt-5.2", env)
    (req,) = endpoint["requests"]
    assert req["path"] == "/v1/chat/completions" and req["auth"] == f"Bearer {JUDGE_KEY}"
    assert req["body"] == {"model": "gpt-5.2", "messages": [{"role": "user", "content": "THE PAYLOAD"}]}
    grades, cost = ft.judge_reply(raw)
    assert grades == {"a1": {"accuracy": 5}} and cost == 0.0
    assert json.loads(raw)["backend"] == "openai" and json.loads(raw)["model"] == "served-model"


def test_a_reasoning_models_thinking_left_in_the_reply_is_dropped(endpoint):
    endpoint["replies"] = [(200, reply('<think>maybe {"x": 1}?</think>\n{"a1": {"accuracy": 4}}'), None)]
    assert ft.judge_reply(ft.call_judge("p", "glm-4.7", endpoint["env"]))[0] == {"a1": {"accuracy": 4}}


def test_no_key_and_no_cap_unless_set(endpoint):
    endpoint["replies"] = [(200, reply("{}"), None)]
    ft.call_judge("p", "local", endpoint["env"])
    ft.call_judge("p", "local", {**endpoint["env"], ft.OPENAI_MAX_TOKENS_ENV: "8000"})
    first, second = endpoint["requests"]
    assert first["auth"] is None and "max_completion_tokens" not in first["body"]
    assert second["body"]["max_completion_tokens"] == 8000


def test_rate_limits_and_server_errors_are_tried_again(endpoint):
    endpoint["replies"] = [(429, "slow down", {"retry-after": "1"}), (503, "busy", None), (200, reply("{}"), None)]
    ft.call_judge("p", "local", endpoint["env"])
    assert len(endpoint["requests"]) == 3


@pytest.mark.parametrize("status_reply, why", [
    ((400, '{"error": "unknown model"}', None), "answered 400"),
    ((200, reply("{}", finish="length"), None), "cut off"),
    ((200, reply("", finish="content_filter"), None), "filtered"),
    ((200, "<html>not an API</html>", None), "did not answer JSON"),
    ((200, {"choices": []}, None), "no choices"),
])
def test_what_cant_be_a_grade_is_a_judge_error(endpoint, status_reply, why):
    endpoint["replies"] = [status_reply]
    with pytest.raises(ft.JudgeError, match=why):
        ft.call_judge("p", "local", endpoint["env"])
    assert len(endpoint["requests"]) == 1  # not tried again


def test_nothing_listening_is_a_judge_error_after_the_tries(monkeypatch):
    monkeypatch.setattr(ft, "_pause", lambda s: None)
    env = {ft.JUDGE_BACKEND_ENV: "openai", ft.OPENAI_BASE_URL_ENV: "http://127.0.0.1:9/v1"}
    with pytest.raises(ft.JudgeError, match="unreachable after"):
        ft.call_judge("p", "local", env)


def test_a_run_judged_by_an_openai_compatible_model_says_which(tmp_path, monkeypatch, endpoint):
    run_dir = _prepared(tmp_path)
    (run_dir / SMOKE / "summary.md").write_text(GOOD_SUMMARY)
    _record(run_dir)
    _fixed_ids(monkeypatch)

    def grades(body):
        keys = re.findall(r"=== ATTEMPT (attempt-[0-9a-f]+) ", body["messages"][0]["content"])
        return reply(json.dumps({k: GRADE for k in keys}), model="glm-4.7")
    endpoint["replies"] = [(200, grades, None)]
    card = grade.grade_run(run_dir, "glm-4.7", dict(endpoint["env"]))
    (entry,) = card["tasks"]
    assert entry["passed"] is True and entry["judge"]["cost_usd"] == 0.0
    assert (card["judge_model"], card["judge_backend"]) == ("glm-4.7", "openai")
    assert endpoint["requests"][0]["body"]["messages"][0]["content"].startswith(grade.KW_RUBRIC)
    with pytest.raises(ValueError, match="--judge-model"):  # the Claude default would mean nothing there
        grade.grade_run(run_dir, ft.DEFAULT_JUDGE_MODEL, dict(endpoint["env"]))


def test_agents_never_get_the_judges_openai_key(monkeypatch):
    from agents.base import agent_environ

    monkeypatch.setenv("KW_JUDGE_API_KEY", JUDGE_KEY)
    monkeypatch.setenv("KW_JUDGE_BASE_URL", "http://127.0.0.1:8080/v1")
    env = agent_environ()
    assert "KW_JUDGE_API_KEY" not in env and "kw_judge_api_key" not in agent_environ({"kw_judge_api_key": JUDGE_KEY})
    assert "KW_JUDGE_API_KEY" not in ft.agent_env()  # a probe's environment has no credentials at all
    assert JUDGE_KEY not in ft.Scrubber.from_environment().scrub(f"key={JUDGE_KEY}")  # nor does a log keep it
