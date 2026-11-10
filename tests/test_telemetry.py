"""run_eval.py --telemetry: server counters and GPU sampling, against fake /metrics, sysfs, nvidia-smi and typeperf."""

import sys
import time

import pytest

import telemetry

METRICS = """# HELP llamacpp:prompt_tokens_total Number of prompt tokens processed.
# TYPE llamacpp:prompt_tokens_total counter
llamacpp:prompt_tokens_total {pt}
llamacpp:prompt_seconds_total {ps}
llamacpp:tokens_predicted_total {ot}
llamacpp:tokens_predicted_seconds_total {os_}
llamacpp:requests_processing 0
"""
SERVER = {"pid": 4321, "kind": "llama-server", "name": "llama-server", "model": "/m/Qwen3.8-27B.gguf",
          "port": 8080, "path": "/opt/llama/llama-server"}


def fake_metrics(monkeypatch, texts):
    """engine_info.fetch answers /metrics with texts[port] (a list is consumed one reading at a time)."""
    def fetch(port, path):
        assert path == "/metrics"
        t = texts.get(port)
        return t.pop(0) if isinstance(t, list) else t
    monkeypatch.setattr(telemetry.engine_info, "fetch", fetch)


def test_counter_deltas_give_tokens_and_speeds(monkeypatch):
    fake_metrics(monkeypatch, {8080: [METRICS.format(pt=1000, ps=2.0, ot=50, os_=2.0),
                                      METRICS.format(pt=10120, ps=12.0, ot=1022, os_=42.0)]})
    server, before, why = telemetry.find_server([SERVER])
    assert server["pid"] == 4321 and why is None
    block = telemetry.server_block(server, before, telemetry.read_counters(8080), None)
    assert block == {"kind": "llama-server", "port": 8080, "model": "Qwen3.8-27B.gguf", "tokens_in": 9120,
                     "tokens_out": 972, "prefill_tok_s": 912.0, "decode_tok_s": 24.3, "reason": None}


def test_no_metrics_or_no_server_is_null_with_a_reason_never_zero(monkeypatch):
    fake_metrics(monkeypatch, {8080: None})
    server, before, why = telemetry.find_server([SERVER])
    assert before is None and "--metrics" in why
    block = telemetry.server_block(server, before, None, why)
    assert block["tokens_in"] is None and block["decode_tok_s"] is None and "--metrics" in block["reason"]
    _, _, why = telemetry.find_server([{**SERVER, "kind": "ollama", "port": 11434}])
    assert why.startswith("no llama-server, vLLM or SGLang running") and "ollama" in why
    assert telemetry.find_server([])[2] == ("no llama-server, vLLM or SGLang running; "
                                            "nothing answers /metrics on ports 8000 or 30000")


def test_two_answering_servers_are_not_guessed_between(monkeypatch):
    text = METRICS.format(pt=1, ps=1, ot=1, os_=1)
    fake_metrics(monkeypatch, {8080: text, 8081: text})
    server, _, why = telemetry.find_server([SERVER, {**SERVER, "pid": 5, "port": 8081}])
    assert server is None and "several" in why
    fake_metrics(monkeypatch, {8080: None, 8081: text})  # only one answers: that one
    assert telemetry.find_server([SERVER, {**SERVER, "pid": 5, "port": 8081}])[0]["port"] == 8081


def test_idle_task_and_restarted_server():
    a = {n: 10.0 for n in telemetry._names("llama-server")}
    idle = telemetry.server_block(SERVER, a, dict(a), None)
    assert idle["tokens_out"] == 0 and idle["decode_tok_s"] is None and "no tokens generated" in idle["reason"]
    restarted = telemetry.server_block(SERVER, a, {n: 1.0 for n in a}, None)
    assert restarted["tokens_in"] is None and "restarted" in restarted["reason"]
    gone = telemetry.server_block(SERVER, a, None, None)
    assert gone["tokens_in"] is None and "stopped answering" in gone["reason"]


