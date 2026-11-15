#!/usr/bin/env bash
# Run on the Linux simulation server in a user-controlled terminal.
set -euo pipefail
SCRIPT_ROOT="$(dirname "$(dirname "$(readlink -f -- "${BASH_SOURCE[0]}")")")"
if [[ -n "${FASTSIM_PYTHON:-}" ]]; then
    PYTHON_BIN="$FASTSIM_PYTHON"
elif [[ -x /root/.pyenv/versions/3.11.14/bin/python3 ]]; then
    PYTHON_BIN=/root/.pyenv/versions/3.11.14/bin/python3
else
    PYTHON_BIN="$(command -v python3)"
fi
exec "$PYTHON_BIN" -u - "$SCRIPT_ROOT" "$@" <<'PY'
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys

ROOT = Path(sys.argv[1]).resolve()
parser = argparse.ArgumentParser(description="三个真实 C4 小 FS 的逐核 unknown 验证")
parser.add_argument("--check-only", action="store_true",
                    help="只检查文件和输入哈希，不访问 /dev/kvm、不启动采集")
parser.add_argument("--output", type=Path, help="新的回执目录，须位于项目内")
args = parser.parse_args(sys.argv[2:])
if sys.version_info < (3, 10):
    raise SystemExit("需要 Python >= 3.10；可设置 FASTSIM_PYTHON 指定解释器。")

os.environ["M5_OVERRIDE_PY_SOURCE"] = "true"
libraries = ["/root/.pyenv/versions/3.11.14/lib", "/opt/gcc-11/lib64",
             "/home/data00/xuhaoen/toolchains/protobuf/lib"]
if os.environ.get("LD_LIBRARY_PATH"):
    libraries.append(os.environ["LD_LIBRARY_PATH"])
os.environ["LD_LIBRARY_PATH"] = ":".join(libraries)
sys.path.insert(0, str(ROOT / "tools"))

gem5 = ROOT / "gem5-spr/build/X86/gem5.opt"
binary = ROOT / "tmp/spr-retired-cache-v2-20261107-build/spr-retired-cache-smoke"
launcher = ROOT / "tmp/spec-speed-32g-c4-c8-c16-20261027-final/frozen/native-launcher"
scheduler = ROOT / "tmp/spr-asid-task-clock-20261104/kernel-scheduler-profile.json"
kernel = Path("/root/.cache/gem5/x86-linux-kernel-5.4.49-1.0.0")
disk = Path("/root/.cache/gem5/x86-ubuntu-18.04-img-1.0.0")
required = [gem5, binary, launcher, scheduler, kernel, disk,
            ROOT / "build/fastsim_audit_retired_frontend",
            ROOT / "tools/run_spr_retired_cache_smoke.py",
            ROOT / "tools/prepare_spr_capture.py",
            ROOT / "tools/retired_cache_contract.py",
            ROOT / "tools/audit_p1_native_response_sideband.py",
            ROOT / "gem5-spr/configs/fastsim/fs_capture.py",
            ROOT / "gem5-spr/configs/fastsim/collection.py",
            ROOT / "scripts/run_gem5_sync_probe.py",
            ROOT / "tools/spec_rate_checkpoint.py",
            ROOT / "configs/spr-retired-load-miss-v2.json"]

def sha(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for data in iter(lambda: stream.read(16 << 20), b""):
            digest.update(data)
    return digest.hexdigest()

try:
    for path in required:
        if not path.is_file():
            raise RuntimeError(f"缺少文件：{path}")
    for path in (gem5, binary, launcher, ROOT / "build/fastsim_audit_retired_frontend"):
        if not os.access(path, os.X_OK):
            raise RuntimeError(f"文件不可执行：{path}")
    from retired_cache_contract import audit_retired_cache, MISS_EVENTS, SPR_MISS_POLICY
    from audit_p1_native_response_sideband import audit_result
    print("检查依赖与输入哈希……", flush=True)
    frozen = {str(path): sha(path) for path in required}
    expected = {
        kernel: "da3d5e7dbf696b977f635d1123e06b324af873183d90111101ced005dc64e174",
        disk: "00aed79a5b91fbd97885944c3f70511e19756d4f6ae9e13e15322ab9c8759905",
    }
    for path, digest in expected.items():
        if frozen[str(path)] != digest:
            raise RuntimeError(f"本地资源与本轮已核对输入不一致：{path}")
except Exception as error:
    raise SystemExit(f"预检查失败：{error}")

if args.check_only:
    print("预检查通过。未访问 KVM，未运行 FS；这不代表 unknown 验收通过。")
    raise SystemExit(0)

stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
output = (args.output or ROOT / "tmp" /
          f"spr-retired-cache-smoke-manual-{stamp}-{os.getpid()}").resolve()
try:
    output.relative_to(ROOT)
except ValueError:
    raise SystemExit("回执目录必须位于项目目录内。")
if output.exists():
    raise SystemExit(f"拒绝覆盖已有目录：{output}")
output.mkdir(parents=True)
print(f"回执目录：{output}", flush=True)

def dump(path, value):
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)

