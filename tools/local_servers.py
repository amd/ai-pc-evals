"""Local model servers running on this PC, and its free memory: what shares the GPU with an agent under test.

    python tools/local_servers.py        # JSON: every llama-server, Ollama, LM Studio and Lemonade process

Each server is ``{"pid", "kind", "name", "model", "port", "path"}``, parsed from its command line; a
field the command line doesn't give is None. Uses psutil when it is installed; on Windows without
it, one PowerShell ``Get-CimInstance`` call. Elsewhere without psutil the list is empty.
"""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
from pathlib import PureWindowsPath
from typing import Any, Dict, List, Optional, Sequence

#: Process name (lowercase, no .exe) -> kind of server.
KINDS = {
    "llama-server": "llama-server",
    "ollama": "ollama",
    "ollama app": "ollama",
    "lms": "lm-studio",
    "lm studio": "lm-studio",
    "lemonade": "lemonade",
    "lemonade-server": "lemonade",
    "lemonade-server-dev": "lemonade",
    "lemonade-router": "lemonade",
}
#: Default ports, when the command line doesn't name one.
DEFAULT_PORTS = {"llama-server": 8080, "ollama": 11434, "lm-studio": 1234, "lemonade": 8000}
PS_TIMEOUT_S = 30


def _stem(name: str) -> str:
    name = PureWindowsPath(name or "").name.lower()
    return name[:-4] if name.endswith(".exe") else name


def _flag(args: Sequence[str], *names: str) -> Optional[str]:
    """The value after ``--port 8001`` or in ``--port=8001``."""
    for i, arg in enumerate(args):
        for name in names:
            if arg == name and i + 1 < len(args):
                return args[i + 1]
            if arg.startswith(name + "="):
                return arg.split("=", 1)[1]
    return None


def describe(pid: int, name: str, args: Sequence[str]) -> Optional[Dict[str, Any]]:
    """One server from its process name and command line, or None when it isn't one."""
    kind = KINDS.get(_stem(name))
    if kind is None:
        return None
    args = [a.strip('"') for a in args]
    if kind == "ollama" and _stem(name) == "ollama" and "serve" not in args[1:]:
        return None  # `ollama run` / `ollama pull`: a client, not the server
    port = _flag(args, "--port")
    model = _flag(args, "-m", "--model", "-hf", "--hf-repo", "--model-path")
    return {
        "pid": pid,
        "kind": kind,
        "name": PureWindowsPath(name).name,
        "model": model,
        "port": int(port) if port and port.isdigit() else DEFAULT_PORTS[kind],
        "path": args[0] if args else "",
    }


def _from_psutil() -> List[Dict[str, Any]]:
    import psutil  # pylint: disable=import-outside-toplevel

    found = []
    for proc in psutil.process_iter(["pid", "name"]):
        name = proc.info.get("name") or ""
        if _stem(name) not in KINDS:
            continue  # the command line only for candidates: reading every process's is slow on Windows
        try:
            args = proc.cmdline()
        except (psutil.Error, OSError):
            args = []
        server = describe(proc.info["pid"], name, args)
        if server:
            found.append(server)
    return found


def _from_powershell() -> List[Dict[str, Any]]:
    names = " OR ".join(f"Name LIKE '{n}%'" for n in sorted({n.split()[0] for n in KINDS}))
    cmd = ["powershell", "-NoProfile", "-Command",
           f'Get-CimInstance Win32_Process -Filter "{names}" | Select-Object ProcessId,Name,CommandLine '
           "| ConvertTo-Json -Compress"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                             timeout=PS_TIMEOUT_S).stdout.strip()
        rows = json.loads(out) if out else []
    except (OSError, subprocess.SubprocessError, ValueError):
        return []
    found = []
    for row in rows if isinstance(rows, list) else [rows]:
        try:
            args = shlex.split(row.get("CommandLine") or "", posix=False)
        except ValueError:
            args = (row.get("CommandLine") or "").split()
        server = describe(int(row.get("ProcessId") or 0), row.get("Name") or "", args)
        if server:
            found.append(server)
    return found


def list_servers() -> List[Dict[str, Any]]:
    """Every local model server running now; [] when processes can't be listed."""
    try:
        return _from_psutil()
    except ImportError:
        return _from_powershell() if sys.platform == "win32" else []
    except Exception:  # noqa: BLE001 - a listing that fails must not stop a run
        return []


def memory() -> Optional[Dict[str, float]]:
    """Free and total system memory in GB, or None when it can't be read."""
    try:
        import psutil  # pylint: disable=import-outside-toplevel

        vm = psutil.virtual_memory()
        return {"free_gb": round(vm.available / 2**30, 1), "total_gb": round(vm.total / 2**30, 1)}
    except Exception:  # noqa: BLE001 - psutil missing or failing: say nothing rather than guess
        return None


def label(server: Dict[str, Any]) -> str:
    """``llama-server pid 1234 (port 8001, model Qwen3-30B.gguf)``, noting whose engine it looks like."""
    bits = [f"port {server['port']}" if server.get("port") else "",
            f"model {PureWindowsPath(server['model']).name}" if server.get("model") else ""]
    path = (server.get("path") or "").lower()
    owner = next((o for o in ("perplexity", "lemonade", "gaia") if o in path), "")
    if owner:
        bits.append(f"{owner}'s install")
    detail = ", ".join(b for b in bits if b)
    return f"{server['kind']} pid {server['pid']}" + (f" ({detail})" if detail else "")


if __name__ == "__main__":
    print(json.dumps({"servers": list_servers(), "memory": memory()}, indent=1))