# /metrics captured from live servers serving Qwen3-0.6B, trimmed to the counters SOURCES reads, before and after six
# streamed chat requests sharing a 2.5k-token system prompt (so five of them hit the prefix cache).
# vLLM 0.29.0, rocm/vllm container, Radeon AI PRO R9700:
VLLM_BEFORE = """# HELP vllm:prompt_tokens_total Number of prefill tokens processed.
# TYPE vllm:prompt_tokens_total counter
vllm:prompt_tokens_total{engine="0",model_name="qwen3-0.6b"} 156.0
# HELP vllm:prompt_tokens_cached_total Number of cached prompt tokens (local + external).
# TYPE vllm:prompt_tokens_cached_total counter
vllm:prompt_tokens_cached_total{engine="0",model_name="qwen3-0.6b"} 0.0
# HELP vllm:generation_tokens_total Number of generation tokens processed.
# TYPE vllm:generation_tokens_total counter
vllm:generation_tokens_total{engine="0",model_name="qwen3-0.6b"} 112.0
vllm:request_prefill_time_seconds_count{engine="0",model_name="qwen3-0.6b"} 1.0
vllm:request_prefill_time_seconds_sum{engine="0",model_name="qwen3-0.6b"} 0.017304164997767657
vllm:request_decode_time_seconds_count{engine="0",model_name="qwen3-0.6b"} 1.0
vllm:request_decode_time_seconds_sum{engine="0",model_name="qwen3-0.6b"} 0.7035562590463087
"""
VLLM_AFTER = """# HELP vllm:prompt_tokens_total Number of prefill tokens processed.
# TYPE vllm:prompt_tokens_total counter
vllm:prompt_tokens_total{engine="0",model_name="qwen3-0.6b"} 15222.0
# HELP vllm:prompt_tokens_cached_total Number of cached prompt tokens (local + external).
# TYPE vllm:prompt_tokens_cached_total counter
vllm:prompt_tokens_cached_total{engine="0",model_name="qwen3-0.6b"} 12480.0
# HELP vllm:generation_tokens_total Number of generation tokens processed.
# TYPE vllm:generation_tokens_total counter
vllm:generation_tokens_total{engine="0",model_name="qwen3-0.6b"} 1554.0
vllm:request_prefill_time_seconds_count{engine="0",model_name="qwen3-0.6b"} 7.0
vllm:request_prefill_time_seconds_sum{engine="0",model_name="qwen3-0.6b"} 0.21065197995631024
vllm:request_decode_time_seconds_count{engine="0",model_name="qwen3-0.6b"} 7.0
vllm:request_decode_time_seconds_sum{engine="0",model_name="qwen3-0.6b"} 14.646517580084037
"""
# SGLang 0.5.23, lmsysorg/sglang container, RTX 4080 SUPER. cached_tokens_total appears on the first cache hit.
SGLANG_BEFORE = """# HELP sglang:prompt_tokens_total Number of prefill tokens processed.
# TYPE sglang:prompt_tokens_total counter
sglang:prompt_tokens_total{engine_type="unified",is_streaming="false",model_name="qwen3-0.6b"} 167.0
# HELP sglang:generation_tokens_total Number of generation tokens processed.
# TYPE sglang:generation_tokens_total counter
sglang:generation_tokens_total{engine_type="unified",is_streaming="false",model_name="qwen3-0.6b"} 115.0
sglang:time_to_first_token_seconds_sum{engine_type="unified",is_streaming="false",model_name="qwen3-0.6b"} 5.810334406647598
sglang:time_to_first_token_seconds_count{engine_type="unified",is_streaming="false",model_name="qwen3-0.6b"} 2.0
sglang:e2e_request_latency_seconds_sum{engine_type="unified",is_streaming="false",model_name="qwen3-0.6b"} 6.365259524987778
sglang:e2e_request_latency_seconds_count{engine_type="unified",is_streaming="false",model_name="qwen3-0.6b"} 2.0
"""
SGLANG_AFTER = """# HELP sglang:prompt_tokens_total Number of prefill tokens processed.
# TYPE sglang:prompt_tokens_total counter
sglang:prompt_tokens_total{engine_type="unified",is_streaming="false",model_name="qwen3-0.6b"} 167.0
sglang:prompt_tokens_total{engine_type="unified",is_streaming="true",model_name="qwen3-0.6b"} 15066.0
# HELP sglang:generation_tokens_total Number of generation tokens processed.
# TYPE sglang:generation_tokens_total counter
sglang:generation_tokens_total{engine_type="unified",is_streaming="false",model_name="qwen3-0.6b"} 115.0
sglang:generation_tokens_total{engine_type="unified",is_streaming="true",model_name="qwen3-0.6b"} 1445.0
# HELP sglang:cached_tokens_total Number of cached prompt tokens by source (device/host/storage).
# TYPE sglang:cached_tokens_total counter
sglang:cached_tokens_total{cache_source="device",engine_type="unified",model_name="qwen3-0.6b"} 12503.0
sglang:time_to_first_token_seconds_sum{engine_type="unified",is_streaming="false",model_name="qwen3-0.6b"} 5.810334406647598
sglang:time_to_first_token_seconds_count{engine_type="unified",is_streaming="false",model_name="qwen3-0.6b"} 2.0
sglang:time_to_first_token_seconds_sum{engine_type="unified",is_streaming="true",model_name="qwen3-0.6b"} 0.15304387001378927
sglang:time_to_first_token_seconds_count{engine_type="unified",is_streaming="true",model_name="qwen3-0.6b"} 6.0
sglang:e2e_request_latency_seconds_sum{engine_type="unified",is_streaming="false",model_name="qwen3-0.6b"} 6.365259524987778
sglang:e2e_request_latency_seconds_count{engine_type="unified",is_streaming="false",model_name="qwen3-0.6b"} 2.0
sglang:e2e_request_latency_seconds_sum{engine_type="unified",is_streaming="true",model_name="qwen3-0.6b"} 3.9637006180128083
sglang:e2e_request_latency_seconds_count{engine_type="unified",is_streaming="true",model_name="qwen3-0.6b"} 6.0
"""
# A server that has served nothing yet: its labelled counters have no samples.
SGLANG_EMPTY = "".join(x + "\n" for x in SGLANG_BEFORE.splitlines() if x.startswith("#"))
VLLM_PROC = {"pid": 117955, "kind": "vllm", "name": "vllm", "model": "Qwen/Qwen3-0.6B", "port": 8000,
             "path": "/usr/bin/python3"}