dump(output / "freeze.json", {
    "files": frozen, "python": sys.executable, "target": 50000,
    "warmup": 500000, "participant_cores": 4, "memory": "32GiB",
    "scope": "workload_task", "script_sha256": sha(
        ROOT / "scripts/run_spr_retired_cache_smoke_oneclick.sh"),
})
states = [{"mode": mode, "phase": "not-run", "passed": False} for mode in range(3)]

def report():
    summary = {
        "schema": "spr-small-fs-unknown-validation-v1", "states": states,
        "expected_modes": [0, 1, 2], "scope": "workload_task",
        "passed": all(state["passed"] for state in states),
        "hardware_equivalence": "unverified",
        "special_path_coverage": "仅报告实际计数；模式名不证明覆盖",
        "neutrality_and_same_source_replay": "not-run",
        "four_workload_acceptance": "not-run",
    }
    dump(output / "summary.json", summary)
    lines = ["# 小 FS unknown 验证", "",
             f"当前状态：{'通过' if summary['passed'] else '未通过/未完成'}。",
             "范围为 workload_task。只验证本次小 FS；不代表完整修复验收。",
             "unknown 占比的分母为该核全部合格退休 load。",
             "SPR 硬件等价：unverified。", "",
             "| mode | core | eligible loads | L1 miss | L1 unknown | L2 miss | L2 unknown | L3 miss | L3 unknown |",
             "|---|---|---:|---:|---|---:|---|---:|---|"]
    for state in states:
        for core in state.get("per_core", []):
            row = [state["mode"], core["core_id"], core["eligible_loads"]]
            for name in MISS_EVENTS:
                event = core["events"][name]
                ratio = event["unknown"] * 100 / core["eligible_loads"] if core["eligible_loads"] else None
                row.extend([event["count"], f"{event['unknown']} ({ratio:.6f}%)"
                            if ratio is not None else f"{event['unknown']} (n/a)"])
            lines.append("| " + " | ".join(map(str, row)) + " |")
    lines.extend(["", "## 模式状态", ""])
    for state in states:
        lines.append(f"- mode {state['mode']}: {state['phase']}; {state.get('error', '')}")
    lines.extend(["", "服务路径、正负估计拆分与来源门禁见 summary.json 和逐模式 audit JSON。",
                  "未测量的模式没有退休计数，不能将其解释为零 unknown。", ""])
    (output / "summary.md").write_text("\n".join(lines))
    return summary

