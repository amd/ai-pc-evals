# Copyright(C) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""The judge's api backend (the Anthropic Messages API, no Claude Code), against a faked client: it sends what
``claude -p`` gets, parses the same way, retries what is transient, and says in the scorecard which backend judged.
No live call, unless KW_JUDGE_API_LIVE=1 (and ANTHROPIC_API_KEY) is set."""
import json
import os
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import anthropic
import pytest

try:  # the HTTP library the installed SDK is built on: httpx2 from anthropic 2.x, httpx before
    import httpx2 as httpx
except ImportError:
    import httpx

from kwengine import _support as ft
from kwengine import cli, grade
from test_kwengine import GOOD_SUMMARY, SMOKE, _no_credentials_or_keys, _prepared, _record  # noqa: F401

KEY = "sk-ant-api03-" + "k" * 40
#: Read before the shared fixture takes ANTHROPIC_API_KEY out of each test's environment.
LIVE_KEY = os.environ.get("ANTHROPIC_API_KEY") if os.environ.get("KW_JUDGE_API_LIVE") == "1" else None
API_ENV = {ft.JUDGE_BACKEND_ENV: "api", ft.API_KEY_ENV: KEY}
GRADE = {"instruction_compliance": 5, "work_quality": 4, "accuracy": 5, "completeness": 4, "fabrication_free": 5,
         "answers_correctly": True, "missing": "", "one_line": "ok"}
#: The SDK reads these from the process environment itself, so a machine behind an Anthropic gateway would
#: send the offline tests' requests to the gateway with its headers. The live test keeps them.
GATEWAY_ENV = ("ANTHROPIC_BASE_URL", "ANTHROPIC_CUSTOM_HEADERS", "ANTHROPIC_AUTH_TOKEN")


@pytest.fixture(autouse=True)
def _no_gateway(request, monkeypatch):
    if "live" not in request.node.name:
        for name in GATEWAY_ENV:
            monkeypatch.delenv(name, raising=False)


def message(text, stop_reason="end_turn", model="claude-opus-5-5", input_tokens=1000, output_tokens=100):
    """A Messages API reply as the SDK returns it: a thinking block (empty, as Opus 5.5 omits it), then text."""
    return SimpleNamespace(
        content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=text)],
        stop_reason=stop_reason, stop_details=None, model=model,
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens,
                              cache_read_input_tokens=0, cache_creation_input_tokens=None))


class FakeStream:
    def __init__(self, outcome):
        self.outcome = outcome

    def __enter__(self):
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self.outcome


class FakeClient:
    """``client.messages.stream(...)``: each call takes the next outcome (a reply, an error, or a function of
    the request's kwargs), and every request is kept."""

    def __init__(self, *outcomes):
        self.outcomes, self.calls = list(outcomes), []
        self.messages = self

    def stream(self, **kwargs):
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        return FakeStream(outcome(kwargs) if callable(outcome) else outcome)


def fake_api(monkeypatch, *outcomes):
    client = FakeClient(*outcomes)
    keys = []
    monkeypatch.setattr(ft, "_api_client", lambda env: keys.append(env[ft.API_KEY_ENV]) or client)
    pauses = []
    monkeypatch.setattr(ft, "_pause", pauses.append)
    monkeypatch.setattr(ft.random, "uniform", lambda a, b: 0.0)
    client.pauses, client.keys = pauses, keys
    return client


def grading_reply(kwargs):
    """The grades for every attempt the payload asks about, as the judge is asked for them."""
    keys = re.findall(r"=== ATTEMPT (attempt-[0-9a-f]+) ", kwargs["messages"][0]["content"])
    return message("Here are the grades:\n" + json.dumps({k: GRADE for k in keys}) + "\nDone.")


def status_error(cls, code, headers=None):
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return cls(f"status {code}", response=httpx.Response(code, headers=headers or {}, request=request), body=None)


def connection_error():
    return anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"))


# ---- choosing the backend ---------------------------------------------------


def test_the_default_backend_is_claude_code(monkeypatch):
    monkeypatch.setattr(ft.shutil, "which", lambda name: "/bin/claude")
    assert ft.resolve_judge_backend({}) == ("claude-code", "")
    assert ft.resolve_judge_backend({ft.API_KEY_ENV: KEY}) == ("claude-code", "")  # a key alone changes nothing
    assert ft.resolve_judge_backend(API_ENV) == ("api", "")


def test_without_the_cli_a_key_falls_back_to_the_api_and_says_so(monkeypatch):
    monkeypatch.setattr(ft.shutil, "which", lambda name: None)
    backend, note = ft.resolve_judge_backend({ft.API_KEY_ENV: KEY})
    assert backend == "api" and "claude CLI is not on PATH" in note
    # Named explicitly, claude-code never falls back; without a key there is nothing to fall back to.
    assert ft.resolve_judge_backend({ft.JUDGE_BACKEND_ENV: "claude-code", ft.API_KEY_ENV: KEY}) == ("claude-code", "")
    assert ft.resolve_judge_backend({}) == ("claude-code", "")


def test_a_bad_backend_or_an_api_backend_without_a_key_stops_grading():
    with pytest.raises(ValueError, match="one of claude-code, api"):
        ft.resolve_judge_backend({ft.JUDGE_BACKEND_ENV: "openai"})
    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
        ft.resolve_judge_backend({ft.JUDGE_BACKEND_ENV: "api"})


def test_claude_code_model_names_map_to_api_ids():
    assert ft.api_model_id("opus") == "claude-opus-5-5" == ft.DEFAULT_JUDGE_MODEL
    assert ft.api_model_id("Opus[1m]") == "claude-opus-5-5"
    assert (ft.api_model_id("sonnet"), ft.api_model_id("haiku")) == ("claude-sonnet-5-5", "claude-haiku-5-5")
    assert ft.api_model_id("claude-opus-4-8") == "claude-opus-4-8"  # a full id is used as given
    assert ft.judge_model_id("opus", "claude-code") == "opus"  # the CLI gets, and the card records, what was given


def test_the_judges_default_model_is_named_in_one_place():
    """``DEFAULT_JUDGE_MODEL`` is the judge's default for the engine and every tool: a tool that names a Claude
    model id itself would keep judging with the old model after the default moves."""
    repo = Path(__file__).resolve().parents[1]
    model_id = re.compile(r"""["']claude-(?:opus|sonnet|haiku|fable)-\d""")
    named = [f"{path.relative_to(repo)}:{n}" for folder in ("kwengine", "tools") for path in (repo / folder).rglob("*.py")
             if path.name != "_support.py"
             for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1) if model_id.search(line)]
    assert not named, f"import kwengine._support.DEFAULT_JUDGE_MODEL instead: {named}"