SGLANG_PROC = {"pid": 120917, "kind": "sglang", "name": "python3", "model": "Qwen/Qwen3-0.6B", "port": 30000,
               "path": "python3"}


def test_vllm_counts_computed_prompt_tokens_and_reads_its_phase_histograms(monkeypatch):
    fake_metrics(monkeypatch, {8000: [VLLM_BEFORE, VLLM_AFTER]})
    server, before, why = telemetry.find_server([VLLM_PROC])
    assert why is None
    block = telemetry.server_block(server, before, telemetry.read_counters(8000, "vllm"), None)
    # 15066 prompt tokens, 12480 of them prefix-cache hits; the same as the requests' usage and the server's
    # prompt_tokens_by_source{source="local_compute"}. The speeds match the client's: decode 103.5 tok/s.
    assert block == {"kind": "vllm", "port": 8000, "model": "Qwen3-0.6B", "tokens_in": 2586, "tokens_out": 1442,
                     "prefill_tok_s": 13374.9, "decode_tok_s": 103.4, "reason": None}


def test_sglang_sums_label_sets_and_a_cache_counter_not_there_yet_is_zero(monkeypatch):
    fake_metrics(monkeypatch, {30000: [SGLANG_BEFORE, SGLANG_AFTER]})
    server, before, why = telemetry.find_server([SGLANG_PROC])
    assert why is None and "sglang:cached_tokens_total" not in before
    block = telemetry.server_block(server, before, telemetry.read_counters(30000, "sglang"), None)
    # 15066 - 12503 cached; the server's own realtime_tokens_total{mode="prefill_compute"} rose by the same 2563.
    assert (block["kind"], block["tokens_in"], block["tokens_out"]) == ("sglang", 2563, 1445)
    assert (block["prefill_tok_s"], block["decode_tok_s"], block["reason"]) == (16746.8, 379.2, None)


def test_sglang_counters_created_during_the_task_start_at_zero(monkeypatch):
    fake_metrics(monkeypatch, {30000: [SGLANG_EMPTY, SGLANG_AFTER]})
    server, before, why = telemetry.find_server([SGLANG_PROC])
    assert why is None and before == {}
    block = telemetry.server_block(server, before, telemetry.read_counters(30000, "sglang"), None)
    assert (block["tokens_in"], block["tokens_out"], block["reason"]) == (15233 - 12503, 1560, None)


