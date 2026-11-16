# Copyright(C) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""What one agent task cost the local model server and the GPU: run_eval.py --telemetry.

For each task, run_eval.py reads the model server's Prometheus /metrics before and after the agent runs and turns
the counter deltas into tokens in and out and prefill and decode speed. Servers read (SOURCES says which counter
feeds which field):

    llama-server      started with --metrics
    vLLM              always serves /metrics
    SGLang            started with --enable-metrics

The server is found from its process (tools/local_servers.py; a vLLM or SGLang in a Docker container on Linux shows
there too, with its in-container port, which the serving skill maps to the same host port). When no such process is
listed (Docker Desktop, a server psutil can't see), vLLM's and SGLang's default ports, 8000 and 30000, are asked for
/metrics, and the metric names say which server answered.

tokens_in counts the prompt tokens the server computed: llama-server never counts a prompt token it reuses from its
cache, so vLLM's and SGLang's prefix-cache hits are taken off their prompt counts. prefill tok/s = tokens_in /
prefill seconds; decode tok/s = tokens_out / decode seconds. The seconds are each request's time in that phase,
added up over requests (vLLM's prefill/decode histograms; SGLang's time to first token, which includes queueing, and
end-to-end latency minus it).

The GPU is sampled every second in a background thread while the agent works:

    AMD, Linux        sysfs: mem_info_vram_used, gpu_busy_percent, hwmon power1_average
    NVIDIA            one long-running nvidia-smi --query-gpu ... -lms 1000 (Linux and Windows)
    AMD, Windows      the GPU performance counters, one long-running typeperf (no power counter exists)

After the agent stops, the GPU fault events logged while it ran are counted (``faults``: GPU resets, page faults,
timeouts, other), read once from the kernel log (journalctl -k, else dmesg: amdgpu's reset, page fault and ring
timeout lines, NVIDIA's NVRM Xid lines with their numbers) or, on Windows, the System event log (Display 4101, a
display driver timeout, and errors and warnings from amdkmdag, amdwddmg and nvlddmkm).

``context`` is the server's window for one request (llama-server: a slot's n_ctx; vLLM and SGLang: max_model_len)
and, for llama-server, the largest prompt any request sent while the agent worked, from /slots polled every second;
run_eval.py merges it with what the agent's own output says (tools/agents/trace.py).

The result is a ``telemetry`` block in the task's result.json and one printed line. Anything that can't be
measured is null, with the reason in ``reason``; never 0. Telemetry reads the model server and the GPU only, never
the agent's process, and never changes a score.

