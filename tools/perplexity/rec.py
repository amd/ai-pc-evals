# Copyright(C) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""Record the primary display with ffmpeg until a stop file appears.

usage: python rec.py <out.mp4>
stop:  create <out.mp4>.stop  (ffmpeg gets 'q' so the mp4 is finalized)

A window-title capture grabs Perplexity's hidden helper window (black), so
capture the primary display with the app maximized on it. REC_VIDEO_SIZE sets the
captured area from the top-left corner (default 2560x1080, the evaluation display).
"""

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

if len(sys.argv) != 2:
    sys.exit(__doc__)
if not shutil.which("ffmpeg"):
    sys.exit("ERROR: ffmpeg is not on PATH; install it (winget install Gyan.FFmpeg) to record runs")
out = Path(sys.argv[1])
out.parent.mkdir(parents=True, exist_ok=True)
stop = out.with_suffix(out.suffix + ".stop")
stop.unlink(missing_ok=True)
proc = subprocess.Popen(
    [
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "gdigrab", "-framerate", "10",
        "-offset_x", "0", "-offset_y", "0", "-video_size", os.environ.get("REC_VIDEO_SIZE", "2560x1080"),
        "-i", "desktop",
        "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
        "-pix_fmt", "yuv420p", str(out),
    ],
    stdin=subprocess.PIPE,
)
started = time.time()
while proc.poll() is None and not stop.exists():
    time.sleep(1)
if proc.poll() is None:
    proc.communicate(b"q", timeout=60)
stop.unlink(missing_ok=True)
print(f"{out} {time.time() - started:.0f}s exit={proc.returncode}")
sys.exit(proc.returncode)
