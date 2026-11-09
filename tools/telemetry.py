"""What one agent task cost the local model server and the GPU: run_eval.py --telemetry.

For each task, run_eval.py reads llama-server's /metrics before and after the agent runs (tokens in and out,
prefill and decode speed; llama-server must run with --metrics) and samples the GPU every second in a background
thread while the agent works:

    AMD, Linux        sysfs: mem_info_vram_used, gpu_busy_percent, hwmon power1_average
    NVIDIA            one long-running nvidia-smi --query-gpu ... -lms 1000 (Linux and Windows)
    AMD, Windows      the GPU performance counters, one long-running typeperf (no power counter exists)

The result is a ``telemetry`` block in the task's result.json and one printed line. Anything that can't be
measured is null, with the reason in ``reason``; never 0. Telemetry reads the model server and the GPU only, never
the agent's process, and never changes a score.

On a multi-GPU box the totals cover the GPUs the model server uses (found from its process: amdgpu fdinfo,
nvidia-smi's compute apps, or the server's GPU engine counters), else every GPU; ``gpu_scope`` says which, and
``per_gpu`` has each GPU on its own.

    python tools/telemetry.py [SECONDS]     # sample for SECONDS (default 5) and print the block
"""

from __future__ import annotations

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
from pathlib import Path
from statistics import mean
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent / "perplexity"))
import engine_info  # noqa: E402
import local_servers  # noqa: E402

INTERVAL_S = 1.0
GIB = 2**30
CMD_TIMEOUT_S = 15
PROMPT_TOKENS, PROMPT_SECONDS = "llamacpp:prompt_tokens_total", "llamacpp:prompt_seconds_total"
OUT_TOKENS, OUT_SECONDS = "llamacpp:tokens_predicted_total", "llamacpp:tokens_predicted_seconds_total"
COUNTERS = (PROMPT_TOKENS, PROMPT_SECONDS, OUT_TOKENS, OUT_SECONDS)
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

def read_counters(port: int) -> Optional[Dict[str, float]]:
    """llama-server's four token counters from /metrics, or None (no answer, or started without --metrics)."""
    text = engine_info.fetch(port, "/metrics")
    found = engine_info.metrics(text) if text else {}
    return {c: found[c] for c in COUNTERS} if all(c in found for c in COUNTERS) else None


def find_server(servers: Optional[List[Dict[str, Any]]] = None
                ) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, float]], Optional[str]]:
    """The llama-server to measure, its counters now, and why there is none."""
    servers = local_servers.list_servers() if servers is None else servers
    llama = [s for s in servers if s["kind"] == "llama-server" and s.get("port")]
    if not llama:
        others = sorted({s["kind"] for s in servers})
        return None, None, "no llama-server running" + (
            f" (found {', '.join(others)}; only llama-server's /metrics is read)" if others else "")
    answering = [(s, c) for s in llama for c in [read_counters(s["port"])] if c is not None]
    if len(answering) == 1:
        return answering[0][0], answering[0][1], None
    ports = ", ".join(str(s["port"]) for s in llama)
    if not answering:
        return llama[0] if len(llama) == 1 else None, None, (
            f"llama-server (port {ports}) has no /metrics: start it with --metrics")
    return None, None, f"several llama-servers answer /metrics (ports {ports}): can't tell which one the agent uses"


def server_block(server: Optional[Dict[str, Any]], before: Optional[Dict[str, float]],
                 after: Optional[Dict[str, float]], reason: Optional[str]) -> Dict[str, Any]:
    """Token counts and prefill/decode speed from two counter readings."""
    out: Dict[str, Any] = {"kind": "llama-server" if server else None, "port": (server or {}).get("port"),
                           "model": Path(str(server["model"]).replace("\\", "/")).name
                           if server and server.get("model") else None,
                           "tokens_in": None, "tokens_out": None, "prefill_tok_s": None, "decode_tok_s": None,
                           "reason": reason}
    if reason:
        return out
    if after is None:
        out["reason"] = "the server stopped answering /metrics during the task"
        return out
    d = {c: after[c] - before[c] for c in COUNTERS}
    if any(v < 0 for v in d.values()):
        out["reason"] = "the server's counters went down: it restarted during the task"
        return out
    out["tokens_in"], out["tokens_out"] = int(d[PROMPT_TOKENS]), int(d[OUT_TOKENS])
    why = []
    if d[PROMPT_SECONDS] > 0:
        out["prefill_tok_s"] = round(d[PROMPT_TOKENS] / d[PROMPT_SECONDS], 1)
    else:
        why.append("prefill: no prompt tokens processed")
    if d[OUT_SECONDS] > 0:
        out["decode_tok_s"] = round(d[OUT_TOKENS] / d[OUT_SECONDS], 1)
    else:
        why.append("decode: no tokens generated")
    out["reason"] = "; ".join(why) or None
    return out


# ---------------------------------------------------------------- GPU samplers