On a multi-GPU box the totals cover the GPUs the model server and its child processes use (amdgpu fdinfo, or KFD's
per-process sysfs for a server in a ROCm container, whose root-owned fdinfo can't be read; nvidia-smi's compute
apps; or the server's GPU engine counters), else every GPU; ``gpu_scope`` says which, and
``per_gpu`` has each GPU on its own.

    python tools/telemetry.py [SECONDS]     # sample for SECONDS (default 5) and print the block
"""

from __future__ import annotations

import base64
import csv
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from statistics import mean
from typing import Any, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent / "perplexity"))
import engine_info  # noqa: E402
import isolation  # noqa: E402
import local_servers  # noqa: E402

INTERVAL_S = 1.0
GIB = 2**30
CMD_TIMEOUT_S = 15
#: Each server field: the /metrics counters whose deltas are added up, minus those marked "-". A metric with labels
#: (vLLM's engine and model_name, SGLang's is_streaming and cache_source) is summed over its label sets; a histogram
#: is read by its _sum. Names checked against vLLM 0.29.0 and SGLang 0.5.15.post1 and 0.5.23.
SOURCES: Dict[str, Dict[str, Tuple[str, ...]]] = {
    "llama-server": {"tokens_in": ("llamacpp:prompt_tokens_total",),
                     "prefill_s": ("llamacpp:prompt_seconds_total",),
                     "tokens_out": ("llamacpp:tokens_predicted_total",),
                     "decode_s": ("llamacpp:tokens_predicted_seconds_total",)},
    "vllm": {"tokens_in": ("vllm:prompt_tokens_total", "-vllm:prompt_tokens_cached_total"),
             "prefill_s": ("vllm:request_prefill_time_seconds_sum",),
             "tokens_out": ("vllm:generation_tokens_total",),
             "decode_s": ("vllm:request_decode_time_seconds_sum",)},
    "sglang": {"tokens_in": ("sglang:prompt_tokens_total", "-sglang:cached_tokens_total"),
               "prefill_s": ("sglang:time_to_first_token_seconds_sum",),
               "tokens_out": ("sglang:generation_tokens_total",),
               "decode_s": ("sglang:e2e_request_latency_seconds_sum", "-sglang:time_to_first_token_seconds_sum")},
}
#: Prefix-cache hit counters: SGLang creates its counter on the first hit, so one that isn't there is 0.
CACHE_HITS = ("vllm:prompt_tokens_cached_total", "sglang:cached_tokens_total")
#: Why a running server may not answer /metrics.
NO_METRICS = {"llama-server": "start it with --metrics", "sglang": "start it with --enable-metrics",
              "vllm": "vLLM always serves it: it may still be loading, or its container maps another host port"}
#: Asked for /metrics when no server process is listed; the metric names say which server answered.
PROBE_PORTS = {"vllm": 8000, "sglang": 30000}
METRIC_PREFIX = {"llamacpp": "llama-server", "vllm": "vllm", "sglang": "sglang"}
WIN_COUNTERS = (r"\GPU Adapter Memory(*)\Dedicated Usage", r"\GPU Adapter Memory(*)\Shared Usage",
                r"\GPU Engine(*)\Utilization Percentage")


def _read(path: str) -> Optional[str]:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read().strip()
    except OSError:
        return None


def _num(text: Optional[str]) -> Optional[float]:
    try:
        return float(str(text).strip())
    except (TypeError, ValueError):
        return None  # "[N/A]", "", a missing file


def _run(cmd: Sequence[str]) -> str:
    try:
        return subprocess.run(list(cmd), capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=CMD_TIMEOUT_S).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


# ---------------------------------------------------------------- the model server's counters

def _names(kind: str) -> List[str]:
    return [n.lstrip("-") for spec in SOURCES[kind].values() for n in spec]


def totals(text: str) -> Dict[str, float]:
    """Prometheus text as ``{metric name: value}``, each name summed over its label sets."""
    found: Dict[str, float] = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        head, _, value = line.rpartition(" ")
        name, v = re.split(r"[{\s]", head.strip(), maxsplit=1)[0], _num(value)
        if name and v is not None and abs(v) != float("inf") and v == v:  # v == v: not NaN
            found[name] = found.get(name, 0.0) + v
    return found


def metrics_kind(text: str) -> Optional[str]:
    """Which server wrote this /metrics text, from its metric names."""
    m = re.search(r"^(?:# (?:HELP|TYPE) )?(llamacpp|vllm|sglang):", text, re.M)
    return METRIC_PREFIX[m.group(1)] if m else None


def read_counters(port: int, kind: str = "llama-server") -> Optional[Dict[str, float]]:
    """The counters SOURCES[kind] reads, from /metrics (a counter not created yet is left out), or None when
    /metrics doesn't answer or isn't that server's."""
    text = engine_info.fetch(port, "/metrics")
    if not text or metrics_kind(text) != kind:
        return None
    found = totals(text)
    return {n: found[n] for n in _names(kind) if n in found}


def probe() -> List[Tuple[Dict[str, Any], Dict[str, float]]]:
    """vLLM or SGLang answering /metrics on its default port, with no process listed (Docker Desktop, no psutil)."""
    found = []
    for kind, port in PROBE_PORTS.items():
        text = engine_info.fetch(port, "/metrics")
        if text and metrics_kind(text) == kind:
            model = re.search(r'model_name="([^"]+)"', text)
            server = {"pid": None, "kind": kind, "name": None, "model": model.group(1) if model else None,
                      "port": port, "path": ""}
            found.append((server, {n: v for n, v in totals(text).items() if n in _names(kind)}))
    return found


def _bare_user(user: Optional[str]) -> str:
    """'PC\\name', 'name@domain' or 'name' as 'name', lowercased; "" for None."""
    return (user or "").split("@")[0].replace("/", "\\").split("\\")[-1].lower()


def _agent_user() -> str:
    """The separate agent user's bare name, or "" when agents run as the maintainer."""
    try:
        return _bare_user(isolation.agent_user())
    except OSError:  # a misconfigured agent user: the runner reports that itself
        return ""


def find_server(servers: Optional[List[Dict[str, Any]]] = None
                ) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, float]], Optional[str]]:
    """The model server to measure, its counters now, and why there is none. A server the separate agent user
    runs is never measured: it may be one an earlier task's agent left behind, serving made-up /metrics."""
    servers = local_servers.list_servers() if servers is None else servers
    agent = _agent_user()
    theirs = [s for s in servers if agent and _bare_user(s.get("user")) == agent]
    servers = [s for s in servers if s not in theirs]
    listed = [s for s in servers if s["kind"] in SOURCES and s.get("port")]
    if not listed and theirs:  # don't probe the ports either: what answers there may be the agent's
        return None, None, (f"only the agent user {agent} runs a model server "
                            f"({', '.join(str(s['pid']) for s in theirs)}): not measured")
    if listed:
        answering = [(s, c) for s in listed for c in [read_counters(s["port"], s["kind"])] if c is not None]
    else:
        answering = probe()
        if not answering:
            others = sorted({s["kind"] for s in servers})
            return None, None, "no llama-server, vLLM or SGLang running" + (
                f" (found {', '.join(others)}, whose counters aren't read)" if others else "") + (
                f"; nothing answers /metrics on ports {' or '.join(str(p) for p in PROBE_PORTS.values())}")
    if len(answering) == 1:
        return answering[0][0], answering[0][1], None
    where = ", ".join(f"{s['kind']} port {s['port']}" for s in (listed or [s for s, _ in answering]))
    if not answering:
        hints = "; ".join(dict.fromkeys(NO_METRICS[s["kind"]] for s in listed))
        return listed[0] if len(listed) == 1 else None, None, f"{where}: no /metrics: {hints}"
    return None, None, f"several servers answer /metrics ({where}): can't tell which one the agent uses"


def server_block(server: Optional[Dict[str, Any]], before: Optional[Dict[str, float]],
                 after: Optional[Dict[str, float]], reason: Optional[str]) -> Dict[str, Any]:
    """Token counts and prefill/decode speed from two counter readings."""
    kind = (server or {}).get("kind")
    out: Dict[str, Any] = {"kind": kind, "port": (server or {}).get("port"),
                           "model": Path(str(server["model"]).replace("\\", "/")).name
                           if server and server.get("model") else None,
                           "tokens_in": None, "tokens_out": None, "prefill_tok_s": None, "decode_tok_s": None,
                           "reason": reason}
    if reason:
        return out
    if after is None or before is None or kind not in SOURCES:
        out["reason"] = "the server stopped answering /metrics during the task"
        return out
    if any(after[n] < before[n] for n in after if n in before):
        out["reason"] = "the server's counters went down: it restarted during the task"
        return out
    why: List[str] = []
    d: Dict[str, Optional[float]] = {}

    def delta(name: str) -> float:  # a labelled counter appears on its first increment: one not there yet is 0
        return after.get(name, 0.0) - before.get(name, 0.0)

    for field, spec in SOURCES[kind].items():
        missing = [n.lstrip("-") for n in spec if n.lstrip("-") not in after and n.lstrip("-") not in CACHE_HITS]
        if missing:
            d[field] = None
            why.append(f"{field}: the server has no {', '.join(missing)}")
            continue
        d[field] = sum(-delta(n[1:]) if n.startswith("-") else delta(n) for n in spec)
    if d["tokens_in"] is not None:
        out["tokens_in"] = int(round(d["tokens_in"]))
    if d["tokens_out"] is not None:
        out["tokens_out"] = int(round(d["tokens_out"]))
    for name, tokens, secs, idle in (("prefill_tok_s", "tokens_in", "prefill_s", "prefill: no prompt tokens processed"),
                                     ("decode_tok_s", "tokens_out", "decode_s", "decode: no tokens generated")):
        if d[tokens] is None or d[secs] is None:
            continue
        if d[secs] > 0 and d[tokens] > 0:
            out[name] = round(d[tokens] / d[secs], 1)
        else:
            why.append(idle)
    out["reason"] = "; ".join(why) or None
    return out


