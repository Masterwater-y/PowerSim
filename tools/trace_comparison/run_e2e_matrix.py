"""Exclusive CPU-budget end-to-end campaign; validation is outside engine wall."""

import argparse
import hashlib
import json
import os
import pathlib
import re
import shutil
import sqlite3
import subprocess
import sys
from run_native_matrix import ROOT, freeze_config
from source_identity import audit_artifact
from baseline_policy import ZSIM_PARALLEL_BASELINE

BUILD = ROOT / "tmp/e2e-throughput-20261116"
REFERENCE = ROOT / "tmp/native-trace-comparison-final-20261116"


def read(path):
    return json.loads(pathlib.Path(path).read_text())


def digest(path):
    return hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()


def extract(engine, out, cores, sniper_model="rob"):
    if engine == "fastsim":
        d = read(out / "stats.json")
        by = {x["core"]: x for x in d["cores"]}
        if set(by) != set(range(cores)):
            raise ValueError("FastSim core coverage mismatch")
        return dict(
            instructions=[by[c]["instructions"] for c in range(cores)],
            uops=[by[c]["uops"] for c in range(cores)],
            user_uops=d["scope_metrics"]["user_trace_uops"],
            simulation_seconds=d["wall_time_breakdown"]["measurement_seconds"],
            model_elapsed_seconds=d["time_accounting"]["elapsed_time_seconds"],
        )
    if engine == "zsim":
        d = json.loads((out / "engine.log").read_text().splitlines()[-1])
        return dict(
            instructions=d["instructions"],
            uops=d["uops"],
            simulation_seconds=d["measurement_seconds"],
            model_elapsed_seconds=max(d["native_cycles"]) / d["frequency_hz"],
            native=d,
        )
    con = sqlite3.connect(out / "output/simulation/sim.stats.sqlite3")
    if sniper_model not in ["rob", "interval"]:
        raise ValueError("unsupported Sniper core model")
    timer = "rob_timer" if sniper_model == "rob" else "interval_timer"
    endpoint = "retired" if sniper_model == "rob" else "dispatched"
    q = "select v.core,v.value from 'values' v join names n using(nameid) where n.objectname=? and n.metricname=? and v.prefixid=(select prefixid from prefixes where prefixname='roi-end') order by v.core"
    mac = dict(con.execute(q, (timer, f"fst_{endpoint}_macros")))
    uop = dict(con.execute(q, (timer, f"fst_{endpoint}_uops")))
    con.close()
    if set(mac) != set(range(cores)) or set(uop) != set(range(cores)):
        raise ValueError("Sniper model completion population coverage mismatch")
    text = (out / "engine.log").read_text()
    begins = {
        int(c): (int(t), int(h))
        for c, t, h in re.findall(
            r"\[FST-BEGIN:(\d+)\] model_elapsed_fs=(\d+) host_monotonic_ns=(\d+)", text
        )
    }
    ends = {
        int(c): (int(t), int(h))
        for c, t, h in re.findall(
            r"\[FST-ENDPOINT:(\d+)\] complete_macros=\d+ model_elapsed_fs=(\d+) stop=0 host_monotonic_ns=(\d+)",
            text,
        )
    }
    if set(begins) != set(range(cores)) or set(ends) != set(range(cores)):
        raise ValueError("Sniper measurement endpoint mismatch")
    return dict(
        instructions=[mac[c] for c in range(cores)],
        uops=[uop[c] for c in range(cores)],
        simulation_seconds=(
            max(v[1] for v in ends.values()) - min(v[1] for v in begins.values())
        )
        / 1e9,
        model_elapsed_seconds=max(ends[c][0] - begins[c][0] for c in range(cores))
        / 1e15,
    )


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", type=pathlib.Path, required=True)
    p.add_argument("--case")
    p.add_argument("--strict-pairs", action="store_true")
    p.add_argument("--zsim-binary", type=pathlib.Path, default=ZSIM_PARALLEL_BASELINE)
    p.add_argument("--zsim-prepare-jobs", default="auto")
    p.add_argument("--sniper-tree", type=pathlib.Path, default=BUILD / "sniper-source")
    p.add_argument("--sniper-core-type", choices=["rob", "interval"], default="rob")
    p.add_argument(
        "--engines",
        nargs="+",
        choices=["fastsim", "zsim", "sniper"],
        default=["fastsim", "zsim", "sniper"],
    )
    a = p.parse_args()
    output = a.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    (output / "configs").mkdir()
    rows = read(REFERENCE / "fastsim/freeze.json")["cases"]
    if a.case:
        rows = [r for r in rows if r["case"] == a.case]
    if not rows:
        raise ValueError("no cases selected")
    shutil.copy2(REFERENCE / "input-identity.json", output / "input-identity.json")
    reference = read(REFERENCE / "fastsim/summary.json")
    population = {r["case"]: r for r in reference}
    proof = read(output / "input-identity.json")
    for row in rows:
        case = row["case"]
        proof["cases"][case].update(
            manifest=row["manifest"],
            manifest_sha256=population[case]["manifest_sha256"],
        )
        consumed = read(REFERENCE / "fastsim" / case / "stats.json")[
            "functional_source_identity"
        ]
        proof["cases"][case]["ancillary_artifacts"] = [
            audit_artifact(art, proof["cutoff_ns"])
            for art in consumed.get("artifacts", [])
        ]
    (output / "input-identity.json").write_text(json.dumps(proof, indent=2))
    binaries = {
        "fastsim": BUILD / "fastsim-source/build/fastsim",
        "zsim": a.zsim_binary.resolve(),
        "sniper": a.sniper_tree.resolve() / "build/fst-native/lib/sniper",
    }
    for k in ["fastsim", "zsim"]:
        dest = output / (k + "-frozen")
        shutil.copy2(binaries[k], dest)
        binaries[k] = dest
    hashes = {k: digest(v) for k, v in binaries.items()}
    sniper = a.sniper_tree.resolve()
    shutil.copy2(REFERENCE / "sniper-repaired/sniper.cfg", output / "sniper.cfg")
    for row in rows:
        config, values = freeze_config(row["frozen_config"], output / "configs")
        row["e2e_config"] = str(config)
        row["e2e_values"] = values
    (output / "freeze.json").write_text(
        json.dumps(
            dict(
                cases=rows,
                binaries={k: str(v) for k, v in binaries.items()},
                binary_sha256=hashes,
                driver_sha256=digest(__file__),
                launcher_sha256=digest(
                    pathlib.Path(__file__).with_name("e2e_launch.py")
                ),
                original_validation_campaign=str(REFERENCE),
                cpu_budget="52-63",
                concurrency="one engine/case at a time",
                sniper_core_type=a.sniper_core_type,
                sniper_config_sha256=digest(output / "sniper.cfg"),
            ),
            indent=2,
        )
    )
    results = []
    env = dict(
        os.environ,
        LD_LIBRARY_PATH="/opt/gcc-11/lib64",
        SNIPER_SIM_LD_LIBRARY_PATH="/opt/gcc-11/lib64",
        FST_ALLOW_INCOMPLETE_CODE="1",
    )
    env.pop("FASTSIM_LLC_AUDIT", None)
    for index, row in enumerate(rows):
        case = row["case"]
        expected = population[case]
        cores = len(expected["instructions"])
        values = row["e2e_values"]
        user = read(REFERENCE / "fastsim" / case / "stats.json")["scope_metrics"][
            "user_trace_uops"
        ]
        env.update(
            FST_CODE_DRAM_BYTES=values.get("dram.size", "34359738368"),
            FST_CODE_SEED=values.get("trace.instruction_mapping_seed", "1"),
            FST_CODE_PHYS_BITS=values.get(
                "trace.instruction_physical_address_bits", "48"
            ),
            FST_CODE_PAGE_BITS=values.get("trace.instruction_page_bits", "12"),
            FST_CODE_SPLIT_DRAM=(
                "1"
                if values.get("dram.memory_layout") == "x86-low3g-high4g-v1"
                else "0"
            ),
        )
        order = a.engines
        order = order[index % len(order) :] + order[: index % len(order)]
        for engine in order:
            modes = ["strict", "trusted"] if a.strict_pairs else ["trusted"]
            pair = []
            for mode in modes:
                out = output / case / (engine + "-" + mode)
                out.mkdir(parents=True)
                result = dict(
                    case=case,
                    engine=engine,
                    mode=mode,
                    cores=cores,
                    binary_sha256=hashes[engine],
                    formal_throughput_acceptance=False,
                )
                if engine == "sniper":
                    result["sniper_core_type"] = a.sniper_core_type
                cwd = ROOT
                job_env = env.copy()
                if engine == "zsim" and a.zsim_prepare_jobs is not None:
                    job_env["TRACEBENCH_PREPARE_JOBS"] = a.zsim_prepare_jobs
                    result["requested_preparation_jobs"] = a.zsim_prepare_jobs
                if engine == "fastsim":
                    command = [
                        str(binaries[engine]),
                        "simulate",
                        "--config",
                        row["e2e_config"],
                        "--manifest",
                        row["manifest"],
                        "--cores",
                        str(cores),
                        "--measurement-scope",
                        "user-plus-kernel",
                        "--native-kernel-trace",
                        "true",
                        "--allow-mmio-escape",
                        "true",
                        "--allow-cross-page-without-virtual-token",
                        "true",
                        "--preload-validated-input",
                        "true",
                        "--perf-cache-input-identity",
                        "false",
                        "--output",
                        str(out / "stats.json"),
                    ]
                elif engine == "zsim":
                    command = [
                        str(binaries[engine]),
                        row["manifest"],
                        row["e2e_config"],
                        "--allow-incomplete-code",
                    ]
                else:
                    cwd = sniper
                    command = [
                        str(sniper / "run-sniper"),
                        "--build-dir=build/fst-native",
                        "-c",
                        str(output / "sniper.cfg"),
                        "-g",
                        f"perf_model/core/type={a.sniper_core_type}",
                        "-d",
                        str(out / "output"),
                        "-g",
                        f"general/total_cores={cores}",
                        "-g",
                        f"traceinput/num_apps={cores}",
                        "-g",
                        f"traceinput/fst_manifest={row['manifest']}",
                        "-g",
                        f"perf_model/l3_cache/shared_cores={cores}",
                        "-g",
                        f"perf_model/dram_directory/interleaving={cores}",
                        "-g",
                        f"perf_model/dram/controllers_interleaving={cores}",
                        "--sim-end=last",
                        "--trace-manual",
                    ]
                    for core in range(4, cores):
                        command += ["-g", f"traceinput/thread_{core}=fst{core}"]
                (out / "command.json").write_text(json.dumps(command, indent=2))
                launch = [
                    sys.executable,
                    str(ROOT / "scripts/run_high_frequency.py"),
                    "--",
                    sys.executable,
                    str(ROOT / "tools/trace_comparison/e2e_launch.py"),
                    "--receipt",
                    str(output / "input-identity.json"),
                    "--case",
                    case,
                    "--engine",
                    engine,
                    "--cores",
                    str(cores),
                    "--command",
                    str(out / "command.json"),
                    "--cwd",
                    str(cwd),
                    "--log",
                    str(out / "engine.log"),
                    "--metrics",
                    str(out / "timing.json"),
                ]
                if mode == "strict":
                    launch += ["--strict-pilot"]
                try:
                    with (out / "launcher.log").open("w") as log:
                        r = subprocess.run(
                            launch,
                            env=job_env,
                            cwd=ROOT,
                            stdout=log,
                            stderr=subprocess.STDOUT,
                        )
                    if r.returncode:
                        raise ValueError("launch/engine failed; see logs")
                    result.update(extract(engine, out, cores, sniper_model=a.sniper_core_type))
                    result["timing"] = read(out / "timing.json")
                    if (
                        result["instructions"] != expected["instructions"]
                        or result["uops"] != expected["uops"]
                    ):
                        raise ValueError("per-core population mismatch")
                    if result.get("user_uops", user) != user:
                        raise ValueError("user numerator changed")
                    result["user_uops"] = user
                    result["e2e_user_mups"] = (
                        user / result["timing"]["e2e_seconds"] / 1e6
                    )
                    result["population_match"] = True
                    if mode == "trusted" and not result["timing"]["trusted_input"]:
                        raise ValueError("unqualified end-to-end scope")
                    pair.append(result)
                except Exception as e:
                    result["error"] = str(e)
                (out / "result.json").write_text(json.dumps(result, indent=2))
                results.append(result)
                (output / "progress.json").write_text(json.dumps(results, indent=2))
                print(
                    case,
                    engine,
                    mode,
                    result.get("e2e_user_mups"),
                    result.get("error", ""),
                    flush=True,
                )
            if a.strict_pairs and len(pair) == 2:
                # Content-equal inputs must produce identical deterministic model outputs.
                if engine == "fastsim":
                    ds = [
                        read(output / case / (engine + "-" + m) / "stats.json")
                        for m in modes
                    ]
                    if (
                        ds[0]["cores"] != ds[1]["cores"]
                        or ds[0]["time_accounting"] != ds[1]["time_accounting"]
                    ):
                        raise ValueError("FastSim model changed in trusted mode")
                if engine == "zsim":
                    z0, z1 = pair[0]["native"], pair[1]["native"]
                    if any(
                        z0[k] != z1[k]
                        for k in [
                            "instructions",
                            "uops",
                            "prepared_hashes",
                            "unknown_code_records",
                        ]
                    ) or max(z0["native_cycles"]) != max(z1["native_cycles"]):
                        raise ValueError(
                            "zsim prepared input or common completion changed in trusted mode"
                        )
                    drift = [
                        b - a for a, b in zip(z0["native_cycles"], z1["native_cycles"])
                    ]
                    (output / case / "zsim-equivalence.json").write_text(
                        json.dumps(
                            dict(
                                prepared_inputs_identical=True,
                                common_completion_identical=True,
                                per_core_cycle_drift=drift,
                            ),
                            indent=2,
                        )
                    )
                if engine == "sniper":
                    logs = [
                        (output / case / (engine + "-" + m) / "engine.log").read_text()
                        for m in modes
                    ]
                    pattern = r"\[FST-ENDPOINT:(\d+)\] complete_macros=(\d+) model_elapsed_fs=(\d+) stop=(\d+)"
                    if sorted(re.findall(pattern, logs[0])) != sorted(
                        re.findall(pattern, logs[1])
                    ):
                        raise ValueError("Sniper model changed in trusted mode")
                print(case, engine, "strict/trusted equivalence PASSED", flush=True)
    (output / "summary.json").write_text(json.dumps(results, indent=2))
    return int(any(r.get("error") for r in results))


if __name__ == "__main__":
    raise SystemExit(main())
