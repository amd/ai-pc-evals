# Copyright(C) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""Record the desktop with ffmpeg while an agent works, and finalize the file cleanly.

``start`` and ``stop`` are usually separate commands (``python -m kwengine
rec-start`` / ``rec-stop``), and only the process holding ffmpeg's stdin can
send the ``q`` that makes it write a playable MP4. So ``start`` launches a
small detached supervisor that owns ffmpeg; ``stop`` drops a stop file beside
the recording, and the supervisor sends ``q`` and waits for ffmpeg to finish.
Killing ffmpeg instead leaves an MP4 with no index that most players reject.

Files beside ``<name>.mp4``: ``.pid`` while recording, ``.log`` (ffmpeg's
stderr), ``.status`` (the exit code), and ``.stop`` while stopping.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

POLL_S = 0.2
READY_TIMEOUT_S = 15
FINALIZE_TIMEOUT_S = 60
#: The repo root: the supervisor imports this checkout's kwengine.
SRC_ROOT = Path(__file__).resolve().parents[1]


def _sidecar(path: Path, suffix: str) -> Path:
    return path.with_name(path.name + suffix)


def find_ffmpeg() -> str:
    found = shutil.which("ffmpeg")
    if not found:
        raise FileNotFoundError(
            "Screen recording needs ffmpeg on PATH. Install it (Windows: "
            "`winget install Gyan.FFmpeg`), open a new terminal, and retry."
        )
    return found


def ffmpeg_command(ffmpeg: str, path: Path, fps: int) -> List[str]:
    """The whole desktop, H.264 in yuv420p (plays everywhere), CRF 28."""
    return [
        ffmpeg,
        "-hide_banner",
        "-y",
        "-f",
        "gdigrab",
        "-framerate",
        str(fps),
        "-i",
        "desktop",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "28",
        "-pix_fmt",
        "yuv420p",
        str(path),
    ]