# ---------------------------------------------------------------- GPU samplers

def process_tree(pid: Optional[int]) -> set:
    """The pid and its descendants: vLLM's engine core and SGLang's scheduler, which hold the GPU, are children of
    the server process (psutil; without it, the pid alone)."""
    if not pid:
        return set()
    pids = {pid}
    try:
        import psutil  # pylint: disable=import-outside-toplevel

        pids.update(p.pid for p in psutil.Process(pid).children(recursive=True))
    except Exception:  # noqa: BLE001 - psutil missing, or the process is gone
        pass
    return pids


class Sampler:
    """Collects one (vram_gb, util_pct, power_w, shared_gb) per GPU per tick; a subclass fills ``gpus`` and
    ``series``. shared_gb is system memory the GPU has mapped (GTT on Linux, "Shared Usage" on Windows): on an
    APU such as Strix Halo the model's weights sit there, not in the small VRAM carve-out."""

    vendor = ""

    def __init__(self, server_pid: Optional[int] = None, interval_s: float = INTERVAL_S):
        self.server_pid = server_pid
        self.server_pids = process_tree(server_pid)
        self.interval_s = interval_s
        self.gpus: Dict[str, str] = {}  # id -> name, in order
        self.series: Dict[str, List[Tuple[Optional[float], ...]]] = {}
        self.in_use: List[str] = []  # GPUs the model server uses, when that could be told
        self.reasons: Dict[str, str] = {}  # field -> why it is null for every GPU
        self.error: Optional[str] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    def add(self, gpu: str, vram_gb: Optional[float], util: Optional[float], power: Optional[float],
            shared_gb: Optional[float] = None) -> None:
        with self._lock:
            self.series.setdefault(gpu, []).append((vram_gb, util, power, shared_gb))

    def start(self) -> None:
        self._thread = threading.Thread(target=self._guarded, name="telemetry-gpu", daemon=True)
        self._thread.start()

    def _guarded(self) -> None:
        try:
            self.loop()
        except Exception as e:  # noqa: BLE001 - a broken sampler records why; it never stops the run
            self.error = f"GPU sampling failed: {type(e).__name__}: {e}"

    def loop(self) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)


class AmdSysfs(Sampler):
    """AMD GPUs on Linux, read from /sys/class/drm/card*/device."""

    vendor = "amd"

    def __init__(self, cards: List[Dict[str, Any]], server_pid: Optional[int] = None,
                 interval_s: float = INTERVAL_S, proc_root: str = "/proc", kfd_root: str = "/sys/class/kfd/kfd"):
        super().__init__(server_pid, interval_s)
        self.cards = cards
        self.gpus = {c["id"]: c["name"] for c in cards}
        if not any(c["power"] for c in cards):
            self.reasons["power"] = "no hwmon power1_average or power1_input"
        if server_pid:
            used = set().union(*(self.used_pdevs(pid, proc_root) | self.kfd_pdevs(pid, kfd_root)
                                 for pid in self.server_pids))
            self.in_use = [c["id"] for c in cards if c["id"] in used]

    @staticmethod
    def find(drm_root: str = "/sys/class/drm") -> List[Dict[str, Any]]:
        cards = []
        for card in sorted(glob.glob(os.path.join(drm_root, "card*"))):
            dev = os.path.join(card, "device")
            if not re.fullmatch(r"card\d+", os.path.basename(card)) or _read(os.path.join(dev, "vendor")) != "0x1002":
                continue
            if not os.path.exists(os.path.join(dev, "mem_info_vram_used")):
                continue
            uevent = _read(os.path.join(dev, "uevent")) or ""
            slot = re.search(r"^PCI_SLOT_NAME=(\S+)", uevent, re.M)
            pci = slot.group(1) if slot else os.path.basename(card)
            hwmon = sorted(glob.glob(os.path.join(dev, "hwmon", "hwmon*")))
            power = next((p for h in hwmon for p in (os.path.join(h, "power1_average"), os.path.join(h, "power1_input"))
                          if os.path.exists(p)), None)
            name = _read(os.path.join(dev, "product_name")) or _lspci_name(pci) or \
                f"AMD GPU {_read(os.path.join(dev, 'device')) or ''} ({pci})".replace("  ", " ")
            cards.append({"id": pci, "name": name, "dev": dev, "power": power})
        return cards

    @staticmethod
    def used_pdevs(pid: int, proc_root: str = "/proc") -> set:
        """PCI addresses of the GPUs a process has open (amdgpu's drm-pdev in /proc/<pid>/fdinfo)."""
        found = set()
        for f in glob.glob(os.path.join(proc_root, str(pid), "fdinfo", "*")):
            for line in (_read(f) or "").splitlines():
                if line.startswith("drm-pdev:"):
                    found.add(line.split(":", 1)[1].strip())
        return found

    @staticmethod
    def kfd_pdevs(pid: int, kfd_root: str = "/sys/class/kfd/kfd") -> set:
        """PCI addresses of the GPUs a ROCm process uses, from /sys/class/kfd/kfd/proc/<pid>/vram_<gpu_id>. Anyone
        can read it, unlike another user's fdinfo: a server in a Docker container runs as root."""
        ids = {os.path.basename(f)[5:] for f in glob.glob(os.path.join(kfd_root, "proc", str(pid), "vram_*"))}
        found = set()
        for node in glob.glob(os.path.join(kfd_root, "topology", "nodes", "*")):
            if (_read(os.path.join(node, "gpu_id")) or "") not in ids:
                continue
            props = dict(line.split(" ", 1) for line in (_read(os.path.join(node, "properties")) or "").splitlines()
                         if " " in line)
            loc, dom = _num(props.get("location_id")), _num(props.get("domain"))
            if loc is not None and dom is not None:  # location_id is bus << 8 | device << 3 | function
                loc = int(loc)
                found.add(f"{int(dom):04x}:{loc >> 8:02x}:{(loc >> 3) & 0x1f:02x}.{loc & 7}")
        return found

    def sample(self) -> None:
        for c in self.cards:
            vram = _num(_read(os.path.join(c["dev"], "mem_info_vram_used")))
            gtt = _num(_read(os.path.join(c["dev"], "mem_info_gtt_used")))
            power = _num(_read(c["power"])) if c["power"] else None
            self.add(c["id"], None if vram is None else vram / GIB,
                     _num(_read(os.path.join(c["dev"], "gpu_busy_percent"))),
                     None if power is None else power / 1e6,  # microwatts
                     None if gtt is None else gtt / GIB)

    def loop(self) -> None:
        self.sample()
        while not self._stop.wait(self.interval_s):
            self.sample()

    def stop(self) -> None:
        super().stop()
        if not self.series or not any(self.series.values()):
            self.sample()


