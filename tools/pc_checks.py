# Copyright(C) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""Setup checks for a long run, as preflight warnings: things that cost a whole evening when found at the end.

* The screen locks, or turns off, sooner than the longest task's cap (Windows). A person operating an app by
  computer use needs it unlocked; keep_awake.py stops idle sleep but not a lock screen or a screen saver.
* Little free disk in the eval home or the temp folder (the inputs and the agents' scratch files are large).
* The Python on PATH lacks the common data libraries an agent's scripts reach for (pandas, openpyxl, python-pptx,
  python-docx, matplotlib): a setup difference between two legs that looks like an agent difference.

Each returns warnings as plain sentences; preflight.py lists them. Standard library only.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable, List, Optional, Sequence

REPO = Path(__file__).resolve().parents[1]
TASKS_FILE = REPO / "knowledgework" / "tasks.public.json"
#: Less free space than this in the eval home or the temp folder is a warning.
MIN_FREE_GB = 20
#: What an agent's scripts most often import, as (module, package to install).
DATA_LIBRARIES = (("pandas", "pandas"), ("openpyxl", "openpyxl"), ("pptx", "python-pptx"), ("docx", "python-docx"),
                  ("matplotlib", "matplotlib"))
_POLICY_SYSTEM = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System"
_DESKTOP = r"Control Panel\Desktop"
_DESKTOP_POLICY = r"Software\Policies\Microsoft\Windows\Control Panel\Desktop"


def longest_cap_minutes(tasks_file: Path = TASKS_FILE) -> int:
    """The longest time cap of any task, in minutes (0 when the task file can't be read)."""
    try:
        tasks = json.loads(tasks_file.read_text(encoding="utf-8")).get("tasks", [])
    except (OSError, ValueError):
        return 0
    return max((int(t.get("max_minutes") or 0) for t in tasks), default=0)


def disk_warnings(paths: Sequence[Path], min_free_gb: float = MIN_FREE_GB) -> List[str]:
    """A warning for each folder (or its nearest existing parent) on a drive with less than *min_free_gb* free."""
    out, seen = [], set()
    for path in paths:
        probe = Path(path)
        while not probe.exists() and probe.parent != probe:
            probe = probe.parent
        try:
            free = shutil.disk_usage(probe).free / 1e9
        except OSError:
            continue
        key = os.path.splitdrive(os.path.abspath(probe))[0] or str(probe)
        if free < min_free_gb and key not in seen:
            seen.add(key)
            out.append(f"low disk space: {free:.1f} GB free where {path} is (under {min_free_gb} GB); task inputs "
                       "and the agents' scratch files can fill it mid-run")
    return out


def _reg(hive: str, key: str, name: str) -> Optional[str]:
    try:
        import winreg  # noqa: PLC0415
    except ImportError:
        return None
    try:
        with winreg.OpenKey(getattr(winreg, hive), key) as k:
            return str(winreg.QueryValueEx(k, name)[0])
    except OSError:
        return None


def _powercfg_minutes(setting: str) -> Optional[int]:
    """The plugged-in value of a SUB_VIDEO power setting, in minutes (0 = never), or None if it can't be read."""
    try:
        out = subprocess.run(["powercfg", "/query", "SCHEME_CURRENT", "SUB_VIDEO", setting], capture_output=True,
                             text=True, timeout=30, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"Current AC Power Setting Index:\s*0x([0-9a-fA-F]+)", out)
    return int(m.group(1), 16) // 60 if m else None


def lock_warnings(cap_minutes: int, read: Callable[[str, str, str], Optional[str]] = _reg,
                  screen_off: Callable[[str], Optional[int]] = _powercfg_minutes,
                  platform: str = sys.platform) -> List[str]:
    """Windows: a lock screen or a screen that turns off within *cap_minutes*. Elsewhere, nothing is checked."""
    if not platform.startswith("win") or cap_minutes <= 0:
        return []
    out = []
    limit = read("HKEY_LOCAL_MACHINE", _POLICY_SYSTEM, "InactivityTimeoutSecs")
    if limit and limit.isdigit() and 0 < int(limit) < cap_minutes * 60:
        out.append(f"the PC locks after {int(limit) // 60} min idle (a machine policy, InactivityTimeoutSecs), within "
                   f"the longest task's {cap_minutes}-min cap: an operator working an app by computer use stops at the "
                   "lock screen; ask the PC's admin, or keep the session active")

    def desktop(name: str) -> Optional[str]:  # a policy wins over the user's own setting
        return read("HKEY_CURRENT_USER", _DESKTOP_POLICY, name) or read("HKEY_CURRENT_USER", _DESKTOP, name)
    secure, active, wait = desktop("ScreenSaverIsSecure"), desktop("ScreenSaveActive"), desktop("ScreenSaveTimeOut")
    if secure == "1" and active != "0" and wait and wait.isdigit() and 0 < int(wait) < cap_minutes * 60:
        out.append(f"the screen saver locks the PC after {int(wait) // 60} min, within the longest task's "
                   f"{cap_minutes}-min cap (keep_awake.py doesn't stop it): turn it off, or untick \"On resume, "
                   "display logon screen\", for a run an operator drives")
    off = screen_off("VIDEOIDLE")
    if off and off < cap_minutes:
        out.append(f"the screen turns off after {off} min, within the longest task's {cap_minutes}-min cap: on a "
                   "Modern Standby PC that can send it to standby; set KW_KEEP_DISPLAY_ON=1 for the run (or "
                   "\"powercfg /change monitor-timeout-ac 0\")")
    return out


def library_warnings(python: Optional[str] = None, modules: Sequence[tuple] = DATA_LIBRARIES) -> List[str]:
    """A warning when the Python on PATH, the one an agent's scripts would run with, lacks a data library."""
    python = python or shutil.which("python") or shutil.which("python3")
    if not python:
        return ["no python on PATH: an agent that writes Python scripts can't run them"]
    names = [m for m, _ in modules]
    code = ("import importlib.util as u, sys; "
            f"print(','.join(m for m in {names!r} if u.find_spec(m) is None))")
    try:
        proc = subprocess.run([python, "-I", "-c", code], capture_output=True, text=True, timeout=60, check=False,
                              cwd=tempfile.gettempdir())
    except (OSError, subprocess.SubprocessError) as e:
        return [f"couldn't check the data libraries of {python}: {e}"]
    missing = [m for m in proc.stdout.strip().split(",") if m] if proc.returncode == 0 else []
    if not missing:
        return []
    packages = [p for m, p in modules if m in missing]
    return [f"the Python on PATH ({python}) lacks {', '.join(packages)}: an agent's scripts that import them fail, "
            "a setup difference between legs, not an agent one (pip install " + " ".join(packages) + ")"]


def warnings(eval_home: Path) -> List[str]:
    """Every check above, for preflight."""
    cap = longest_cap_minutes()
    return [*lock_warnings(cap), *disk_warnings([eval_home, Path(tempfile.gettempdir())]), *library_warnings()]
