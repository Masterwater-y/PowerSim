"""Run the retained C4/C8 matrix with frozen engines and explicit diagnostics.

Successful exits and population checks are mechanical evidence. Accuracy and
source qualification remain separate. Parallel host throughput is diagnostic.
"""

import argparse
import concurrent.futures
import hashlib
import json
import os
import pathlib
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from source_identity import stat_identity
from baseline_policy import ZSIM_PARALLEL_BASELINE

ROOT = pathlib.Path(__file__).resolve().parents[2]
NATIVE = ROOT / "tmp/native-trace-adapters-20261116"
SNIPER = NATIVE / "sniper-source-final"


def digest(path):
    return hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()


def cases():
    rows = []
    folder = ROOT / "tmp/host-throughput-phase2-20261113"
    for path in sorted(folder.glob("equivalence-c04-*-candidate.run.json")):
        data = json.loads(path.read_text())
        rows.append(
            (path.name.split("-candidate")[0].replace("equivalence-", ""), data["argv"])
        )
    for row in json.loads(
        (ROOT / "tmp/c8-fastsim-evaluation-20261116/replays.json").read_text()
    ):
        rows.append((row["case"], row["argv"]))
    result = [
        dict(
            case=case,
            manifest=argv[argv.index("--manifest") + 1],
            config=argv[argv.index("--config") + 1],
        )
        for case, argv in rows
    ]
    if len(result) != 10 or len({row["case"] for row in result}) != 10:
        raise ValueError("expected exactly ten unique retained C4/C8 cases")
    return result