def _lspci_name(pci: str) -> Optional[str]:
    if not shutil.which("lspci"):
        return None
    line = _run(["lspci", "-s", pci]).strip()
    name = line.split(": ", 1)[1] if ": " in line else ""
    bracket = re.findall(r"\[([^\]]+)\]", name)
    return (bracket[-1] if bracket else name) or None


class Streaming(Sampler):
    """A sampler fed by one long-running command that prints a line per tick; stop() ends that process."""

    def __init__(self, server_pid: Optional[int] = None, interval_s: float = INTERVAL_S):
        super().__init__(server_pid, interval_s)
        self.proc: Optional[subprocess.Popen] = None

    def command(self) -> List[str]:
        raise NotImplementedError

    def line(self, text: str) -> None:
        raise NotImplementedError

    def loop(self) -> None:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.proc = subprocess.Popen(self.command(), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
                                     creationflags=flags)
        try:
            if self._stop.is_set():
                return  # stop() ran before the child existed: it had nothing to end, so this does
            for text in self.proc.stdout:
                if self._stop.is_set():
                    break
                if text.strip():
                    self.line(text)
        finally:
            self._end()

    def _end(self) -> None:
        proc = self.proc
        if proc and proc.poll() is None:
            proc.terminate()  # our own child, by its handle
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    def stop(self) -> None:
        self._stop.set()
        self._end()
        super().stop()


class NvidiaSmi(Streaming):
    vendor = "nvidia"

    def __init__(self, server_pid: Optional[int] = None, interval_s: float = INTERVAL_S, exe: Sequence[str] = ("nvidia-smi",)):
        super().__init__(server_pid, interval_s)
        self.exe = list(exe)
        uuids = {}
        for row in csv.reader(_run(self.exe + ["--query-gpu=index,uuid,name", "--format=csv,noheader"]).splitlines()):
            if len(row) >= 3:
                idx, uuid, name = (x.strip() for x in row[:3])
                self.gpus[idx] = name
                uuids[uuid] = idx
        if server_pid:
            apps = _run(self.exe + ["--query-compute-apps=pid,gpu_uuid", "--format=csv,noheader"])
            used = {uuids.get(r[1].strip()) for r in csv.reader(apps.splitlines())
                    if len(r) >= 2 and r[0].strip().isdigit() and int(r[0]) in self.server_pids}
            self.in_use = [i for i in self.gpus if i in used]

    def command(self) -> List[str]:
        return self.exe + ["--query-gpu=index,memory.used,utilization.gpu,power.draw",
                           "--format=csv,noheader,nounits", "-lms", str(int(self.interval_s * 1000))]

    def line(self, text: str) -> None:
        cols = [c.strip() for c in text.split(",")]
        if len(cols) < 4:
            return
        mib = _num(cols[1])
        self.add(cols[0], None if mib is None else mib * 2**20 / GIB, _num(cols[2]), _num(cols[3]))


class WindowsCounters(Streaming):
    """Windows GPU performance counters through typeperf: memory per adapter, utilization as Task Manager shows it
    (per engine summed over processes, the busiest engine)."""

    LUID = re.compile(r"luid_(0x[0-9a-f]+_0x[0-9a-f]+)", re.I)
    ENGINE = re.compile(r"pid_(\d+)_luid_(0x[0-9a-f]+_0x[0-9a-f]+)_phys_(\d+)_eng_(\d+)", re.I)

    def __init__(self, server_pid: Optional[int] = None, interval_s: float = INTERVAL_S, vendor: str = "amd",
                 names: Sequence[str] = (), exe: Sequence[str] = ("typeperf",)):
        super().__init__(server_pid, interval_s)
        self.vendor = vendor
        self.names = list(names)
        self.exe = list(exe)
        self.columns: List[Tuple[str, str, Any]] = []  # (kind, luid, engine key)
        self.reasons["power"] = "Windows has no GPU power counter"

    def command(self) -> List[str]:
        return self.exe + list(WIN_COUNTERS) + ["-si", str(max(1, int(round(self.interval_s))))]

    def header(self, row: List[str]) -> None:
        for path in row[1:]:
            low = path.lower()
            eng = self.ENGINE.search(path)
            luid = self.LUID.search(path)
            if not luid:
                self.columns.append(("", "", None))
            elif "\\dedicated usage" in low:
                self.columns.append(("dedicated", luid.group(1), None))
            elif "\\shared usage" in low:
                self.columns.append(("shared", luid.group(1), None))
            elif eng:
                pid, luid_, phys, engine = eng.groups()
                self.columns.append(("engine", luid_, (phys, engine)))
                if int(pid) in self.server_pids and luid_ not in self.in_use:
                    self.in_use.append(luid_)
            else:
                self.columns.append(("", "", None))
        luids = list(dict.fromkeys(c[1] for c in self.columns if c[0] == "dedicated"))
        if not luids:  # typeperf printed a header but none of the GPU counters asked for
            self.error = ("typeperf found none of the GPU memory counters: their names are localized on a "
                          "non-English Windows, and only the English ones are asked for")
        for luid in luids:
            self.gpus[luid] = luid
        named = self.in_use if self.in_use else luids
        if len(self.names) == 1 and len(named) == 1:
            self.gpus[named[0]] = self.names[0]

    def line(self, text: str) -> None:
        row = next(csv.reader([text]), [])
        if not row:
            return
        if row[0].startswith("(PDH-CSV"):
            self.header(row)
            return
        if not self.columns:
            return
        ded: Dict[str, float] = {}
        shared: Dict[str, float] = {}
        engines: Dict[Tuple[str, Any], float] = {}
        for (kind, luid, key), value in zip(self.columns, row[1:]):
            v = _num(value)
            if v is None or not kind:
                continue
            if kind == "dedicated":
                ded[luid] = v
            elif kind == "shared":
                shared[luid] = v
            else:
                engines[(luid, key)] = engines.get((luid, key), 0.0) + v
        for luid in self.gpus:
            busy = [v for (lu, _), v in engines.items() if lu == luid]
            self.add(luid, ded[luid] / GIB if luid in ded else None, min(100.0, max(busy)) if busy else None, None,
                     shared[luid] / GIB if luid in shared else None)


