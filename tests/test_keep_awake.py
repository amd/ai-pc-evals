# Copyright(C) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""The PC is kept awake while agents run and grading runs, released however the run ends, and a sleep is noticed."""
import sys

import pytest

import isolation_helpers  # noqa: F401  (puts tools/ on sys.path)
import keep_awake
import run_eval


def test_a_late_heartbeat_counts_as_sleep_and_a_slightly_late_one_does_not():
    now = [1000.0]
    watch = keep_awake.SleepWatch(gap_s=30, every_s=1, clock=lambda: now[0])
    for step in (1.0, 1.2, 5.0, 1801.0, 1.0):  # a busy second or two, then 30 minutes in Modern Standby
        now[0] += step
        watch.beat()
    assert watch.total() == pytest.approx(1800.0)


@pytest.fixture
def fake_hold(monkeypatch):
    released = []
    monkeypatch.setattr(keep_awake, "hold", lambda: (lambda: released.append(True), "keeping the PC awake (test)"))
    return released


def test_awake_is_released_on_an_error_too(fake_hold):
    with pytest.raises(RuntimeError):
        with keep_awake.awake(say=lambda line: None) as watch:
            assert keep_awake.slept_seconds() == 0.0 and watch is keep_awake._watch
            raise RuntimeError("an agent crashed")
    assert fake_hold == [True] and keep_awake._watch is None and keep_awake.slept_seconds() == 0.0


def test_nested_awake_holds_once(fake_hold):
    with keep_awake.awake(say=lambda line: None) as outer:
        with keep_awake.awake(say=lambda line: None) as inner:
            assert inner is outer
        assert fake_hold == []
    assert fake_hold == [True]


def test_a_failed_hold_never_stops_the_run(monkeypatch):
    def broken():
        raise OSError("no power API here")
    monkeypatch.setattr(keep_awake, "hold", broken)
    said = []
    with keep_awake.awake(say=said.append):
        pass
    assert "could not keep the PC awake" in said[0]


def test_linux_holds_a_systemd_inhibit_lock_or_says_it_cannot(monkeypatch):
    started = []
    monkeypatch.setattr(keep_awake, "_child_hold", lambda argv: started.append(argv) or (lambda: None))
    release, note = keep_awake.hold("linux", {}, which=lambda name: "/usr/bin/" + name)
    assert release and "systemd-inhibit" in note
    assert started[0][:2] == ["systemd-inhibit", "--what=sleep:idle"]
    release, note = keep_awake.hold("linux", {}, which=lambda name: None)
    assert release is None and "nothing keeps the PC awake" in note


@pytest.mark.skipif(sys.platform != "win32", reason="SetThreadExecutionState is Windows'")
def test_windows_asks_for_the_system_and_resets_it():
    import ctypes

    k = ctypes.windll.kernel32
    k.SetThreadExecutionState.restype = ctypes.c_uint
    k.SetThreadExecutionState.argtypes = [ctypes.c_uint]
    release, note = keep_awake.hold("win32", {})
    try:
        held = k.SetThreadExecutionState(keep_awake.ES_CONTINUOUS | keep_awake.ES_SYSTEM_REQUIRED)  # the state before
        assert held & keep_awake.ES_SYSTEM_REQUIRED and not held & keep_awake.ES_DISPLAY_REQUIRED
        assert "system required" in note
    finally:
        release()
    assert k.SetThreadExecutionState(keep_awake.ES_CONTINUOUS) == keep_awake.ES_CONTINUOUS  # nothing held after


def test_run_eval_keeps_the_pc_awake_while_grading_and_lets_go_when_it_fails(fake_hold, monkeypatch, tmp_path):
    def regrade(*args, **kw):
        assert keep_awake._watch is not None and fake_hold == []  # held while grading
        raise run_eval.EvalError("the judge is unreachable")
    monkeypatch.setattr(run_eval, "regrade", regrade)
    monkeypatch.setattr(run_eval, "missing_psutil", lambda: None)
    assert run_eval.main(["--grade-only", str(tmp_path)]) == 1
    assert fake_hold == [True] and keep_awake._watch is None