def _no_window() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _write_atomic(path: Path, text: str) -> None:
    """Write via a temp file and a rename: a reader (``stop``, another process) never sees a half-written file."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def read_json(path: Path, timeout: float = 5.0) -> Any:
    """A sidecar file as JSON, retried briefly: on Windows a file being renamed into place can't be opened for a
    moment, and a reader must not fail on that."""
    deadline = time.time() + timeout
    while True:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            if time.time() > deadline:
                raise
            time.sleep(0.02)


def _alive(pid: int) -> bool:
    import psutil  # pylint: disable=import-outside-toplevel

    return psutil.pid_exists(pid)


def _log_tail(path: Path, limit: int = 600) -> str:
    log = _sidecar(path, ".log")
    if not log.is_file():
        return "(no ffmpeg log)"
    return log.read_text(encoding="utf-8", errors="replace")[-limit:].strip()


def supervise(path: Path, fps: int) -> int:
    """Run ffmpeg on *path* until a stop file appears; then send ``q`` and wait."""
    pid_file, stop_file = _sidecar(path, ".pid"), _sidecar(path, ".stop")
    status_file = _sidecar(path, ".status")
    with open(_sidecar(path, ".log"), "wb") as log:
        proc = subprocess.Popen(  # pylint: disable=consider-using-with
            ffmpeg_command(find_ffmpeg(), path, fps),
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=log,
            creationflags=_no_window(),
        )
        _write_atomic(pid_file, json.dumps({"supervisor": os.getpid(), "ffmpeg": proc.pid, "fps": fps}))
        status: Dict[str, Any] = {}
        try:
            while True:
                code = proc.poll()
                if code is not None:
                    status = {"code": code, "error": "ffmpeg exited before stop"}
                    break
                if stop_file.exists():
                    proc.stdin.write(b"q")
                    proc.stdin.flush()
                    proc.stdin.close()
                    try:
                        status = {"code": proc.wait(timeout=FINALIZE_TIMEOUT_S)}
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()
                        status = {
                            "code": -1,
                            "error": f"ffmpeg did not finalize within "
                            f"{FINALIZE_TIMEOUT_S}s and was killed",
                        }
                    break
                time.sleep(POLL_S)
        finally:
            _write_atomic(status_file, json.dumps(status))
            pid_file.unlink(missing_ok=True)
            stop_file.unlink(missing_ok=True)
    return int(status.get("code", -1))


def start(path: Path, fps: int = 15) -> Dict[str, Any]:
    """Start recording the desktop to *path*; return once ffmpeg is running."""
    if sys.platform != "win32":
        raise RuntimeError(
            "Screen recording uses ffmpeg's gdigrab, which is Windows-only."
        )
    find_ffmpeg()
    path = Path(path).resolve()
    pid_file = _sidecar(path, ".pid")
    if pid_file.is_file():
        info = read_json(pid_file)
        if _alive(int(info["supervisor"])):
            raise RuntimeError(
                f"{path} is already being recorded (supervisor pid "
                f"{info['supervisor']}). Stop it with `python -m kwengine rec-stop "
                f"{path}`."
            )
        pid_file.unlink()
    path.parent.mkdir(parents=True, exist_ok=True)
    for suffix in (".stop", ".status"):
        _sidecar(path, suffix).unlink(missing_ok=True)
    env = dict(os.environ)
    # The supervisor must import this checkout's kwengine, not another copy's.
    env["PYTHONPATH"] = os.pathsep.join(
        [str(SRC_ROOT), *filter(None, [env.get("PYTHONPATH")])]
    )
    subprocess.Popen(  # pylint: disable=consider-using-with
        [
            sys.executable,
            "-m",
            "kwengine.screen_record",
            "supervise",
            str(path),
            "--fps",
            str(fps),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
        creationflags=_no_window() | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )
    deadline = time.time() + READY_TIMEOUT_S
    status_file = _sidecar(path, ".status")
    while time.time() < deadline:
        if pid_file.is_file():
            return read_json(pid_file)
        if status_file.is_file():
            break
        time.sleep(POLL_S)
    raise RuntimeError(
        f"The recorder did not start within {READY_TIMEOUT_S}s. ffmpeg said: "
        f"{_log_tail(path)}"
    )


def stop(path: Path, timeout: int = FINALIZE_TIMEOUT_S + 30) -> Path:
    """Stop the recording of *path* cleanly; return it once it is a finished file."""
    path = Path(path).resolve()
    pid_file, status_file = _sidecar(path, ".pid"), _sidecar(path, ".status")
    if not pid_file.is_file():
        raise RuntimeError(
            f"Nothing is recording to {path} (no {pid_file.name}). Start one with "
            "`python -m kwengine rec-start`."
        )
    _sidecar(path, ".stop").touch()
    deadline = time.time() + timeout
    while pid_file.is_file():
        if time.time() > deadline:
            raise RuntimeError(
                f"The recorder of {path} did not stop within {timeout}s. Its "
                f"supervisor pid is in {pid_file}; ffmpeg said: {_log_tail(path)}"
            )
        time.sleep(POLL_S)
    status = (
        read_json(status_file)
        if status_file.is_file()
        else {"code": -1, "error": "the supervisor left no status"}
    )
    if status.get("code") != 0:
        raise RuntimeError(
            f"Recording {path} failed (exit {status.get('code')}): "
            f"{status.get('error', '')} ffmpeg said: {_log_tail(path)}"
        )
    if not path.is_file() or path.stat().st_size == 0:
        raise RuntimeError(f"ffmpeg exited cleanly but wrote no video at {path}.")
    return path


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m kwengine.screen_record")
    sub = parser.add_subparsers(dest="action", required=True)
    run = sub.add_parser("supervise", help="Internal: own ffmpeg until stopped")
    run.add_argument("path")
    run.add_argument("--fps", type=int, default=15)
    args = parser.parse_args(argv)
    return supervise(Path(args.path), args.fps)


if __name__ == "__main__":
    sys.exit(main())