def test_the_grade_command_takes_a_backend_flag():
    args = cli.build_parser().parse_args(["grade", "RUN", "--judge-backend", "api"])
    assert args.judge_backend == "api"
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["grade", "RUN", "--judge-backend", "other"])


# ---- the call: same prompt, same parsing ---------------------------------------


def test_the_api_gets_exactly_what_claude_p_gets_on_stdin(monkeypatch):
    payload = grade.KW_RUBRIC + "\n\n=== ATTEMPT attempt-abc123 ===\nan attempt"
    sent = []

    def fake_run(cmd, **kwargs):
        sent.append(kwargs["input"])
        return SimpleNamespace(returncode=0, stderr="", stdout=json.dumps({"result": "{}", "total_cost_usd": 0}))
    monkeypatch.setattr(ft.shutil, "which", lambda name: "/bin/claude")
    monkeypatch.setattr(ft.subprocess, "run", fake_run)
    ft.call_judge(payload, "opus", {})
    client = fake_api(monkeypatch, message("{}"))
    ft.call_judge(payload, "opus", API_ENV)
    (request,) = client.calls
    assert request["messages"] == [{"role": "user", "content": sent[0]}] and sent[0] == payload
    assert request["model"] == "claude-opus-5-5" and client.keys == [KEY]
    # Like `claude -p --tools ""`: no tools, no extra system brief, and no sampling settings or thinking budget
    # (the model's own defaults, as the CLI leaves them).
    assert set(request) == {"model", "max_tokens", "messages"}


