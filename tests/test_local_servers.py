"""Other model servers on the PC, the running engine's build, and the waiter's stall hint: no real processes or HTTP."""

import io
import json
import sys
import types
import urllib.error

import pytest

import engine_info
import local_servers
import wait_task


def test_a_server_is_parsed_from_its_command_line():
    s = local_servers.describe(10, "llama-server.exe", ['"C:/Lemonade/bin/llama-server.exe"', "-m", "C:/m/Qwen3.gguf",
                                                        "--port", "8001", "--ctx-size", "65536"])
    assert s == {"pid": 10, "kind": "llama-server", "name": "llama-server.exe", "model": "C:/m/Qwen3.gguf",
                 "port": 8001, "path": "C:/Lemonade/bin/llama-server.exe"}
    assert local_servers.describe(11, "llama-server", ["llama-server", "--model=x.gguf"])["port"] == 8080
    assert local_servers.describe(12, "ollama.exe", ["ollama", "serve"])["port"] == 11434
    assert local_servers.describe(13, "ollama.exe", ["ollama", "run", "llama3"]) is None  # a client
    assert local_servers.describe(14, "LM Studio.exe", ["LM Studio.exe"])["kind"] == "lm-studio"
    assert local_servers.describe(15, "notepad.exe", ["notepad"]) is None


def test_listing_uses_psutil_and_reads_only_candidates_command_lines(monkeypatch):
    read = []

    class Proc:
        def __init__(self, pid, name, args):
            self.info, self.args = {"pid": pid, "name": name}, args

        def cmdline(self):
            read.append(self.info["name"])
            return self.args

    fake = types.SimpleNamespace(Error=Exception, process_iter=lambda attrs: [
        Proc(1, "explorer.exe", ["explorer"]), Proc(2, "llama-server.exe", ["llama-server", "--port", "9000"]),
        Proc(3, "lemonade-server.exe", ["lemonade-server", "serve"])])
    monkeypatch.setitem(sys.modules, "psutil", fake)
    found = local_servers.list_servers()
    assert [(s["pid"], s["kind"], s["port"]) for s in found] == [(2, "llama-server", 9000), (3, "lemonade", 8000)]
    assert read == ["llama-server.exe", "lemonade-server.exe"]


def test_without_psutil_windows_asks_powershell_once_and_elsewhere_lists_nothing(monkeypatch):
    monkeypatch.setitem(sys.modules, "psutil", None)  # import psutil raises ImportError
    rows = {"ProcessId": 7, "Name": "llama-server.exe",
            "CommandLine": '"C:\\Program Files\\Perplexity\\llama-server.exe" -m "C:\\m\\pplx 27b.gguf" --port 51234'}
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return types.SimpleNamespace(stdout=json.dumps(rows), returncode=0)
    monkeypatch.setattr(local_servers.subprocess, "run", fake_run)
    monkeypatch.setattr(local_servers.sys, "platform", "win32")
    (s,) = local_servers.list_servers()
    assert (s["pid"], s["port"], s["model"]) == (7, 51234, "C:\\m\\pplx 27b.gguf") and len(calls) == 1
    assert "perplexity's install" in local_servers.label(s)
    monkeypatch.setattr(local_servers.sys, "platform", "linux")
    assert local_servers.list_servers() == [] and len(calls) == 1


PROPS = {"build_info": "b10825-9e0e220", "model_path": "C:/m/pplx.gguf", "total_slots": 1,
         "default_generation_settings": {"n_ctx": 65536}}


def _serve(monkeypatch, pages):
    asked = []

    def urlopen(url, timeout):
        asked.append(url)
        path = url.split(":", 2)[2].split("/", 1)[1]
        if "/" + path not in pages:
            raise urllib.error.HTTPError(url, 501, "no", {}, None)
        return io.BytesIO(pages["/" + path].encode("utf-8"))
    monkeypatch.setattr(engine_info.urllib.request, "urlopen", urlopen)
    return asked


def test_engine_info_reads_props_and_metrics_from_localhost_only(monkeypatch):
    asked = _serve(monkeypatch, {"/props": json.dumps(PROPS),
                                 "/metrics": "# HELP x\nllamacpp:prompt_tokens_total 1200\nllamacpp:tokens_predicted_total 340\n"})
    info = engine_info.info(51234)
    assert info == {"port": 51234, "build_info": "b10825-9e0e220", "n_ctx": 65536, "model_path": "C:/m/pplx.gguf",
                    "total_slots": 1, "metrics": {"llamacpp:prompt_tokens_total": 1200.0,
                                                  "llamacpp:tokens_predicted_total": 340.0}}
    assert all(u.startswith("http://127.0.0.1:51234/") for u in asked)


def test_engine_info_without_metrics_or_without_a_server(monkeypatch, capsys):
    _serve(monkeypatch, {"/props": json.dumps(PROPS)})
    assert engine_info.info(1)["metrics"] is None
    monkeypatch.setattr(engine_info.local_servers, "list_servers", lambda: [
        {"kind": "ollama", "port": 11434}, {"kind": "llama-server", "port": 51234}])
    assert engine_info.main(["--build"]) == 0 and capsys.readouterr().out.strip() == "b10825-9e0e220"
    _serve(monkeypatch, {})
    assert engine_info.info(1) == {"port": 1, "error": "no /props on 127.0.0.1:1"}
    assert engine_info.main(["--build", "--port", "1"]) == 1


def test_the_waiter_hints_once_that_a_quiet_agent_may_be_waiting_for_input(tmp_path, monkeypatch, capsys):
    run = tmp_path / "runs" / "perplexity-cloud" / "b1"
    (run / "kw-01").mkdir(parents=True)
    clock = [1000.0]
    (run / ".start-kw-01").write_text("1000", encoding="utf-8")
    monkeypatch.setattr(wait_task, "EVAL_HOME", tmp_path)
    monkeypatch.setattr(wait_task, "STALL_S", 60)
    monkeypatch.setattr(wait_task, "find_task", lambda tid: {"deliverables": ["out.md"], "max_minutes": 5})
    monkeypatch.setattr(wait_task.time, "time", lambda: clock[0])
    monkeypatch.setattr(wait_task.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + s))
    assert wait_task.main("perplexity-cloud", "b1", "kw-01") == 1  # times out: nothing was ever written
    out = capsys.readouterr().out
    assert out.count("may be waiting for your input") == 1 and "stalled-asked-human" in out


@pytest.mark.parametrize("args", [["--help"]])
def test_engine_info_has_a_cli(args):
    with pytest.raises(SystemExit) as exc:
        engine_info.main(args)
    assert exc.value.code == 0