class Sampler:
    """Collects one (vram_gb, util_pct, power_w) per GPU per tick; a subclass fills ``gpus`` and ``series``."""

    vendor = ""

    def __init__(self, server_pid: Optional[int] = None, interval_s: float = INTERVAL_S):
        self.server_pid = server_pid
        self.interval_s = interval_s
        self.gpus: Dict[str, str] = {}  # id -> name, in order
        self.series: Dict[str, List[Tuple[Optional[float], Optional[float], Optional[float]]]] = {}
        self.extra: Dict[str, Dict[str, float]] = {}  # id -> extra peaks (Windows shared memory)
        self.in_use: List[str] = []  # GPUs the model server uses, when that could be told
        self.reasons: Dict[str, str] = {}  # field -> why it is null for every GPU
        self.error: Optional[str] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    def add(self, gpu: str, vram_gb: Optional[float], util: Optional[float], power: Optional[float]) -> None:
        with self._lock:
            self.series.setdefault(gpu, []).append((vram_gb, util, power))

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
                 interval_s: float = INTERVAL_S, proc_root: str = "/proc"):
        super().__init__(server_pid, interval_s)
        self.cards = cards
        self.gpus = {c["id"]: c["name"] for c in cards}
        if not any(c["power"] for c in cards):
            self.reasons["power"] = "no hwmon power1_average or power1_input"
        if server_pid:
            used = self.used_pdevs(server_pid, proc_root)
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

    def sample(self) -> None:
        for c in self.cards:
            vram = _num(_read(os.path.join(c["dev"], "mem_info_vram_used")))
            power = _num(_read(c["power"])) if c["power"] else None
            self.add(c["id"], None if vram is None else vram / GIB,
                     _num(_read(os.path.join(c["dev"], "gpu_busy_percent"))),
                     None if power is None else power / 1e6)  # microwatts

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
        for text in self.proc.stdout:
            if self._stop.is_set():
                break
            if text.strip():
                self.line(text)

    def stop(self) -> None:
        self._stop.set()
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()  # our own child, by its handle
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
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
                    if len(r) >= 2 and r[0].strip() == str(server_pid)}
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
                if self.server_pid and int(pid) == self.server_pid and luid_ not in self.in_use:
                    self.in_use.append(luid_)
            else:
                self.columns.append(("", "", None))
        luids = list(dict.fromkeys(c[1] for c in self.columns if c[0] == "dedicated"))
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
            self.add(luid, ded[luid] / GIB if luid in ded else None, min(100.0, max(busy)) if busy else None, None)
            if luid in shared:
                peak = self.extra.setdefault(luid, {})
                peak["shared_peak_gb"] = max(peak.get("shared_peak_gb", 0.0), round(shared[luid] / GIB, 2))


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
        if nvidia_smi_works():
            return NvidiaSmi(server_pid, interval_s), None
        if not shutil.which("typeperf"):
            return None, "no nvidia-smi and no typeperf"
        adapters = windows_adapters()
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
                           "samples": 0, "vram_peak_gb": None, "vram_mean_gb": None, "util_mean_pct": None,
                           "power_mean_w": None, "per_gpu": [], "reason": reason}
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
        out["per_gpu"].append({"id": g, "name": name, "in_scope": g in scope, "samples": len(s),
                               "vram_peak_gb": _r(vp, 2), "vram_mean_gb": _r(vm, 2), "util_mean_pct": _r(um),
                               "power_mean_w": _r(pm), **sampler.extra.get(g, {})})
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
        util = [None if any(x[1] is None for x in row) else mean(x[1] for x in row) for row in rows]  # type: ignore
        out["util_mean_pct"] = _r(_stats(util)[1])
        out["power_mean_w"] = _r(_stats(total(2))[1])
        for field, key in (("vram_peak_gb", "vram"), ("util_mean_pct", "util"), ("power_mean_w", "power")):
            if out[field] is None:
                why.append(f"{key}: {sampler.reasons.get(key, 'not readable')}")
    out["reason"] = "; ".join(why) or None
    return out


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
        self.block: Optional[Dict[str, Any]] = None

    def start(self) -> "Telemetry":
        self.started = time.monotonic()
        try:
            self.server, self.before, self.server_reason = find_server()
        except Exception as e:  # noqa: BLE001
            self.server_reason = f"the server could not be read: {type(e).__name__}: {e}"
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
            after = read_counters(self.server["port"]) if self.server and self.before is not None else None
            server = server_block(self.server, self.before, after, self.server_reason)
        except Exception as e:  # noqa: BLE001
            server = server_block(None, None, None, f"the server could not be read: {type(e).__name__}: {e}")
        try:
            gpu = gpu_block(self.sampler, self.gpu_reason)
        except Exception as e:  # noqa: BLE001
            gpu = gpu_block(None, f"GPU samples could not be summarized: {type(e).__name__}: {e}")
        self.block = {"interval_s": self.interval_s, "seconds": round(time.monotonic() - self.started, 1),
                      "server": server, "gpu": gpu}
        return self.block


def line(block: Dict[str, Any]) -> str:
    """``telemetry: prefill 912 tok/s, decode 24.3 tok/s, peak VRAM 22.4 GB, util 71%, 210 W``."""
    s, g = block.get("server") or {}, block.get("gpu") or {}

    def val(x: Optional[float], fmt: str) -> str:
        return "n/a" if x is None else fmt.format(x)

    bits = [f"prefill {val(s.get('prefill_tok_s'), '{:.0f}')} tok/s", f"decode {val(s.get('decode_tok_s'), '{:.1f}')} tok/s",
            f"peak VRAM {val(g.get('vram_peak_gb'), '{:.1f}')} GB", f"util {val(g.get('util_mean_pct'), '{:.0f}')}%"]
    if g.get("power_mean_w") is not None:
        bits.append(f"{g['power_mean_w']:.0f} W")
    notes = [r for r in (s.get("reason"), g.get("reason")) if r]
    return "telemetry: " + ", ".join(bits) + (f" ({'; '.join(notes)})" if notes else "")


if __name__ == "__main__":
    tel = Telemetry().start()
    time.sleep(float(sys.argv[1]) if len(sys.argv) > 1 else 5)
    result = tel.stop()
    print(json.dumps(result, indent=1))
    print(line(result))
