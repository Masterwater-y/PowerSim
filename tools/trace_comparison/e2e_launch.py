"""Verify receipts outside timing, then include all engine preparation in wall."""

import argparse
import hashlib
import json
import os
import pathlib
import resource
import subprocess
import time
from e2e_timing import elapsed_seconds, parse_endpoints
from source_identity import stat_identity


def verify_case(receipt, case):
    if receipt["policy"] != "local-sha256-and-prelaunch-ctime-v1":
        raise ValueError("unsupported validation receipt policy")
    row = receipt["cases"][case]
    if (
        "manifest" in row
        and hashlib.sha256(pathlib.Path(row["manifest"]).read_bytes()).hexdigest()
        != row["manifest_sha256"]
    ):
        raise ValueError("validated manifest content changed")
    if not row["artifacts"]:
        raise ValueError("empty source receipt")
    for art in row["artifacts"] + row.get("ancillary_artifacts", []):
        if stat_identity(art["path"]) != art["stat"]:
            raise ValueError("input changed since content audit: " + art["path"])
        if (
            max(art["stat"]["mtime_ns"], art["stat"]["ctime_ns"])
            >= receipt["cutoff_ns"]
        ):
            raise ValueError("input audit does not predate validated campaign")
    for art in row.get("ancillary_artifacts", []):
        if (
            hashlib.sha256(pathlib.Path(art["path"]).read_bytes()).hexdigest()
            != art["sha256"]
        ):
            raise ValueError("ancillary input content changed: " + art["path"])
    for directory, before in row["directories"].items():
        if stat_identity(directory) != before:
            raise ValueError("input namespace changed: " + directory)
    expected = {art["path"] for art in row["artifacts"]}
    actual = set()
    for art in row["artifacts"]:
        if art["role"] == "fst":
            f = pathlib.Path(art["path"])
            actual.update(str(p) for p in f.parent.glob(f.name + "*") if p.is_file())
    if expected != actual:
        raise ValueError("functional companion inventory changed")
    return row["source_identity"]


def verify_command_inputs(command, engine, row):
    if "manifest" not in row or "manifest_sha256" not in row:
        raise ValueError("validation receipt must bind the input manifest")
    if engine == "fastsim":
        if command.count("--manifest") != 1:
            raise ValueError("missing or duplicated manifest argument")
        actual = command[command.index("--manifest") + 1]
    elif engine == "zsim":
        actual = command[1]
    else:
        values = [
            part.split("=", 1)[1]
            for part in command
            if part.lstrip("-").startswith("traceinput/fst_manifest=")
        ]
        if len(values) != 1:
            raise ValueError("missing or duplicated Sniper manifest")
        actual = values[0]
    manifest = pathlib.Path(row["manifest"])
    if pathlib.Path(actual).resolve() != manifest.resolve():
        raise ValueError("command selects a different validated source")
    if hashlib.sha256(manifest.read_bytes()).hexdigest() != row["manifest_sha256"]:
        raise ValueError("manifest changed since validation")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for name in ["receipt", "command", "log", "metrics", "cwd"]:
        p.add_argument("--" + name, type=pathlib.Path, required=True)
    p.add_argument("--case", required=True)
    p.add_argument("--engine", choices=["fastsim", "sniper", "zsim"], required=True)
    p.add_argument("--cores", type=int, required=True)
    p.add_argument(
        "--strict-pilot",
        action="store_true",
        help="validation-inclusive equivalence check only",
    )
    a = p.parse_args()
    proof = a.receipt.read_bytes()
    receipt = json.loads(proof)
    validation_start = time.monotonic()
    identity = verify_case(receipt, a.case)
    validation_before = time.monotonic() - validation_start
    env = dict(
        os.environ,
        TRACEBENCH_VALIDATED_INPUT="0" if a.strict_pilot else "1",
        TRACEBENCH_RECEIPT_SHA256=hashlib.sha256(proof).hexdigest(),
    )
    command = json.loads(a.command.read_text())
    verify_command_inputs(command, a.engine, receipt["cases"][a.case])
    with a.log.open("w") as log:
        start = time.monotonic_ns()
        completed = subprocess.run(
            command, cwd=a.cwd, env=env, stdout=log, stderr=subprocess.STDOUT
        )
        process_end = time.monotonic_ns()
    validation_start = time.monotonic()
    verify_case(receipt, a.case)
    verify_command_inputs(command, a.engine, receipt["cases"][a.case])
    validation_after = time.monotonic() - validation_start
    if completed.returncode:
        raise ValueError("engine failed; see " + str(a.log))
    ends = parse_endpoints(a.log.read_text(), a.engine, a.cores)
    seconds = elapsed_seconds(
        start, ends, process_end, a.cores if a.engine == "sniper" else 1
    )
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    metrics = dict(
        schema="tracebench-e2e-v1",
        policy=(
            "validation-inclusive-strict-pilot-v1"
            if a.strict_pilot
            else "preparation-included-extra-validation-excluded-v1"
        ),
        engine=a.engine,
        start_ns=start,
        end_ns=max(ends.values()),
        e2e_seconds=seconds,
        process_wall_seconds=(process_end - start) / 1e9,
        validation_before_seconds=validation_before,
        validation_after_seconds=validation_after,
        source_identity=identity,
        validation_receipt_sha256=hashlib.sha256(proof).hexdigest(),
        peak_rss_kib=usage.ru_maxrss,
        child_cpu_seconds=usage.ru_utime + usage.ru_stime,
        trusted_input=not a.strict_pilot,
        preprocessing_included=True,
        warmup_included=True,
        output_formatting_after_model_end_excluded=True,
    )
    a.metrics.write_text(json.dumps(metrics, indent=2))
    print(json.dumps(metrics), flush=True)


if __name__ == "__main__":
    main()