def test_sglang_without_enable_metrics_names_the_flag(monkeypatch):
    fake_metrics(monkeypatch, {30000: None})
    server, before, why = telemetry.find_server([SGLANG_PROC])
    assert server["kind"] == "sglang" and before is None
    assert why == "sglang port 30000: no /metrics: start it with --enable-metrics"
    block = telemetry.server_block(server, before, None, why)
    assert block["kind"] == "sglang" and block["tokens_in"] is None and "--enable-metrics" in block["reason"]


def test_a_missing_counter_is_null_with_its_name(monkeypatch):
    no_e2e = "".join(x + "\n" for x in SGLANG_AFTER.splitlines() if "e2e" not in x)
    fake_metrics(monkeypatch, {30000: [SGLANG_BEFORE, no_e2e]})
    server, before, _ = telemetry.find_server([SGLANG_PROC])
    block = telemetry.server_block(server, before, telemetry.read_counters(30000, "sglang"), None)
    assert block["tokens_out"] == 1445 and block["decode_tok_s"] is None
    assert "decode_s: the server has no sglang:e2e_request_latency_seconds_sum" in block["reason"]


def test_with_no_process_listed_the_default_ports_are_asked(monkeypatch):
    fake_metrics(monkeypatch, {8000: VLLM_AFTER, 30000: None})
    server, before, why = telemetry.find_server([])  # vLLM in Docker Desktop: no process to see
    assert why is None and (server["kind"], server["port"], server["pid"], server["model"]) == (
        "vllm", 8000, None, "qwen3-0.6b")
    assert before["vllm:prompt_tokens_total"] == 15222.0
    fake_metrics(monkeypatch, {8000: METRICS.format(pt=1, ps=1, ot=1, os_=1), 30000: None})  # not vLLM's
    assert telemetry.find_server([])[0] is None


def test_gpu_scope_includes_the_servers_child_processes(monkeypatch):
    import types

    class Proc:
        def __init__(self, pid):
            self.pid = pid

        def children(self, recursive):
            return [Proc(901), Proc(902)] if recursive else [Proc(901)]
    monkeypatch.setitem(sys.modules, "psutil", types.SimpleNamespace(Process=Proc))
    assert telemetry.process_tree(900) == {900, 901, 902}
    assert telemetry.process_tree(None) == set()
    monkeypatch.setitem(sys.modules, "psutil", None)
    assert telemetry.process_tree(900) == {900}


def make_card(root, n, pci, vram, busy, power_uw=None, vendor="0x1002"):
    dev = root / "drm" / f"card{n}" / "device"
    dev.mkdir(parents=True)
    (dev / "vendor").write_text(vendor + "\n")
    (dev / "uevent").write_text(f"DRIVER=amdgpu\nPCI_SLOT_NAME={pci}\n")
    (dev / "product_name").write_text(f"Radeon {n}\n")
    (dev / "mem_info_vram_used").write_text(str(vram))
    (dev / "gpu_busy_percent").write_text(str(busy))
    if power_uw is not None:
        (dev / "hwmon" / "hwmon3").mkdir(parents=True)
        (dev / "hwmon" / "hwmon3" / "power1_average").write_text(str(power_uw))
    (root / "drm" / f"card{n}-DP-1").mkdir()  # a connector, not a GPU
    return dev


def test_amd_sysfs_samples_the_gpu_the_server_has_open(tmp_path):
    make_card(tmp_path, 0, "0000:0c:00.0", 512 * 2**20, 0)  # an iGPU the server doesn't use
    dev = make_card(tmp_path, 1, "0000:03:00.0", 22 * 2**30, 70, power_uw=210_000_000)
    make_card(tmp_path, 2, "0000:05:00.0", 0, 0, vendor="0x10de")  # not AMD
    fdinfo = tmp_path / "proc" / "4321" / "fdinfo"
    fdinfo.mkdir(parents=True)
    (fdinfo / "7").write_text("pos:\t0\ndrm-driver:\tamdgpu\ndrm-pdev:\t0000:03:00.0\n")
    (fdinfo / "8").write_text("pos:\t0\n")
    cards = telemetry.AmdSysfs.find(str(tmp_path / "drm"))
    assert [c["id"] for c in cards] == ["0000:0c:00.0", "0000:03:00.0"]
    s = telemetry.AmdSysfs(cards, server_pid=4321, interval_s=0.05, proc_root=str(tmp_path / "proc"))
    assert s.in_use == ["0000:03:00.0"]
    s.start()
    time.sleep(0.12)
    (dev / "mem_info_vram_used").write_text(str(24 * 2**30))
    (dev / "gpu_busy_percent").write_text("90")
    time.sleep(0.12)
    s.stop()
    g = telemetry.gpu_block(s, None)
    assert g["vendor"] == "amd" and g["gpus"] == ["Radeon 1"] and "pid 4321" in g["gpu_scope"]
    assert g["samples"] >= 3 and g["vram_peak_gb"] == 24.0 and 22.0 < g["vram_mean_gb"] < 24.0
    assert 70 < g["util_mean_pct"] < 90 and g["power_mean_w"] == 210.0 and g["reason"] is None
    assert [p["in_scope"] for p in g["per_gpu"]] == [False, True]


