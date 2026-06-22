import json
import os
import re
import subprocess


SPR_MODELS = {143}


def validate_machine(target="spr", pmu_json=None, uncore_alias=None, spr_discovery=None):
    cpu = parse_cpuinfo()
    lscpu = parse_lscpu()
    family = int(cpu.get("cpu family", -1)) if cpu.get("cpu family", "").isdigit() else -1
    model = int(cpu.get("model", -1)) if cpu.get("model", "").isdigit() else -1
    vendor = cpu.get("vendor_id", "")
    compatible = bool(target == "spr" and vendor == "GenuineIntel" and family == 6 and model in SPR_MODELS)
    pmu = {
        "core_events_file_found": bool(pmu_json and os.path.exists(pmu_json)),
        "uncore_alias_found": bool(uncore_alias and os.path.exists(uncore_alias)),
        "perf_found": _command_exists("perf"),
        "perf_basic_events": _perf_has_basic_events(),
        "spr_discovery_ok": False,
        "spr_discovery": {},
    }
    if spr_discovery and os.path.exists(spr_discovery):
        pmu["spr_discovery"] = run_spr_discovery(spr_discovery)
        pmu["spr_discovery_ok"] = pmu["spr_discovery"].get("ok", False)
    return {
        "ok": compatible,
        "target": target,
        "detected": {
            "vendor": vendor,
            "family": family,
            "model": model,
            "model_name": cpu.get("model name", ""),
            "stepping": cpu.get("stepping", ""),
            "sockets": _int_or_none(lscpu.get("Socket(s)")),
            "cores_per_socket": _int_or_none(lscpu.get("Core(s) per socket")),
            "threads_per_core": _int_or_none(lscpu.get("Thread(s) per core")),
            "logical_cpus": _int_or_none(lscpu.get("CPU(s)")),
        },
        "pmu": pmu,
        "verdict": "compatible" if compatible else "incompatible",
    }


def parse_cpuinfo(path="/proc/cpuinfo"):
    out = {}
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            if not line.strip():
                if out:
                    break
                continue
            if ":" in line:
                k, v = line.split(":", 1)
                out[k.strip()] = v.strip()
    return out


def parse_lscpu():
    try:
        proc = subprocess.run(["lscpu"], check=False, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
    except Exception:
        return {}
    out = {}
    for line in proc.stdout.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip()
    return out


def run_spr_discovery(path):
    try:
        proc = subprocess.run(["python3", path, "--format", "compact"], check=False, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    data = {"ok": proc.returncode == 0, "raw": proc.stdout.strip(), "stderr": proc.stderr.strip()}
    m = re.search(r"FORMAT .*codename=(\w+)", proc.stdout)
    if m:
        data["codename"] = m.group(1)
    m = re.search(r"SOCKETS count=(\d+)", proc.stdout)
    if m:
        data["sockets"] = int(m.group(1))
    exposed = [int(x) for x in re.findall(r"exposed_cha=(\d+)", proc.stdout)]
    if exposed:
        data["cha_per_socket_exposed"] = exposed
    widths = [int(x) for x in re.findall(r"ctr_width=(\d+)", proc.stdout)]
    if widths:
        data["ctr_width"] = sorted(set(widths))
    return data


def _command_exists(cmd):
    from shutil import which
    return which(cmd) is not None


def _perf_has_basic_events():
    if not _command_exists("perf"):
        return False
    try:
        proc = subprocess.run(["perf", "list", "--no-desc"], check=False, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20)
        text = proc.stdout + proc.stderr
        return all(ev in text for ev in ["instructions", "cycles", "branch-misses"])
    except Exception:
        return False


def _int_or_none(value):
    if value is None:
        return None
    m = re.search(r"\d+", str(value))
    return int(m.group(0)) if m else None


def dumps(obj):
    return json.dumps(obj, indent=2, sort_keys=False) + "\n"