def freeze_config(path, output, seen=None):
    path = pathlib.Path(path).resolve()
    seen = set() if seen is None else seen
    if path in seen:
        raise ValueError("configuration include cycle")
    seen.add(path)
    text = path.read_text()
    target = output / (digest(path) + ".cfg")
    values = {}
    rendered = []
    for line in text.splitlines():
        clean = line.split("#", 1)[0].strip()
        if "=" not in clean:
            rendered.append(line)
            continue
        key, value = map(str.strip, clean.split("=", 1))
        value = value.strip('"')
        if key == "config.include":
            child = pathlib.Path(value)
            include, inherited = freeze_config(
                child if child.is_absolute() else path.parent / child, output, seen
            )
            values.update(inherited)
            rendered.append(f"config.include = {include}")
        else:
            values[key] = value
            rendered.append(line)
    target.write_text("\n".join(rendered) + "\n")
    seen.remove(path)
    return target, values


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--simulator", choices=["fastsim", "zsim", "sniper"], required=True
    )
    parser.add_argument("--output", type=pathlib.Path, required=True)
    parser.add_argument("--jobs", type=int, choices=range(1, 4), default=3)
    parser.add_argument("--allow-incomplete-code", action="store_true")
    parser.add_argument("--sniper-tree", type=pathlib.Path, default=SNIPER)
    parser.add_argument(
        "--fastsim-binary", type=pathlib.Path, default=ROOT / "build/fastsim"
    )
    parser.add_argument(
        "--zsim-binary", type=pathlib.Path, default=ZSIM_PARALLEL_BASELINE
    )
    parser.add_argument("--zsim-prepare-jobs", default="auto")
    args = parser.parse_args()
    sniper = args.sniper_tree.resolve()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    (output / "configs").mkdir()
    rows = cases()
    executable = {
        "fastsim": args.fastsim_binary.resolve(),
        "zsim": args.zsim_binary.resolve(),
        "sniper": sniper / "build/fst-native/lib/sniper",
    }[args.simulator]
    original_hash = digest(executable)
    if args.simulator != "sniper":
        frozen = output / "engine-frozen"
        shutil.copy2(executable, frozen)
        if digest(frozen) != original_hash:
            raise ValueError("executable changed while freezing")
        executable = frozen
    for row in rows:
        frozen_config, values = freeze_config(row["config"], output / "configs")
        row["frozen_config"] = str(frozen_config)
        row["effective_values"] = values
    sniper_config = output / "sniper.cfg"
    if args.simulator == "sniper":
        shutil.copy2(
            ROOT / "tmp/sniper-zsim-spr-20261112/sniper/config/spr_m1_fastsim.cfg",
            sniper_config,
        )
    (output / "freeze.json").write_text(
        json.dumps(
            dict(
                simulator=args.simulator,
                binary=str(executable),
                binary_sha256=original_hash,
                driver_sha256=digest(__file__),
                cases=rows,
                allow_incomplete_code=args.allow_incomplete_code,
                zsim_preparation_jobs=args.zsim_prepare_jobs,
            ),
            indent=2,
        )
    )
    environment = dict(
        os.environ,
        LD_LIBRARY_PATH="/opt/gcc-11/lib64",
        SNIPER_SIM_LD_LIBRARY_PATH="/opt/gcc-11/lib64",
    )

    def run(row):
        case = row["case"]
        out = output / case
        out.mkdir()
        result = dict(case=case, formal_validation=False, population_match=False)
        try:
            config = pathlib.Path(row["frozen_config"])
            values = row["effective_values"]
            manifest = pathlib.Path(row["manifest"])
            entries = [
                line.split()
                for line in manifest.read_text().splitlines()
                if line.strip() and not line.lstrip().startswith("#")
            ]
            expected = {int(e[0]): (int(e[5]), int(e[7])) for e in entries}
            cores = len(expected)
            if set(expected) != set(range(cores)):
                raise ValueError("physical core IDs must be dense and unique")
            artifacts = {}
            for entry in entries:
                path = pathlib.Path(entry[2])
                path = path if path.is_absolute() else manifest.parent / path
                for artifact in path.parent.glob(path.name + "*"):
                    if artifact.is_file():
                        artifacts[str(artifact)] = stat_identity(artifact)
            result["prelaunch_artifact_stats"] = artifacts
            result.update(
                expected_per_core=expected,
                manifest_sha256=digest(manifest),
                frozen_config=str(config),
                original_config_sha256=digest(row["config"]),
            )
            env = environment.copy()
            if args.simulator == "zsim":
                env["TRACEBENCH_PREPARE_JOBS"] = args.zsim_prepare_jobs
            env.update(
                FST_ALLOW_INCOMPLETE_CODE="1" if args.allow_incomplete_code else "0",
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
            result["functional_mapping_environment"] = {
                k: v for k, v in env.items() if k.startswith("FST_")
            }
            cwd = ROOT
            if args.simulator == "zsim":
                command = [str(executable), str(manifest), str(config)]
                if args.allow_incomplete_code:
                    command.append("--allow-incomplete-code")
            elif args.simulator == "fastsim":
                command = [
                    str(executable),
                    "simulate",
                    "--config",
                    str(config),
                    "--manifest",
                    str(manifest),
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
                    "true",
                    "--output",
                    str(out / "stats.json"),
                ]
            else:
                cwd = sniper
                command = [
                    str(sniper / "run-sniper"),
                    "--build-dir=build/fst-native",
                    "-c",
                    str(sniper_config),
                    "-d",
                    str(out / "output"),
                    "-g",
                    f"general/total_cores={cores}",
                    "-g",
                    f"traceinput/num_apps={cores}",
                    "-g",
                    f"traceinput/fst_manifest={manifest}",
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
            command = [
                sys.executable,
                str(ROOT / "scripts/run_high_frequency.py"),
                "--",
                "bash",
                "-c",
                'cd "$1" && shift && exec "$@"',
                "--",
                str(cwd),
            ] + command
            (out / "command.json").write_text(json.dumps(command, indent=2))
            start = time.monotonic()
            result["process_start_wall_ns"] = time.time_ns()
            with (out / "run.log").open("w") as log:
                completed = subprocess.run(
                    command, cwd=cwd, env=env, stdout=log, stderr=subprocess.STDOUT
                )
            result.update(
                returncode=completed.returncode,
                process_wall_seconds=time.monotonic() - start,
            )
            result["process_end_wall_ns"] = time.time_ns()
            for path, before in artifacts.items():
                if stat_identity(path) != before:
                    raise ValueError("source artifact changed during replay: " + path)
            if completed.returncode != 0:
                raise ValueError("engine exited unsuccessfully; see per-case run.log")
            if args.simulator == "zsim":
                data = json.loads((out / "run.log").read_text().splitlines()[-1])
                macros, uops = data["instructions"], data["uops"]
                result["statistics"] = data
            elif args.simulator == "fastsim":
                data = json.loads((out / "stats.json").read_text())
                by_core = {c["core"]: c for c in data["cores"]}
                if set(by_core) != set(expected):
                    raise ValueError("FastSim physical core identity mismatch")
                macros = [by_core[c]["instructions"] for c in range(cores)]
                uops = [by_core[c]["uops"] for c in range(cores)]
                result["statistics_file"] = str(out / "stats.json")
                stats = output / "stats"
                stats.mkdir(exist_ok=True)
                (stats / (case + ".json")).symlink_to(out / "stats.json")
            else:
                con = sqlite3.connect(out / "output/simulation/sim.stats.sqlite3")
                query = "select v.core,v.value from 'values' v join names n using(nameid) where n.objectname=? and n.metricname=? and v.prefixid=(select prefixid from prefixes where prefixname=?) order by v.core"
                mac = dict(
                    con.execute(query, ("rob_timer", "fst_retired_macros", "roi-end"))
                )
                uop = dict(
                    con.execute(query, ("rob_timer", "fst_retired_uops", "roi-end"))
                )
                if set(mac) != set(expected) or set(uop) != set(expected):
                    raise ValueError("Sniper physical core identity mismatch")
                macros = [mac[c] for c in range(cores)]
                uops = [uop[c] for c in range(cores)]
                con.close()
                text = (out / "output/simulation/sim.stdout").read_text()
                result["semantic_hashes"] = {
                    int(c): h
                    for c, h in re.findall(r"\[FST-SEMANTICS:(\d+)\] hash=(\d+)", text)
                }
                result["unknown_code_records"] = {
                    int(c): int(n)
                    for c, n in re.findall(
                        r"\[FST-CODE-COVERAGE:(\d+)\] unknown_records=(\d+)", text
                    )
                }
                result["begin"] = {
                    int(c): dict(model_fs=int(t), host_ns=int(h))
                    for c, t, h in re.findall(
                        r"\[FST-BEGIN:(\d+)\] model_elapsed_fs=(\d+) host_monotonic_ns=(\d+)",
                        text,
                    )
                }
                result["end"] = {
                    int(c): dict(
                        macros=int(m), model_fs=int(t), stop=int(s), host_ns=int(h)
                    )
                    for c, m, t, s, h in re.findall(
                        r"\[FST-ENDPOINT:(\d+)\] complete_macros=(\d+) model_elapsed_fs=(\d+) stop=(\d+) host_monotonic_ns=(\d+)",
                        text,
                    )
                }
                if (
                    len(result["begin"]) != cores
                    or len(result["end"]) != cores
                    or any(v["stop"] for v in result["end"].values())
                ):
                    raise ValueError("missing or stopped physical stream endpoint")
            result["instructions"], result["uops"] = macros, uops
            result["population_match"] = (
                len(macros) == cores
                and len(uops) == cores
                and all((macros[c], uops[c]) == expected[c] for c in range(cores))
            )
        except Exception as error:
            result["error"] = str(error)
        (out / "result.json").write_text(json.dumps(result, indent=2))
        print(
            case,
            result.get("returncode"),
            result["population_match"],
            result.get("error", ""),
            flush=True,
        )
        return result

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as executor:
        results = list(executor.map(run, rows))
    (output / "summary.json").write_text(json.dumps(results, indent=2))
    return (
        0 if all(r["population_match"] and not r.get("error") for r in results) else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