def stage(state, name, argv):
    state["phase"] = name
    report()
    log_path = output / f"mode{state['mode']}-{name}.log"
    dump(output / f"mode{state['mode']}-{name}-command.json",
         {"argv": argv, "cwd": str(ROOT)})
    print(f"[mode {state['mode']}] {name}；日志：{log_path}", flush=True)
    with log_path.open("x") as log:
        child = subprocess.Popen(argv, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                                 start_new_session=True)
        dump(output / f"mode{state['mode']}-{name}-pid.json", {"pid": child.pid})
        try:
            code = child.wait()
        except BaseException:
            try:
                os.killpg(child.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            child.wait()
            raise
    dump(output / f"mode{state['mode']}-{name}-exit.json", {"returncode": code})
    if code:
        print(log_path.read_text(errors="replace")[-4000:], file=sys.stderr)
        if "--output" in argv:
            producer_log = Path(argv[argv.index("--output") + 1]) / "capture.log"
            if producer_log.is_file():
                print(producer_log.read_text(errors="replace")[-4000:], file=sys.stderr)
        raise RuntimeError(f"{name} 返回 {code}；详见 {log_path}")

def capture_receipt(source, checkpoint=False):
    receipt = json.loads((source / "capture-receipt.json").read_text())
    result = json.loads((source / "sync-run-result.json").read_text())
    if receipt["returncode"] or receipt["guard_reason"]:
        raise RuntimeError(f"采集回执未通过：{source}")
    if not result.get("checkpoint_saved" if checkpoint else "common_end"):
        raise RuntimeError(f"准备状态/公共终点未完成：{source}")
    if not checkpoint and not result.get("measurement_open"):
        raise RuntimeError(f"测量窗口未打开：{source}")

report()
try:
    import fcntl
    with open("/dev/kvm", "rb+", buffering=0) as device:
        if fcntl.ioctl(device.fileno(), 0xAE00, 0) != 12:
            raise RuntimeError("KVM API 版本不符合预期")
    print("KVM 设备可打开；继续用真实 FS 验证。", flush=True)
    for state in states:
        mode = state["mode"]
        source = output / f"mode{mode}"
        checkpoint = output / f"checkpoint-mode{mode}"
        checkpoint_source = output / f"checkpoint-mode{mode}-run"
        state["source"] = str(source)
        common = [sys.executable, str(ROOT / "tools/run_spr_retired_cache_smoke.py"),
                  "--gem5", str(gem5), "--binary", str(binary), "--mode", str(mode),
                  "--kernel", str(kernel), "--disk", str(disk),
                  "--launcher", str(launcher), "--scheduler-profile", str(scheduler),
                  "--target", "50000", "--warmup", "500000", "--run"]
        stage(state, "checkpoint", common + ["--output", str(checkpoint_source),
              "--save-checkpoint", str(checkpoint)])
        capture_receipt(checkpoint_source, checkpoint=True)
        stage(state, "capture", common + ["--output", str(source),
              "--checkpoint", str(checkpoint)])
        capture_receipt(source)
        stage(state, "prepare", [sys.executable, str(ROOT / "tools/prepare_spr_capture.py"),
              "--source", str(source), "--workload-label", f"SPR-small-mode{mode}"])
        state["phase"] = "audit"
        report()
        preparation = json.loads((source / "spr-capture-preparation.json").read_text())
        reference = audit_retired_cache(source / "tao_trace")
        dump(output / f"mode{mode}-retired-audit.json", reference)
        state["per_core"] = []
        for core in reference["per_core"]:
            cid = core["core_id"]
            events = core["scopes"]["workload_task"]["events"]
            raw = json.loads((source / f"tao_trace/oracle/perf-retirement-core{cid}.json").read_text())
            paths = raw["scopes"]["workload_task"]["coverage"]["spr_service_paths"]
            if paths["policy"] != SPR_MISS_POLICY or paths["unit"] != "retired-load-macro-containing-path":
                raise RuntimeError("服务路径计数策略或单位不匹配")
            counts = paths["counts"]
            if not isinstance(counts, dict) or len(counts) != 15 or any(
                    type(value) is not int or not 0 <= value <= events["retired_loads"]["count"]
                    for value in counts.values()):
                raise RuntimeError("服务路径计数缺失或超出退休 load 人口")
            state["per_core"].append({
                "core_id": cid, "eligible_loads": events["retired_loads"]["count"],
                "population_unknown": events["retired_loads"]["unknown"],
                "events": {name: events[name] for name in MISS_EVENTS},
                "service_paths": paths,
            })
        state["membership_policy"] = reference.get("membership_policy")
        state["source_evidence_gaps"] = reference.get("evidence_gaps", [])
        state["unknown_zero"] = (
            sorted(core["core_id"] for core in state["per_core"]) == [0, 1, 2, 3] and
            all(core["eligible_loads"] > 0 and core["population_unknown"] == 0 and
                all(event["unknown"] == 0 for event in core["events"].values())
                for core in state["per_core"]))
        native = audit_result(source)
        dump(output / f"mode{mode}-native-audit.json", native)
        state["native_hierarchy_conserved"] = native["native_hierarchy_semantic_comparable"]
        state["target_identity_valid"] = preparation.get("target_identity_valid") is True
        state["passed"] = (state["unknown_zero"] and state["membership_policy"] == SPR_MISS_POLICY
                           and not state["source_evidence_gaps"] and state["target_identity_valid"]
                           and state["native_hierarchy_conserved"])
        if not state["passed"]:
            raise RuntimeError("逐核 unknown/来源/原生守恒门禁未通过；已保留原始计数")
        state["phase"] = "complete"
        report()
        print(f"[mode {mode}] 小 FS unknown 门禁通过。", flush=True)
    for path in required:
        if sha(path) != frozen[str(path)]:
            raise RuntimeError(f"运行期间输入或工具发生变化：{path}")
except BaseException as error:
    active = next((state for state in states if state["phase"] not in ("not-run", "complete")), None)
    if active is not None:
        active["failed_phase"] = active["phase"]
        active["phase"] = "failed"
        active["passed"] = False
        active["error"] = f"{type(error).__name__}: {error}"
    else:
        states[0]["passed"] = False
        states[0]["error"] = f"{type(error).__name__}: {error}"
        states[0]["phase"] = "preflight-or-final-check-failed"
    report()
    print(f"验证停止：{error}\n结果：{output / 'summary.md'}", file=sys.stderr)
    raise SystemExit(130 if isinstance(error, KeyboardInterrupt) else 1)

report()
print(f"三个小 FS unknown 门禁均通过。\n结果：{output / 'summary.md'}", flush=True)
PY
