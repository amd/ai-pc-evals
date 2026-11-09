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
    assert why.startswith("no llama-server running") and "ollama" in why
    assert telemetry.find_server([])[2] == "no llama-server running"


def test_two_answering_servers_are_not_guessed_between(monkeypatch):
    text = METRICS.format(pt=1, ps=1, ot=1, os_=1)
    fake_metrics(monkeypatch, {8080: text, 8081: text})
    server, _, why = telemetry.find_server([SERVER, {**SERVER, "pid": 5, "port": 8081}])
    assert server is None and "several" in why
    fake_metrics(monkeypatch, {8080: None, 8081: text})  # only one answers: that one
    assert telemetry.find_server([SERVER, {**SERVER, "pid": 5, "port": 8081}])[0]["port"] == 8081


def test_idle_task_and_restarted_server():
    a = {c: 10.0 for c in telemetry.COUNTERS}
    idle = telemetry.server_block(SERVER, a, dict(a), None)
    assert idle["tokens_out"] == 0 and idle["decode_tok_s"] is None and "no tokens generated" in idle["reason"]
    restarted = telemetry.server_block(SERVER, a, {c: 1.0 for c in telemetry.COUNTERS}, None)
    assert restarted["tokens_in"] is None and "restarted" in restarted["reason"]
    gone = telemetry.server_block(SERVER, a, None, None)
    assert gone["tokens_in"] is None and "stopped answering" in gone["reason"]


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
