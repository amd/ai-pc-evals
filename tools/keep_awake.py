# Copyright(C) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""Keep the PC awake while agents run and while grading, and notice when it slept anyway.

A PC that sleeps mid-run (Windows Modern Standby froze one for 30 minutes) pauses the agent and the runner, which
inflates wall time and can push a task past its time cap. ``awake()`` asks the OS not to sleep on idle until the
block ends, however it ends:

* Windows: ``SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)``, reset to ``ES_CONTINUOUS`` on exit.
  On some Modern Standby PCs the display turning off still sends the PC to standby; ``KW_KEEP_DISPLAY_ON=1`` adds
  ``ES_DISPLAY_REQUIRED`` (the screen stays on for the run).
* Linux: ``systemd-inhibit --what=sleep:idle`` held by a child process; without systemd-inhibit, nothing is held
  (said once). macOS: ``caffeinate -i``.

None of it stops a sleep someone asks for (closing the lid, the power button). So ``awake()`` also runs a heartbeat
thread that notices when the process was frozen (sleep, standby, a suspended VM): a heartbeat that comes more than
``GAP_S`` seconds late. ``slept_seconds()`` is the total so far, for a task to record the part that fell in it.
Standard library only.
"""
from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import sys
import threading
import time
from typing import Callable, Iterator, List, Optional

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002
#: A heartbeat this late (seconds) means the process was frozen: the PC slept, or was suspended.
GAP_S = 30.0
HEARTBEAT_S = 1.0


class SleepWatch:
    """A heartbeat thread adding up the time the process was frozen: each beat that comes more than *gap_s* late
    adds the whole late part. The wall clock (time.time) is used, not a monotonic one: on Linux CLOCK_MONOTONIC stops
    while the PC sleeps, so it can't see the sleep."""

    def __init__(self, gap_s: float = GAP_S, every_s: float = HEARTBEAT_S,
                 clock: Callable[[], float] = time.time) -> None:
        self.gap_s, self.every_s, self.clock = gap_s, every_s, clock
        self.gaps: List[float] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last = clock()

    def beat(self) -> None:
        """One heartbeat: count the time since the last one beyond the expected beat, when it is past the gap."""
        now = self.clock()
        late = now - self._last - self.every_s
        if late > self.gap_s:
            with self._lock:
                self.gaps.append(late)
        self._last = now

    def total(self) -> float:
        with self._lock:
            return sum(self.gaps)

    def start(self) -> "SleepWatch":
        self._last = self.clock()
        self._thread = threading.Thread(target=self._loop, name="kw-sleep-watch", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _loop(self) -> None:
        while not self._stop.wait(self.every_s):
            self.beat()


_watch: Optional[SleepWatch] = None


def slept_seconds() -> float:
    """Seconds the process was frozen since ``awake()`` began; 0 outside it."""
    return _watch.total() if _watch is not None else 0.0


def _windows_hold(display: bool) -> Optional[Callable[[], None]]:
    import ctypes  # noqa: PLC0415

    flags = ES_CONTINUOUS | ES_SYSTEM_REQUIRED | (ES_DISPLAY_REQUIRED if display else 0)
    kernel32 = ctypes.windll.kernel32
    kernel32.SetThreadExecutionState.restype = ctypes.c_uint
    kernel32.SetThreadExecutionState.argtypes = [ctypes.c_uint]
    if not kernel32.SetThreadExecutionState(flags):
        return None
    return lambda: kernel32.SetThreadExecutionState(ES_CONTINUOUS)


def _child_hold(argv: List[str]) -> Optional[Callable[[], None]]:
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
    except OSError:
        return None

    def release() -> None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    return release


def hold(platform: str = sys.platform, env: Optional[dict] = None,
         which: Callable[[str], Optional[str]] = shutil.which) -> tuple:
    """Ask the OS not to sleep on idle: (release, note). *release* undoes it (None when nothing is held); *note*
    says what is held, or why nothing is."""
    env = os.environ if env is None else env
    if platform == "win32":
        display = env.get("KW_KEEP_DISPLAY_ON", "").lower() in ("1", "true", "yes")
        release = _windows_hold(display)
        what = "system" + (" and display" if display else "")
        return release, (f"keeping the PC awake ({what} required)" if release
                         else "could not keep the PC awake (SetThreadExecutionState failed)")
    if platform == "darwin" and which("caffeinate"):
        release = _child_hold(["caffeinate", "-i", "-w", str(os.getpid())])
        return release, "keeping the Mac awake (caffeinate)" if release else "caffeinate did not start"
    if which("systemd-inhibit"):
        release = _child_hold(["systemd-inhibit", "--what=sleep:idle", "--who=ai-pc-evals",
                               "--why=an eval run is in progress", "--mode=block", "sleep", "infinity"])
        return release, "keeping the PC awake (systemd-inhibit)" if release else "systemd-inhibit did not start"
    return None, ("nothing keeps the PC awake (no systemd-inhibit here): turn off sleep on idle for the run, "
                  "or a sleep shows in the results as time the PC slept")


@contextlib.contextmanager
def awake(say: Callable[[str], None] = lambda line: print(f"  {line}", flush=True)) -> Iterator[SleepWatch]:
    """Hold the PC awake and watch for sleep until the block ends, on an error too. Nested uses share the outer
    one."""
    global _watch  # noqa: PLW0603 - one watch per process, read by slept_seconds()
    if _watch is not None:
        yield _watch
        return
    try:
        release, note = hold()
    except Exception as e:  # noqa: BLE001 - keeping awake is best effort: never stop a run over it
        release, note = None, f"could not keep the PC awake ({type(e).__name__}: {e})"
    say(note)
    _watch = SleepWatch().start()
    try:
        yield _watch
    finally:
        watch, _watch = _watch, None
        watch.stop()
        if release is not None:
            with contextlib.suppress(Exception):
                release()