def test_the_api_reply_parses_as_the_cli_reply_does(monkeypatch):
    grades = {"attempt-abc123": GRADE}
    fake_api(monkeypatch, message("Grades follow.\n" + json.dumps(grades) + "\n", input_tokens=10_000,
                                  output_tokens=2_000))
    raw = ft.call_judge("payload", "opus", API_ENV)
    envelope = json.loads(raw)
    assert envelope["backend"] == "api" and envelope["model"] == "claude-opus-5-5"
    parsed, cost = ft.judge_reply(raw)
    assert parsed == grades
    assert cost == pytest.approx((10_000 * 4 + 2_000 * 20) / 1e6)  # Opus 5.5 list price


def test_an_unparseable_api_reply_is_the_same_judge_error_as_the_clis(monkeypatch):
    fake_api(monkeypatch, message("I can't grade these."))
    with pytest.raises(ft.JudgeError, match="no JSON object"):
        ft.judge_reply(ft.call_judge("payload", "opus", API_ENV))


@pytest.mark.parametrize("stop_reason, why", [("refusal", "declined"), ("max_tokens", "cut off")])
def test_a_refusal_or_a_cut_off_reply_is_a_judge_error(monkeypatch, stop_reason, why):
    fake_api(monkeypatch, message('{"attempt-1": ', stop_reason=stop_reason))
    with pytest.raises(ft.JudgeError, match=why):
        ft.call_judge("payload", "opus", API_ENV)


# ---- errors and retries -------------------------------------------------------


def test_rate_limits_overloads_and_dropped_connections_are_tried_again(monkeypatch):
    client = fake_api(monkeypatch,
                      status_error(anthropic.RateLimitError, 429, {"retry-after": "7"}),
                      status_error(anthropic.APIStatusError, 529),
                      connection_error(),
                      status_error(anthropic.InternalServerError, 500),
                      message('{"attempt-1": {}}'))
    parsed, _ = ft.judge_reply(ft.call_judge("payload", "opus", API_ENV))
    assert parsed == {"attempt-1": {}} and len(client.calls) == 5
    assert client.pauses == [7.0, 4.0, 8.0, 16.0]  # retry-after when given, else exponential backoff


def test_a_client_error_is_not_tried_again(monkeypatch):
    for error in (status_error(anthropic.BadRequestError, 400), status_error(anthropic.AuthenticationError, 401)):
        client = fake_api(monkeypatch, error)
        with pytest.raises(ft.JudgeError, match=f"Anthropic API {error.status_code}"):
            ft.call_judge("payload", "opus", API_ENV)
        assert len(client.calls) == 1 and client.pauses == []


def test_retries_end_in_a_judge_error_the_grader_records(monkeypatch):
    client = fake_api(monkeypatch, status_error(anthropic.RateLimitError, 429, {"retry-after": "600"}))
    with pytest.raises(ft.JudgeError, match="429.*after 6 tries"):
        ft.call_judge("payload", "opus", API_ENV)
    assert len(client.calls) == ft.API_ATTEMPTS
    assert client.pauses == [ft.API_BACKOFF_CAP_S] * (ft.API_ATTEMPTS - 1)  # a long retry-after is capped


