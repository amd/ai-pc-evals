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
        {"kind": "ollama", "port": 11434}, {"kind": "llama-server", "port": 51234, "path": "C:/Perplexity/engine/llama-server.exe"}])
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


def test_an_answer_only_task_waits_for_enter_or_a_done_mark_not_30_seconds(tmp_path, monkeypatch, capsys):
    import threading

    run = tmp_path / "runs" / "perplexity-cloud" / "b1"
    (run / "kw-02").mkdir(parents=True)
    (run / ".start-kw-02").write_text("1000", encoding="utf-8")
    clock = [1000.0]
    monkeypatch.setattr(wait_task, "EVAL_HOME", tmp_path)
    monkeypatch.setattr(wait_task, "find_task", lambda tid: {"deliverables": [], "max_minutes": 30})
    monkeypatch.setattr(wait_task.time, "time", lambda: clock[0])
    pressed = threading.Event()

    def sleep(s):  # the operator presses Enter four minutes in
        clock[0] += s
        if clock[0] >= 1240:
            pressed.set()
    monkeypatch.setattr(wait_task.time, "sleep", sleep)
    monkeypatch.setattr(wait_task, "_enter_pressed", lambda: pressed)
    assert wait_task.main("perplexity-cloud", "b1", "kw-02") == 0
    assert (run / ".done-kw-02").read_text(encoding="utf-8").strip() == "1240"
    assert "press Enter" in capsys.readouterr().out
    # Or a done mark made by hand (empty: its own time is taken), and nothing at all times out at the cap.
    (run / ".done-kw-02").write_text("", encoding="utf-8")
    import os
    os.utime(run / ".done-kw-02", (1300, 1300))
    monkeypatch.setattr(wait_task, "_enter_pressed", lambda: threading.Event())
    assert wait_task.main("perplexity-cloud", "b1", "kw-02") == 0
    assert (run / ".done-kw-02").read_text(encoding="utf-8").strip() == "1300"
    (run / ".done-kw-02").unlink()
    assert wait_task.main("perplexity-cloud", "b1", "kw-02") == 1 and "TIMEOUT" in capsys.readouterr().out


def test_the_waiter_ignores_a_file_deleted_while_it_looks(tmp_path, monkeypatch):
    folder = tmp_path / "t"
    folder.mkdir()
    (folder / "keep.md").write_text("x", encoding="utf-8")
    gone = folder / "~$lock.xlsx"
    gone.write_text("x", encoding="utf-8")
    real_stat = type(gone).stat

    def stat(self, *a, **k):
        if self.name == gone.name:
            raise FileNotFoundError(2, "gone", str(self))
        return real_stat(self, *a, **k)
    monkeypatch.setattr(type(gone), "stat", stat)
    assert wait_task.latest(folder) == real_stat(folder / "keep.md").st_mtime


def _git_bash():
    import shutil

    bash = shutil.which("bash")
    if not bash or "system32" in bash.lower():  # WSL's bash.exe can't see this test's paths
        return None
    return bash


@pytest.mark.skipif(_git_bash() is None, reason="needs bash (Git Bash on Windows)")
def test_record_time_runs_to_the_done_mark_and_is_never_silently_zero(tmp_path):
    import subprocess
    from pathlib import Path

    env_sh = Path(wait_task.__file__).with_name("_env.sh").as_posix()
    run = tmp_path / "run"
    (run / "kw-02").mkdir(parents=True)
    (run / ".start-kw-02").write_text("1000\n", encoding="utf-8")

    def seconds(outcome=""):
        script = f'. "{env_sh}"; KW_OUTCOME={outcome} deliverable_seconds "{run.as_posix()}" kw-02'
        return subprocess.run([_git_bash(), "-c", script], capture_output=True, text=True)

    out = seconds()
    assert out.returncode != 0 and "done mark" in out.stderr  # nothing to time it by: an error, not 0
    assert seconds("did-not-run").stdout.strip() == "0"
    (run / ".done-kw-02").write_text("1240\n", encoding="utf-8")
    assert seconds().stdout.strip() == "240"


@pytest.mark.parametrize("args", [["--help"]])
def test_engine_info_has_a_cli(args):
    with pytest.raises(SystemExit) as exc:
        engine_info.main(args)
    assert exc.value.code == 0


def test_the_engine_build_is_perplexitys_never_another_apps(monkeypatch):
    import engine_info

    def server(port, path):
        return {"pid": port, "kind": "llama-server", "name": "llama-server", "model": "", "port": port, "path": path}
    builds = {8001: "b1-lemonade", 9100: "b10825-pplx"}
    monkeypatch.setattr(engine_info, "info", lambda p: {"build_info": builds.get(p)})
    lemonade = server(8001, "C:/Users/me/AppData/Local/lemonade_server/bin/llama-server.exe")
    pplx = server(9100, "C:/Users/me/AppData/Local/Perplexity/engine/2026.09.28/llama-server.exe")
    monkeypatch.setattr(engine_info.local_servers, "list_servers", lambda: [lemonade, pplx])
    assert engine_info.running_build() == "b10825-pplx"
    monkeypatch.setattr(engine_info.local_servers, "list_servers", lambda: [lemonade, server(8002, "D:/x/llama-server")])
    assert engine_info.running_build() is None  # several, none of them Perplexity's: record nothing
    monkeypatch.setattr(engine_info.local_servers, "list_servers", lambda: [lemonade])
    assert engine_info.running_build() is None  # the only engine running is not Perplexity's
    assert engine_info.running_build(port=8001) == "b1-lemonade"  # unless asked for by port
