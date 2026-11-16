# Copyright(C) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""#85: without psutil a run used to get through preflight and a whole task, then crash at the time cap and leave
the agent running. Every entry point that runs or grades agents now stops first, with one line."""
import signal
import sys

import pytest

import grade_run
import run_eval
from agents import base
from kwengine import cli


@pytest.fixture()
def no_psutil(monkeypatch):
    monkeypatch.setitem(sys.modules, "psutil", None)  # import psutil now raises ImportError


def test_running_or_grading_without_psutil_stops_before_any_work(no_psutil, monkeypatch, capsys):
    monkeypatch.setattr(run_eval, "run", lambda *a, **k: pytest.fail("an agent ran"))
    monkeypatch.setattr(run_eval, "regrade", lambda *a, **k: pytest.fail("a run was graded"))
    for argv in (["--agent", "claude-code", "--suite", "smoke"], ["--grade-only", "somewhere"]):
        assert run_eval.main(argv) == 1
        err = capsys.readouterr().err
        assert err.startswith("ERROR: psutil is missing") and "pip install -r requirements.txt" in err
        assert "Traceback" not in err


def test_listing_tasks_needs_no_psutil(no_psutil, capsys):
    assert run_eval.main(["--list-tasks", "smoke"]) == 0
    assert "kw-00-smoke" in capsys.readouterr().out


def test_grade_run_and_the_engine_stop_without_psutil(no_psutil, tmp_path, capsys):
    assert grade_run.main([str(tmp_path), "--agent-closed"]) == 2
    assert "psutil is missing" in capsys.readouterr().err
    with pytest.raises(SystemExit) as stop:
        cli.main(["grade", str(tmp_path)])
    assert stop.value.code == 2 and "psutil is missing" in capsys.readouterr().out


def test_missing_psutil_says_so_only_when_it_is(monkeypatch):
    assert base.missing_psutil() is None
    monkeypatch.setitem(sys.modules, "psutil", None)
    assert sys.executable in base.missing_psutil()


@pytest.mark.parametrize("platform", ["win32", "linux"])
def test_an_agent_still_stops_at_its_cap_if_psutil_fails_mid_run(no_psutil, monkeypatch, platform):
    """Last resort only: the OS's tree kill on Windows, the process itself elsewhere."""
    seen = []
    monkeypatch.setattr(base.sys, "platform", platform)
    monkeypatch.setattr(base.subprocess, "run", lambda cmd, **kw: seen.append(cmd))
    monkeypatch.setattr(base.os, "kill", lambda pid, sig: seen.append((pid, sig)), raising=False)
    if platform != "win32" and not hasattr(signal, "SIGKILL"):
        monkeypatch.setattr(signal, "SIGKILL", 9, raising=False)
    base.kill_tree(4321)
    assert seen == ([["taskkill", "/T", "/F", "/PID", "4321"]] if platform == "win32" else [(4321, signal.SIGKILL)])


def test_a_process_already_gone_is_not_an_error(no_psutil, monkeypatch):
    monkeypatch.setattr(base.sys, "platform", "linux")
    if not hasattr(signal, "SIGKILL"):
        monkeypatch.setattr(signal, "SIGKILL", 9, raising=False)

    def gone(pid, sig):
        raise ProcessLookupError(pid)
    monkeypatch.setattr(base.os, "kill", gone)
    base.kill_tree(4321)