def test_amd_gpu_of_a_root_process_comes_from_kfd(tmp_path):
    # vLLM in a ROCm container: its engine core (a child of the server) runs as root, so its fdinfo is unreadable;
    # KFD's per-process sysfs is not. Node and location_id as on the R9700 box (0xe300 = bus e3).
    make_card(tmp_path, 0, "0000:0c:00.0", 0, 0)
    make_card(tmp_path, 1, "0000:e3:00.0", 0, 0)
    kfd = tmp_path / "kfd"
    for n, gpu_id, loc in ((0, "0", 0), (1, "38502", 58112), (2, "4410", 3072)):
        (kfd / "topology" / "nodes" / str(n)).mkdir(parents=True)
        (kfd / "topology" / "nodes" / str(n) / "gpu_id").write_text(gpu_id + "\n")
        (kfd / "topology" / "nodes" / str(n) / "properties").write_text(f"cpu_cores_count 0\nlocation_id {loc}\n"
                                                                         "domain 0\n")
    (kfd / "proc" / "1545046").mkdir(parents=True)
    (kfd / "proc" / "1545046" / "vram_38502").write_text("28847173632\n")
    assert telemetry.AmdSysfs.kfd_pdevs(1545046, str(kfd)) == {"0000:e3:00.0"}
    assert telemetry.AmdSysfs.kfd_pdevs(1, str(kfd)) == set()
    s = telemetry.AmdSysfs(telemetry.AmdSysfs.find(str(tmp_path / "drm")), server_pid=1545046,
                           proc_root=str(tmp_path / "proc"), kfd_root=str(kfd))
    assert s.in_use == ["0000:e3:00.0"]


def test_amd_sysfs_without_power_says_why(tmp_path):
    make_card(tmp_path, 0, "0000:03:00.0", 2**30, 5)
    s = telemetry.AmdSysfs(telemetry.AmdSysfs.find(str(tmp_path / "drm")), server_pid=None, interval_s=0.05)
    s.start()
    s.stop()
    g = telemetry.gpu_block(s, None)
    assert g["samples"] >= 1 and g["vram_peak_gb"] == 1.0 and g["power_mean_w"] is None
    assert g["gpu_scope"] == "every GPU" and "power: no hwmon" in g["reason"]


FAKE_SMI = r'''
import sys, time
args = " ".join(sys.argv[1:])
if "--query-gpu=index,uuid,name" in args:
    print("0, GPU-aaa, NVIDIA GeForce RTX 4080 SUPER\n1, GPU-bbb, NVIDIA GeForce RTX 4080 SUPER\n2, GPU-ccc, NVIDIA GeForce RTX 4080 SUPER")
elif "--query-compute-apps" in args:
    print("4321, GPU-aaa\n4321, GPU-ccc\n999, GPU-bbb")
else:
    for i in range(1000):
        print(f"0, 10000, 80, 200.5\n1, 500, 3, [N/A]\n2, 9000, 60, 150.5", flush=True)
        time.sleep(0.05)
'''