def windows_adapters() -> List[Tuple[str, str]]:
    """(name, maker) of each display adapter, from Win32_VideoController."""
    out = _run(["powershell", "-NoProfile", "-Command",
                "Get-CimInstance Win32_VideoController | ForEach-Object { $_.Name + '|' + $_.AdapterCompatibility }"])
    return [tuple(line.split("|", 1)) for line in out.splitlines() if "|" in line]  # type: ignore[misc]


def nvidia_smi_works(exe: Sequence[str] = ("nvidia-smi",)) -> bool:
    return bool(shutil.which(exe[0])) and bool(_run(list(exe) + ["--query-gpu=index", "--format=csv,noheader"]).strip())


def pick_sampler(server_pid: Optional[int], interval_s: float = INTERVAL_S) -> Tuple[Optional[Sampler], Optional[str]]:
    """The GPU sampler for this PC, or why there is none."""
    if sys.platform == "win32":
        nvidia = NvidiaSmi(server_pid, interval_s) if nvidia_smi_works() else None
        typeperf = shutil.which("typeperf")
        adapters = windows_adapters() if typeperf else []
        others = [n for n, maker in adapters  # not NVIDIA's, and not a remote or basic display adapter
                  if not any(v in maker.lower() for v in ("nvidia", "microsoft", "standard display types"))]
        # nvidia-smi sees only NVIDIA GPUs. On a box that also has a Radeon (an iGPU next to an NVIDIA dGPU), the
        # model server may run on the Radeon (Vulkan), and on WDDM nvidia-smi rarely lists the server's pid: then
        # the counters, which see every adapter, decide. nvidia-smi is kept when it sees the server, or is alone.
        if nvidia and (nvidia.in_use or not others or not typeperf):
            return nvidia, None
        if not typeperf:
            return None, "no nvidia-smi and no typeperf"
        amd = [n for n, maker in adapters if "advanced micro devices" in maker.lower() or "radeon" in n.lower()]
        vendor = "amd" if amd else "other"
        return WindowsCounters(server_pid, interval_s, vendor, amd or [n for n, _ in adapters]), None
    if sys.platform.startswith("linux"):
        cards = AmdSysfs.find()
        amd = AmdSysfs(cards, server_pid, interval_s) if cards else None
        if amd and amd.in_use:
            return amd, None
        if nvidia_smi_works():
            return NvidiaSmi(server_pid, interval_s), None
        if amd:
            return amd, None
        return None, "no AMD GPU in /sys/class/drm and no nvidia-smi"
    return None, f"GPU sampling is not supported on {sys.platform}"


def _r(x: Optional[float], nd: int = 1) -> Optional[float]:
    return None if x is None else round(x, nd)


def _stats(values: List[Optional[float]]) -> Tuple[Optional[float], Optional[float]]:
    """(peak, mean) over the samples that have a value; None when none has one (a gap is never read as 0)."""
    have = [v for v in values if v is not None]
    return (max(have), mean(have)) if have else (None, None)


def gpu_block(sampler: Optional[Sampler], reason: Optional[str]) -> Dict[str, Any]:
    out: Dict[str, Any] = {"vendor": sampler.vendor if sampler else None, "gpus": [], "gpu_scope": None,
                           "samples": 0, "vram_peak_gb": None, "vram_mean_gb": None, "shared_peak_gb": None,
                           "util_mean_pct": None, "power_mean_w": None, "per_gpu": [], "reason": reason}
    if sampler is None:
        return out
    with sampler._lock:  # pylint: disable=protected-access
        series = {g: list(s) for g, s in sampler.series.items()}
    why: List[str] = [sampler.error] if sampler.error else []
    used = [g for g in sampler.in_use if g in sampler.gpus]
    if used:
        scope = used
        out["gpu_scope"] = f"the GPUs the model server (pid {sampler.server_pid}) uses"
    else:
        scope = list(sampler.gpus)
        out["gpu_scope"] = "every GPU" + ("" if sampler.server_pid is None else
                                          ": couldn't tell which ones the model server uses")
    for g, name in sampler.gpus.items():
        s = series.get(g, [])
        vp, vm = _stats([x[0] for x in s])
        _, um = _stats([x[1] for x in s])
        _, pm = _stats([x[2] for x in s])
        sp, _ = _stats([x[3] for x in s])
        out["per_gpu"].append({"id": g, "name": name, "in_scope": g in scope, "samples": len(s),
                               "vram_peak_gb": _r(vp, 2), "vram_mean_gb": _r(vm, 2), "shared_peak_gb": _r(sp, 2),
                               "util_mean_pct": _r(um), "power_mean_w": _r(pm)})
    out["gpus"] = [sampler.gpus[g] for g in scope]
    n = min((len(series.get(g, [])) for g in scope), default=0)
    out["samples"] = n
    if n == 0:
        why.append("no GPU samples")
    else:
        rows = [[series[g][i] for g in scope] for i in range(n)]

        def total(k: int) -> List[Optional[float]]:
            return [None if any(x[k] is None for x in row) else sum(x[k] for x in row) for row in rows]  # type: ignore

        vp, vm = _stats(total(0))
        out["vram_peak_gb"], out["vram_mean_gb"] = _r(vp, 2), _r(vm, 2)
        out["shared_peak_gb"] = _r(_stats(total(3))[0], 2)  # None where there is no such counter (NVIDIA)
        util = [None if any(x[1] is None for x in row) else mean(x[1] for x in row) for row in rows]  # type: ignore
        out["util_mean_pct"] = _r(_stats(util)[1])
        out["power_mean_w"] = _r(_stats(total(2))[1])
        for field, key in (("vram_peak_gb", "vram"), ("util_mean_pct", "util"), ("power_mean_w", "power")):
            if out[field] is None:
                why.append(f"{key}: {sampler.reasons.get(key, 'not readable')}")
    out["reason"] = "; ".join(why) or None
    return out