SSE_REPLY = "".join(f"event: {name}\ndata: {json.dumps(data)}\n\n" for name, data in [
    ("message_start", {"type": "message_start", "message": {
        "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5", "content": [],
        "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 500, "output_tokens": 1}}}),
    ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
    ("content_block_delta", {"type": "content_block_delta", "index": 0,
                             "delta": {"type": "text_delta", "text": '{"attempt-1": {"one_line": "ok"}}'}}),
    ("content_block_stop", {"type": "content_block_stop", "index": 0}),
    ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                       "usage": {"output_tokens": 50}}),
    ("message_stop", {"type": "message_stop"}),
])


def test_the_real_sdk_streams_retries_and_parses_offline(monkeypatch):
    """The SDK itself, on a mock transport (no network): its errors map to the retry rules, its stream to the
    reply, and the request carries the key and the payload."""
    requests = []

    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(429, headers={"retry-after": "1"},
                                  json={"type": "error", "error": {"type": "rate_limit_error", "message": "slow"}})
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=SSE_REPLY)

    real_client = ft._api_client  # pylint: disable=protected-access

    def client(env):
        made = real_client(env)
        return made.with_options(http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(ft, "_api_client", client)
    pauses = []
    monkeypatch.setattr(ft, "_pause", pauses.append)
    grades, cost = ft.judge_reply(ft.call_judge("the payload", "opus", API_ENV))
    assert grades == {"attempt-1": {"one_line": "ok"}} and pauses == [1.0] and len(requests) == 2
    assert cost == pytest.approx((500 * 4 + 50 * 20) / 1e6)
    body = json.loads(requests[-1].content)
    headers = requests[-1].headers  # the key: x-api-key before anthropic 2.x, a bearer token from 2.x
    assert KEY in (headers.get("x-api-key"), headers.get("authorization", "").removeprefix("Bearer "))
    assert body["model"] == "claude-opus-5-5" and body["stream"]
    assert body["messages"] == [{"role": "user", "content": "the payload"}]


# ---- grading a run through the api backend --------------------------------------


def _fixed_ids(monkeypatch):
    """The fence and the attempt id are random per grade: fixed here, so two grades can be compared."""
    monkeypatch.setattr(grade, "new_fence", lambda: "AGENT-OUTPUT-0123456789ABCDEF")
    monkeypatch.setattr(grade, "neutral_key", lambda taken: "attempt-abc123")


def test_a_run_judged_through_the_api_gets_the_same_payload_and_says_which_backend(tmp_path, monkeypatch):
    run_dir = _prepared(tmp_path)
    (run_dir / SMOKE / "summary.md").write_text(GOOD_SUMMARY)
    _record(run_dir)
    _fixed_ids(monkeypatch)
    sent = []

    def fake_run(cmd, **kwargs):
        if cmd[0] != "/bin/claude":
            return real_run(cmd, **kwargs)
        sent.append(kwargs["input"])
        keys = re.findall(r"=== ATTEMPT (attempt-[0-9a-f]+) ", kwargs["input"])
        return SimpleNamespace(returncode=0, stderr="", stdout=json.dumps(
            {"result": json.dumps({k: GRADE for k in keys}), "total_cost_usd": 0.1}))
    real_run = ft.subprocess.run
    monkeypatch.setattr(ft.shutil, "which", lambda name: "/bin/claude")
    monkeypatch.setattr(ft.subprocess, "run", fake_run)
    cli_card = grade.grade_run(run_dir, "opus", {})
    assert "judge_backend" not in cli_card and grade.judge_backend(cli_card) == "claude-code"

    client = fake_api(monkeypatch, grading_reply)
    card = grade.grade_run(run_dir, "opus", dict(API_ENV))
    (request,) = client.calls
    assert request["messages"][0]["content"] == sent[0]  # the rubric, the fenced attempt, the extracts: unchanged
    assert request["messages"][0]["content"].startswith(grade.KW_RUBRIC)
    (entry,) = card["tasks"]
    assert entry["passed"] is True and entry["why"] == "judge: correct"
    assert entry["judge"]["cost_usd"] == pytest.approx((1000 * 4 + 100 * 20) / 1e6)
    assert (card["judge_model"], card["judge_backend"]) == ("claude-opus-5-5", "api")
    assert "judge_backend_note" not in card
    assert json.loads((run_dir / "scorecard.json").read_text())["judge_backend"] == "api"


def test_a_fallback_to_the_api_is_kept_in_the_scorecard(tmp_path, monkeypatch):
    run_dir = _prepared(tmp_path)
    (run_dir / SMOKE / "summary.md").write_text(GOOD_SUMMARY)
    _record(run_dir)
    monkeypatch.setattr(ft.shutil, "which", lambda name: None)
    fake_api(monkeypatch, grading_reply)
    card = grade.grade_run(run_dir, "claude-opus-5-5", {ft.API_KEY_ENV: KEY})
    assert card["judge_backend"] == "api" and "claude CLI is not on PATH" in card["judge_backend_note"]
    assert card["tasks"][0]["passed"] is True


def test_a_run_whose_batches_all_fail_on_the_api_is_awaiting_the_judge(tmp_path, monkeypatch):
    run_dir = _prepared(tmp_path)
    (run_dir / SMOKE / "summary.md").write_text(GOOD_SUMMARY)
    _record(run_dir)
    client = fake_api(monkeypatch, status_error(anthropic.BadRequestError, 400))
    (entry,) = grade.grade_run(run_dir, "opus", dict(API_ENV), attempts=2)["tasks"]
    assert entry["passed"] is None and "Anthropic API 400" in entry["judge"]["error"]
    assert len(client.calls) == 2  # once per --judge-attempts, as with the CLI


def test_regrading_part_of_a_run_on_another_backend_says_mixed():
    entry = {"id": "a", "passed": True, "input_tokens": 0, "output_tokens": 0, "cost_usd": None, "cost_source": ""}
    old = {"judge_model": "claude-opus-5-5", "tasks": [entry, dict(entry, id="b")]}
    new = {"judge_model": "claude-opus-5-5", "judge_backend": "api", "tasks": [dict(entry, id="b")]}
    card = grade.merge_cards(old, new, ["a", "b"], ["a", "b"])
    assert card["judge_backend"] == "mixed: api, claude-code" and card["judge_model"] == "claude-opus-5-5"
    again = grade.merge_cards(card, new, ["a", "b"], ["a", "b"])
    assert again["judge_backend"] == "mixed: api, claude-code"
    assert grade.merge_cards(new, new, ["b"], ["b"])["judge_backend"] == "api"


# ---- the judge's key never reaches an agent --------------------------------------


def test_agents_never_get_the_judges_api_key(tmp_path, monkeypatch):
    from agents import TaskSpec
    from agents.base import agent_environ
    from agents.claude_code import ClaudeCode
    from agents.command import CommandAgent

    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    monkeypatch.setenv("KW_ANSWERS_PASSPHRASE", "secret")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://localhost:8080")  # where a local model server is: kept
    monkeypatch.delenv("KW_AGENT_USER", raising=False)
    env = agent_environ()
    assert "ANTHROPIC_API_KEY" not in env and "KW_ANSWERS_PASSPHRASE" not in env
    assert env["ANTHROPIC_BASE_URL"] == "http://localhost:8080"
    assert "ANTHROPIC_API_KEY" not in agent_environ({"anthropic_api_key": KEY})  # Windows names are case-blind

    seen = []

    def fake_run(cmd, **kwargs):
        seen.append(kwargs["env"])
        return "", "", 1, False
    monkeypatch.setattr("agents.claude_code.run_capped", fake_run)
    monkeypatch.setattr("agents.command.run_capped", fake_run)
    folder = tmp_path / "task"
    folder.mkdir()
    spec = TaskSpec(id="t", prompt="do it", folder=folder, timeout_s=5)
    ClaudeCode("haiku").run(spec, tmp_path / "log1")
    CommandAgent("x", {"cmd": [sys.executable, "{prompt_file}"]}, "m").run(spec, tmp_path / "log2")
    assert len(seen) == 2 and all("ANTHROPIC_API_KEY" not in e and KEY not in e.values() for e in seen)


def test_an_agent_can_have_an_api_key_of_its_own():
    from agents.base import agent_environ

    env = agent_environ({"ANTHROPIC_API_KEY": KEY, "KW_AGENT_ANTHROPIC_API_KEY": "sk-ant-agent-own-key-123456"})
    assert env == {"ANTHROPIC_API_KEY": "sk-ant-agent-own-key-123456"}


# ---- opt-in live call -------------------------------------------------------------


@pytest.mark.skipif(not LIVE_KEY, reason="set KW_JUDGE_API_LIVE=1 and ANTHROPIC_API_KEY to call the API")
def test_live_api_judge_returns_parseable_grades():
    payload = ('Return STRICT JSON and nothing else: {"attempt-1": {"answers_correctly": true, '
               '"one_line": "<=10 words"}} where answers_correctly says whether 2 + 2 = 4.')
    raw = ft.call_judge_api(payload, ft.DEFAULT_JUDGE_MODEL, {ft.API_KEY_ENV: LIVE_KEY})
    grades, cost = ft.judge_reply(raw)
    assert grades["attempt-1"]["answers_correctly"] is True and cost > 0