def test_nvidia_smi_totals_cover_the_servers_gpus(tmp_path):
    script = tmp_path / "nvidia_smi.py"
    script.write_text(FAKE_SMI)
    s = telemetry.NvidiaSmi(server_pid=4321, interval_s=0.05, exe=[sys.executable, str(script)])
    assert s.in_use == ["0", "2"] and len(s.gpus) == 3
    s.start()
    time.sleep(0.6)
    s.stop()
    assert s.proc.poll() is not None  # the long-running nvidia-smi is gone
    g = telemetry.gpu_block(s, None)
    assert g["vendor"] == "nvidia" and g["samples"] >= 2 and len(g["gpus"]) == 2
    assert g["vram_peak_gb"] == round(19000 / 1024, 2) and g["util_mean_pct"] == 70.0 and g["power_mean_w"] == 351.0
    assert g["per_gpu"][1]["in_scope"] is False and g["per_gpu"][1]["power_mean_w"] is None


TYPEPERF = [
    '',
    '"(PDH-CSV 4.0)","\\\\PC\\GPU Adapter Memory(luid_0x00000000_0x00014CF0_phys_0)\\Dedicated Usage",'
    '"\\\\PC\\GPU Adapter Memory(luid_0x00000000_0x00014CF0_phys_0)\\Shared Usage",'
    '"\\\\PC\\GPU Adapter Memory(luid_0x00000000_0x0000AAAA_phys_0)\\Dedicated Usage",'
    '"\\\\PC\\GPU Engine(pid_4321_luid_0x00000000_0x00014CF0_phys_0_eng_0_engtype_3D)\\Utilization Percentage",'
    '"\\\\PC\\GPU Engine(pid_77_luid_0x00000000_0x00014CF0_phys_0_eng_0_engtype_3D)\\Utilization Percentage",'
    '"\\\\PC\\GPU Engine(pid_4321_luid_0x00000000_0x00014CF0_phys_0_eng_1_engtype_Compute_0)\\Utilization Percentage"',
    '"11/09/2026 15:42:01.548","17179869184.000000","1073741824.000000","0.000000","40.000000","5.000000","10.000000"',
    '"11/09/2026 15:42:02.548","19327352832.000000","2147483648.000000","0.000000","90.000000","20.000000","10.000000"',
    '"11/09/2026 15:42:03.548","","","","","",""',
]


def test_windows_counters_busiest_engine_and_server_adapter():
    s = telemetry.WindowsCounters(server_pid=4321, names=["AMD Radeon(TM) 8060S Graphics"])
    for row in TYPEPERF:
        if row:
            s.line(row)
    assert s.in_use == ["0x00000000_0x00014CF0"]
    g = telemetry.gpu_block(s, None)
    assert g["gpus"] == ["AMD Radeon(TM) 8060S Graphics"] and g["samples"] == 3
    assert g["vram_peak_gb"] == 18.0 and g["vram_mean_gb"] == 17.0  # the blank row is a gap, not 0
    assert g["util_mean_pct"] == 72.5  # engine 0: 45 then 100 (capped); engine 1: 10
    assert g["power_mean_w"] is None and "Windows has no GPU power counter" in g["reason"]
    assert g["per_gpu"][0]["shared_peak_gb"] == 2.0


def test_telemetry_never_raises_and_stops_once(monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("no processes")
    monkeypatch.setattr(telemetry.local_servers, "list_servers", boom)
    monkeypatch.setattr(telemetry, "pick_sampler", boom)
    tel = telemetry.Telemetry().start()
    block = tel.stop()
    assert block["server"]["tokens_in"] is None and "no processes" in block["server"]["reason"]
    assert block["gpu"]["vram_peak_gb"] is None and "no processes" in block["gpu"]["reason"]
    assert tel.stop() is block


def test_line():
    block = {"server": {"prefill_tok_s": 912.4, "decode_tok_s": 24.31, "reason": None},
             "gpu": {"vram_peak_gb": 22.43, "util_mean_pct": 71.2, "power_mean_w": None, "reason": None}}
    assert telemetry.line(block) == "telemetry: prefill 912 tok/s, decode 24.3 tok/s, peak VRAM 22.4 GB, util 71%"
    block["server"] = {"reason": "no llama-server running"}
    block["gpu"]["power_mean_w"] = 210.2
    assert telemetry.line(block) == ("telemetry: prefill n/a tok/s, decode n/a tok/s, peak VRAM 22.4 GB, util 71%, "
                                     "210 W (no llama-server running)")


def test_run_eval_has_the_flag_off_by_default():
    import run_eval
    import inspect
    assert inspect.signature(run_eval.run).parameters["measure"].default is False
    with pytest.raises(SystemExit):
        run_eval.main(["--help"])
