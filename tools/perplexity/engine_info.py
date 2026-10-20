"""What the running local engine is: build, context size and model, read from the server itself.

usage: python engine_info.py [--port N] [--build]

Asks a llama.cpp-compatible server on 127.0.0.1 for /props (build_info, n_ctx, model path) and
/metrics (token counters; only when the server runs with --metrics). Without --port it asks every
llama-server running on this PC (tools/local_servers.py). Prints JSON; --build prints only the
first server's build, for `kwengine record ... --engine-build "$(python engine_info.py --build)"`.

The install folder can hold a newer engine than the one running (the app downloads a package while
the old engine keeps serving), so the build comes from the server, never from the disk.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import local_servers  # noqa: E402

HOST = "127.0.0.1"  # never anything else: this asks the engine on this PC
TIMEOUT_S = 3


def fetch(port: int, path: str) -> Optional[str]:
    """The body of http://127.0.0.1:<port><path>, or None when the server doesn't answer it."""
    try:
        with urllib.request.urlopen(f"http://{HOST}:{int(port)}{path}", timeout=TIMEOUT_S) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except (OSError, urllib.error.URLError, ValueError):
        return None


def metrics(text: str) -> Dict[str, float]:
    """Prometheus text as ``{name: value}``: llamacpp:prompt_tokens_total and the like."""
    found = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        name, _, value = line.rpartition(" ")
        try:
            found[name.strip()] = float(value)
        except ValueError:
            continue
    return found


def info(port: int) -> Dict[str, Any]:
    """One server's build, context size, model and counters; ``error`` when /props doesn't answer."""
    out: Dict[str, Any] = {"port": port}
    raw = fetch(port, "/props")
    try:
        props = json.loads(raw) if raw else None
    except ValueError:
        props = None
    if not isinstance(props, dict):
        out["error"] = f"no /props on {HOST}:{port}"
        return out
    settings = props.get("default_generation_settings") or {}
    out.update({
        "build_info": props.get("build_info"),
        "n_ctx": props.get("n_ctx") or settings.get("n_ctx"),
        "model_path": props.get("model_path") or settings.get("model"),
        "total_slots": props.get("total_slots"),
    })
    counters = fetch(port, "/metrics")
    out["metrics"] = metrics(counters) if counters else None  # None: the server runs without --metrics
    return out


def ports() -> List[int]:
    return [s["port"] for s in local_servers.list_servers() if s["kind"] == "llama-server" and s.get("port")]


def running_build(port: Optional[int] = None) -> Optional[str]:
    """The build of the server on *port*, or of the first running llama-server that answers."""
    for p in [port] if port else ports():
        build = info(p).get("build_info")
        if build:
            return str(build)
    return None


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=None, help="the server's port (default: every running llama-server)")
    ap.add_argument("--build", action="store_true", help="print only the build, for kwengine record --engine-build")
    args = ap.parse_args(argv)
    if args.build:
        build = running_build(args.port)
        if not build:
            print("ERROR: no llama-server answered /props on 127.0.0.1", file=sys.stderr)
            return 1
        print(build)
        return 0
    found = [info(p) for p in ([args.port] if args.port else ports())]
    print(json.dumps(found, indent=1))
    return 0 if any("error" not in f for f in found) else 1


if __name__ == "__main__":
    sys.exit(main())