# ---------------------------------------------------------------- GPU faults in the kernel or system log

FAULT_KINDS = ("resets", "page_faults", "timeouts", "other")
FAULT_LINES = 5  # sample lines kept per task
FAULT_LINE_CHARS = 200
FAULT_TIMEOUT_S = 60
#: amdgpu's headline line for each kind of event. A page fault's follow-up lines (the faulting process, address,
#: *_L2_PROTECTION_FAULT_STATUS, the UTCL2 client) are not counted: PROTECTION_FAULT lines count as page faults only
#: when no headline line is there. A reset counts its "GPU reset begin" line; other reset lines count only without one.
AMD_FAULT = (("resets", re.compile(r"GPU reset begin", re.I)),
             ("page_faults", re.compile(r"page fault|GPU fault detected", re.I)),
             ("timeouts", re.compile(r"ring \S+ timeout|MES failed to respond", re.I)),
             ("other", re.compile(r"device lost|HW exception|\bGPU hang\b|uncorrectable", re.I)))
AMD_RESET_OTHER = re.compile(r"\b(?:GPU|ASIC|BACO|mode ?[12]) reset\b(?!.*\b(?:succeeded|failed|end)\b)", re.I)
AMD_PROTECTION = re.compile(r"L2_PROTECTION_FAULT", re.I)
#: NVIDIA's driver reports each GPU error as an Xid; the number says what happened.
NV_XID = re.compile(r"NVRM: Xid \(([^)]*)\):\s*(\d+)")
NV_OTHER = re.compile(r"NVRM: .*(RmInitAdapter failed|GPU lost)", re.I)
XID_KIND = {8: "timeouts", 109: "timeouts", 119: "timeouts", 120: "timeouts",  # stopped processing, GSP timeouts
            31: "page_faults",  # MMU fault
            79: "resets", 154: "resets"}  # fallen off the bus, recovery action (reset) required
#: Windows: display-driver timeouts (Display 4101, a TDR) and errors and warnings from the GPU drivers.
WIN_FAULT_PS = r"""
$s = [DateTimeOffset]::FromUnixTimeMilliseconds(%d).LocalDateTime
$e = [DateTimeOffset]::FromUnixTimeMilliseconds(%d).LocalDateTime
try {
  Get-WinEvent -FilterHashtable @{LogName='System'; StartTime=$s; EndTime=$e} -ErrorAction Stop |
    Where-Object { ($_.Id -eq 4101 -and $_.ProviderName -eq 'Display') -or
                   ($_.ProviderName -match '^(amdkmdag|amdkmdap|amdwddmg|nvlddmkm)$' -and $_.Level -in 1,2,3) } |
    ForEach-Object { 'EVENT|{0:s}|{1}|{2}|{3}' -f $_.TimeCreated, $_.Id, $_.ProviderName,
                     ((($_.Message + '') -replace '\s+', ' ').Trim()) }
  'FAULTLOG_OK'
} catch {
  if ($_.FullyQualifiedErrorId -match 'NoMatchingEventsFound') { 'FAULTLOG_OK' } else { 'FAULTLOG_ERROR ' + $_ }
}
"""


