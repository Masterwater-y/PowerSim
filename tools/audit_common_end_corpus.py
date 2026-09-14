#!/usr/bin/env python3
"""Publish a replacement corpus index from its own completed common-end oracles.

Only lightweight metadata/header checks are repeated here. Full FST hashes
are already generated during promotion; no extra inference or trace scan.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import struct
import time

from collect_common_end_fst import save
from common_end_capture import POLICY, validate_common_end


def publish(root):
    inventory = json.loads((root / "inventory.json").read_text())
    deletion = json.loads((root / "deletion-complete.json").read_text())
    if not deletion["all_removed"] or deletion["fst_files"] != 600:
        raise ValueError("old corpus deletion was not completed")
    if any(Path(r["path"]).exists() for r in inventory["deletion"]):
        raise ValueError("old FST/companion was recreated")
    rows, failed, pending = [], [], []
    for case in inventory["cases"]:
        result = Path(case["result_dir"])
        status = json.loads((result / "status.json").read_text())
        if status["phase"] != "complete":
            group = failed if status["phase"] == "failed" else pending
            group.append(dict(case=case["case"], phase=status["phase"], error=status.get("error")))
            continue
        trace = json.loads((result / "tao_trace/trace.json").read_text())
        cpi = json.loads((result / "oracle/cpi.json").read_text())
        cpl = [json.loads(line) for line in (result / "oracle/cpl_class.jsonl").read_text().splitlines()
               if line.strip()]
        gate = validate_common_end(list(trace["functional_boundaries"].values()), cpi["per_core"], cpl,
                                   cores=case["cores"], target=10000000)
        manifest = result / "tao_trace/manifest.txt"
        manifest_rows = [line.split() for line in manifest.read_text().splitlines()
                         if line.strip() and not line.startswith("#")]
        if len(manifest_rows) != case["cores"]:
            raise ValueError(f"{case['case']}: wrong manifest participant count")
        total_bytes = total_records = 0
        for core, line in enumerate(manifest_rows):
            boundary = trace["functional_boundaries"][str(core)]
            if len(line) != 8 or line[1] != "fastsim-binary-warmup-slice" or (
                    int(line[0]) != core or int(line[3]) != core):
                raise ValueError(f"{case['case']}: wrong manifest row")
            if list(map(int, line[4:])) != [boundary[k] for k in (
                    "warmup_instructions", "measurement_instructions", "warmup_records", "measurement_records")]:
                raise ValueError(f"{case['case']}: manifest changed the actual population")
            fst = manifest.parent / line[2]
            with fst.open("rb") as source:
                header = struct.unpack("<8sIIIIQQ4Q", source.read(72))
            if header[4] != core or header[5] != boundary["total_records"]:
                raise ValueError(f"{case['case']}: physical FST population mismatch")
            total_records += header[5]
            total_bytes += sum(path.stat().st_size for path in [fst] + [Path(str(fst) + suffix)
                               for suffix in (".deps", ".asmap", ".vmap", ".imap")] if path.exists())
        oracle = json.loads((result / "oracle/kernel_events.json").read_text())["aggregate"]
        case.update(reference=oracle["perf_like_cpi_user_plus_kernel"], manifest=str(manifest),
                    original_manifest=str(manifest), reference_source=str(result / "oracle/kernel_events.json"),
                    measurement_policy=POLICY, collection_status="complete")
        rows.append(dict(case=case["case"], cores=case["cores"], user_uops=gate["user_uops"],
                         per_core_user_uops=gate["per_core_user_uops"], trigger_core=gate["trigger_core"],
                         common_end_tick=gate["common_end_tick"], total_records=total_records,
                         fst_and_companion_bytes=total_bytes,
                         gem5_cpi=case["reference"], manifest=str(manifest)))
    save(root / "inventory.json", inventory)
    report = dict(measurement_policy=POLICY, expected_cases=len(inventory["cases"]),
                  completed_cases=len(rows), failed_cases=failed, pending_cases=pending, cases=rows,
                  fst_files=sum(row["cores"] for row in rows),
                  user_uops=sum(row["user_uops"] for row in rows),
                  fst_and_companion_bytes=sum(row["fst_and_companion_bytes"] for row in rows),
                  validated_at=time.time(), valid=len(rows) == 40 and not failed and not pending)
    save(root / "corpus-audit.json", report)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--watch", action="store_true")
    args = parser.parse_args()
    if args.watch:
        while not (args.root / "collection-finished.json").exists():
            time.sleep(15)
    report = publish(args.root)
    print(json.dumps({key: report[key] for key in ("completed_cases", "fst_files", "failed_cases", "valid")}))
    raise SystemExit(0 if report["valid"] else 1)