def _run_rc(cmd: Sequence[str], timeout: float = FAULT_TIMEOUT_S) -> Tuple[Optional[int], str, str]:
    """(exit code, stdout, stderr); the exit code is None when the command can't run."""
    try:
        p = subprocess.run(list(cmd), capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=timeout, stdin=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return p.returncode, p.stdout, p.stderr
    except FileNotFoundError:
        return None, "", "not installed"
    except (OSError, subprocess.SubprocessError) as e:
        return None, "", f"{type(e).__name__}: {e}"


Hit = Tuple[str, Optional[int], str]  # (kind, Xid, the line)


def kernel_hits(lines: Sequence[str]) -> List[Hit]:
    """The fault lines among kernel-log lines. Kind "reset?" and "protection" are counted only as a fallback."""
    hits = []
    for text in lines:
        xid = NV_XID.search(text)
        if xid:
            hits.append((XID_KIND.get(int(xid.group(2)), "other"), int(xid.group(2)), text))
        elif NV_OTHER.search(text):
            hits.append(("other", None, text))
        elif "amdgpu" in text.lower():
            kind = next((k for k, rx in AMD_FAULT if rx.search(text)), None) or \
                ("reset?" if AMD_RESET_OTHER.search(text) else "protection" if AMD_PROTECTION.search(text) else None)
            if kind:
                hits.append((kind, None, text))
    return hits


def windows_hits(text: str) -> List[Hit]:
    """WIN_FAULT_PS's events: a TDR is a timeout, an nvlddmkm event is read by its Xid, an AMD driver event by its
    message. The message is matched in English: on a non-English Windows an AMD driver event counts as "other"."""
    hits: List[Hit] = []
    for row in text.splitlines():
        if not row.startswith("EVENT|"):
            continue
        _, when, eid, provider, message = (row.split("|", 4) + [""] * 4)[:5]
        line, low = f"{when} {provider} {eid}: {message}", message.lower()
        if provider == "Display":
            hits.append(("timeouts", None, line))
        elif provider == "nvlddmkm":
            xid = re.search(r"\bXid\b\D{0,20}?(\d+)", message)
            hits.append((XID_KIND.get(int(xid.group(1)), "other"), int(xid.group(1)), line) if xid
                        else ("other", None, line))
        else:
            hits.append(("resets" if "reset" in low or "recover" in low else
                         "page_faults" if "page fault" in low or "pagefault" in low else
                         "timeouts" if "timeout" in low or "not responding" in low else "other", None, line))
    return hits


def fault_block(hits: Sequence[Hit], source: Optional[str], reason: Optional[str] = None) -> Dict[str, Any]:
    """Each kind of GPU fault event counted, from the task window's fault lines. With a ``reason`` the log couldn't
    be read: the counts are None, never 0."""
    out: Dict[str, Any] = {k: None for k in FAULT_KINDS}
    out.update(xids=None, lines=[], source=source, reason=reason)
    if reason:
        return out
    n = {k: sum(1 for h in hits if h[0] == k) for k in FAULT_KINDS + ("reset?", "protection")}
    counts = {k: n[k] for k in FAULT_KINDS}
    counts["resets"] = n["resets"] or n["reset?"]
    counts["page_faults"] = n["page_faults"] or n["protection"]
    out.update(counts, xids=[h[1] for h in hits if h[1] is not None],
               lines=[h[2].strip()[:FAULT_LINE_CHARS] for h in hits[:FAULT_LINES]])
    return out


def read_faults(start: float, end: float) -> Dict[str, Any]:
    """GPU fault events logged between two time.time() readings: the kernel log on Linux (journalctl -k, else
    dmesg), the System event log on Windows. Read once, after the task; it reads logs only."""
    if sys.platform == "win32":
        script = WIN_FAULT_PS % (int(start * 1000), int(end * 1000) + 1000)
        rc, out, err = _run_rc(["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand",
                                base64.b64encode(script.encode("utf-16-le")).decode()])
        if "FAULTLOG_OK" not in out.splitlines():
            detail = next((x[15:] for x in out.splitlines() if x.startswith("FAULTLOG_ERROR ")), "") or \
                _last(err, rc)
            return fault_block([], None, f"not measured: the System event log could not be read: {detail[:200]}")
        return fault_block(windows_hits(out), "windows-system-log")
    if not sys.platform.startswith("linux"):
        return fault_block([], None, f"not measured: no GPU fault log reader for {sys.platform}")
    rc, out, err = _run_rc(["journalctl", "-k", "--no-pager", "-o", "short-iso",
                            "--since", f"@{int(start)}", "--until", f"@{int(end) + 1}"])
    denied = re.search(r"not seeing messages from other users|insufficient permissions|No journal files", err, re.I)
    if rc == 0 and not denied:
        return fault_block(kernel_hits([x for x in out.splitlines() if not x.startswith("-- ")]), "journalctl -k")
    why = ["journalctl -k: " + ("no access to the system journal (needs the adm or systemd-journal group)"
                                if denied else _last(err, rc))]
    rc, out, err = _run_rc(["dmesg", "--time-format", "iso"])
    if rc != 0:
        return fault_block([], None, f"not measured: {why[0]}; dmesg: {_last(err, rc)}")
    lines, dated = [], 0
    for x in out.splitlines():
        stamp = DMESG_ISO.match(x)
        if stamp:
            dated += 1
            when = datetime.fromisoformat(f"{stamp.group(1)}.{(stamp.group(2) or '0').ljust(6, '0')[:6]}"
                                          f"{stamp.group(3)}:{stamp.group(4)}").timestamp()
            if start - 1 <= when <= end + 1:
                lines.append(x)
    if out.strip() and not dated:
        return fault_block([], None, f"not measured: {why[0]}; dmesg printed no ISO timestamps")
    return fault_block(kernel_hits(lines), "dmesg")


#: dmesg --time-format iso: 2026-11-10T14:03:21,123456+00:00
DMESG_ISO = re.compile(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:[,.](\d+))?([+-]\d\d):?(\d\d)\s")


def _last(err: str, rc: Optional[int]) -> str:
    """The last line of a command's stderr (without the "dmesg: " it starts with), or its exit code."""
    lines = err.strip().splitlines()
    return re.sub(r"^(?:dmesg|journalctl): ", "", lines[-1])[:160] if lines else f"exit code {rc}"


# ---------------------------------------------------------------- context window (#72)
def _json(port: int, path: str) -> Any:
    text = engine_info.fetch(port, path)
    try:
        return json.loads(text) if text else None
    except ValueError:
        return None


def context_window(server: Optional[Dict[str, Any]]) -> Tuple[Optional[int], Optional[str]]:
    """The window one request gets, and why it is unknown. llama-server: a slot's n_ctx (/slots, else /props; with
    --parallel N and no unified KV cache a slot gets 1/N of -c); vLLM and SGLang: max_model_len (/v1/models)."""
    if not server or not server.get("port"):
        return None, "no model server found"
    port, kind = server["port"], server.get("kind")
    if kind == "llama-server":
        slots = _json(port, "/slots")
        if isinstance(slots, list) and slots and isinstance(slots[0], dict) and slots[0].get("n_ctx"):
            return int(slots[0]["n_ctx"]), None
        props = _json(port, "/props")
        n = ((props or {}).get("default_generation_settings") or {}).get("n_ctx") if isinstance(props, dict) else None
        return (int(n), None) if n else (None, "llama-server answered neither /slots nor /props with n_ctx")
    if kind in ("vllm", "sglang"):
        models = _json(port, "/v1/models")
        data = models.get("data") if isinstance(models, dict) else None
        n = data[0].get("max_model_len") if isinstance(data, list) and data and isinstance(data[0], dict) else None
        return (int(n), None) if n else (None, f"{kind} /v1/models has no max_model_len")
    return None, f"{kind}: its context window isn't read"


class SlotWatcher:
    """Polls llama-server's /slots while the agent works: the largest prompt any request sent. A slot shows its
    request's n_prompt_tokens while it runs and keeps it until the slot's next request, so a 1 s poll misses a
    request only if its slot is reused within the second. What the slots held before the task isn't counted."""

    def __init__(self, port: int, interval_s: float = INTERVAL_S):
        self.port, self.interval_s = port, interval_s
        self.before: Dict[Any, Any] = {}
        self.seen: Dict[Tuple[Any, Any], int] = {}  # (slot, task) -> prompt tokens
        self.error: Optional[str] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def slots(self) -> Optional[List[Dict[str, Any]]]:
        s = _json(self.port, "/slots")
        return [x for x in s if isinstance(x, dict)] if isinstance(s, list) else None

    def start(self) -> "SlotWatcher":
        first = self.slots()
        if first is None:
            self.error = "llama-server doesn't answer /slots (started with --no-slots?)"
            return self
        self.before = {x.get("id"): x.get("id_task") for x in first}
        self._thread = threading.Thread(target=self._loop, name="telemetry-slots", daemon=True)
        self._thread.start()
        return self

    def poll(self) -> None:
        for x in self.slots() or []:
            n = x.get("n_prompt_tokens")
            if not isinstance(n, int) or n <= 0 or x.get("id_task") == self.before.get(x.get("id")):
                continue
            key = (x.get("id"), x.get("id_task"))
            self.seen[key] = max(n, self.seen.get(key, 0))

    def _loop(self) -> None:
        try:
            while not self._stop.wait(self.interval_s):
                self.poll()
        except Exception as e:  # noqa: BLE001
            self.error = f"/slots polling failed: {type(e).__name__}: {e}"

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)
            self.poll()  # the last request, which may have ended since the last tick


def context_block(server: Optional[Dict[str, Any]], window: Optional[int], window_reason: Optional[str],
                  watcher: Optional[SlotWatcher]) -> Dict[str, Any]:
    peak = max(watcher.seen.values()) if watcher and watcher.seen else None
    reasons = [r for r in (window_reason, watcher.error if watcher else None) if r]
    if server and server.get("kind") in ("vllm", "sglang"):
        reasons.append(f"{server['kind']} doesn't report each request's prompt size")
    return {"window": window, "peak_prompt_tokens": peak, "requests": len(watcher.seen) if watcher else None,
            "reason": "; ".join(reasons) or None}


# ---------------------------------------------------------------- one task

class Telemetry:
    """start() before the agent runs, stop() after it (in a finally); neither raises."""

    def __init__(self, interval_s: float = INTERVAL_S):
        self.interval_s = interval_s
        self.server: Optional[Dict[str, Any]] = None
        self.before: Optional[Dict[str, float]] = None
        self.server_reason: Optional[str] = None
        self.sampler: Optional[Sampler] = None
        self.gpu_reason: Optional[str] = None
        self.started = 0.0
        self.wall_start = 0.0
        self.block: Optional[Dict[str, Any]] = None
        self.window: Optional[int] = None
        self.window_reason: Optional[str] = None
        self.slots: Optional[SlotWatcher] = None

    def start(self) -> "Telemetry":
        self.started, self.wall_start = time.monotonic(), time.time()
        try:
            self.server, self.before, self.server_reason = find_server()
        except Exception as e:  # noqa: BLE001
            self.server_reason = f"the server could not be read: {type(e).__name__}: {e}"
        try:
            self.window, self.window_reason = context_window(self.server)
            if self.server and self.server.get("kind") == "llama-server" and self.server.get("port"):
                self.slots = SlotWatcher(self.server["port"], self.interval_s).start()
        except Exception as e:  # noqa: BLE001
            self.window_reason = f"the context window could not be read: {type(e).__name__}: {e}"
        try:
            self.sampler, self.gpu_reason = pick_sampler((self.server or {}).get("pid"), self.interval_s)
            if self.sampler:
                self.sampler.start()
        except Exception as e:  # noqa: BLE001
            self.sampler, self.gpu_reason = None, f"GPU sampling could not start: {type(e).__name__}: {e}"
        return self

    def stop(self) -> Dict[str, Any]:
        """Stop sampling and return the task's telemetry block; a second call returns the same block."""
        if self.block is not None:
            return self.block
        try:
            if self.sampler:
                self.sampler.stop()
        except Exception as e:  # noqa: BLE001
            self.gpu_reason = f"GPU sampling did not stop cleanly: {type(e).__name__}: {e}"
        try:
            if self.slots:
                self.slots.stop()
            if self.server is None:  # a server that loads its model on the first request (Lemonade) starts later
                late, _, _ = find_server()
                if late:
                    self.window, self.window_reason = context_window(late)
            context = context_block(self.server, self.window, self.window_reason, self.slots)
        except Exception as e:  # noqa: BLE001
            context = context_block(None, self.window, f"/slots polling did not stop cleanly: {type(e).__name__}: {e}",
                                    None)
        try:
            after = read_counters(self.server["port"], self.server["kind"]) \
                if self.server and self.before is not None else None
            server = server_block(self.server, self.before, after, self.server_reason)
        except Exception as e:  # noqa: BLE001
            server = server_block(None, None, None, f"the server could not be read: {type(e).__name__}: {e}")
        try:
            gpu = gpu_block(self.sampler, self.gpu_reason)
        except Exception as e:  # noqa: BLE001
            gpu = gpu_block(None, f"GPU samples could not be summarized: {type(e).__name__}: {e}")
        seconds = round(time.monotonic() - self.started, 1)
        try:
            faults = read_faults(self.wall_start, time.time())
        except Exception as e:  # noqa: BLE001
            faults = fault_block([], None, f"not measured: the log could not be read: {type(e).__name__}: {e}")
        self.block = {"interval_s": self.interval_s, "seconds": seconds, "server": server, "gpu": gpu,
                      "faults": faults, "context": context}
        return self.block


def line(block: Dict[str, Any]) -> str:
    """``telemetry: prefill 912 tok/s, decode 24.3 tok/s, peak VRAM 22.4 GB, util 71%, 210 W``."""
    s, g = block.get("server") or {}, block.get("gpu") or {}

    def val(x: Optional[float], fmt: str) -> str:
        return "n/a" if x is None else fmt.format(x)

    bits = [f"prefill {val(s.get('prefill_tok_s'), '{:.0f}')} tok/s", f"decode {val(s.get('decode_tok_s'), '{:.1f}')} tok/s",
            f"peak VRAM {val(g.get('vram_peak_gb'), '{:.1f}')} GB"]
    if g.get("shared_peak_gb") is not None:  # an APU's model sits in shared memory: VRAM alone would understate it
        bits.append(f"peak shared {g['shared_peak_gb']:.1f} GB")
    bits.append(f"util {val(g.get('util_mean_pct'), '{:.0f}')}%")
    if g.get("power_mean_w") is not None:
        bits.append(f"{g['power_mean_w']:.0f} W")
    f = block.get("faults") or {}
    if f.get("reason"):
        bits.append("GPU faults n/a")
    elif any(f.get(k) for k in FAULT_KINDS):
        names = {k: k.replace("_", " ") for k in FAULT_KINDS}
        bits.append("GPU faults: " + ", ".join(f"{f[k]} {names[k].rstrip('s') if f[k] == 1 else names[k]}"
                                               for k in FAULT_KINDS if f.get(k)))
    notes = [r for r in (s.get("reason"), g.get("reason"), f.get("reason")) if r]
    return "telemetry: " + ", ".join(bits) + (f" ({'; '.join(notes)})" if notes else "")


if __name__ == "__main__":
    tel = Telemetry().start()
    time.sleep(float(sys.argv[1]) if len(sys.argv) > 1 else 5)
    result = tel.stop()
    print(json.dumps(result, indent=1))
    print(line(result))
