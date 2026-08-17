#!/usr/bin/env python3
"""Validate and summarize TaoTrace's offline wrong-path attribution oracle.

The JSONL sidecar is deliberately oracle-only.  This tool never reads or
writes FST records and rejects rows that claim otherwise.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from pathlib import Path


SCHEMAS = {
    "taotrace-wrong-path-oracle-v1",
    "taotrace-wrong-path-oracle-v2",
    "taotrace-wrong-path-oracle-v3",
}
CPL_FIELDS = (
    "user_instruction_records",
    "kernel_instruction_records",
    "unknown_cpl_instruction_records",
)
STAGES = (
    "instruction_records",
    "fetched_instructions",
    "renamed_instructions",
    "dispatched_instructions",
    "issued_instructions",
    "completed_instructions",
    "memory_instructions",
    "data_completed_instructions",
    "load_instructions",
    "store_instructions",
)
FLAG_TO_STAGE = {
    "fetched": "fetched_instructions",
    "renamed": "renamed_instructions",
    "dispatched": "dispatched_instructions",
    "execute_seen": "issued_instructions",
    "to_commit_seen": "completed_instructions",
    "data_complete_seen": "data_completed_instructions",
}
STAT_PATTERNS = {
    "commit_squashed_instructions": re.compile(
        r"\.commit\.commitSquashedInsts\s+(\d+)"
    ),
    "issued_squashed_instructions": re.compile(
        r"\.squashedInstsIssued\s+(\d+)"
    ),
    "squashed_loads": re.compile(r"\.lsq\d*\.squashedLoads\s+(\d+)"),
    "squashed_stores": re.compile(r"\.lsq\d*\.squashedStores\s+(\d+)"),
    "branch_mispredicts": re.compile(r"\.commit\.branchMispredicts\s+(\d+)"),
}


def fail(path: Path, line: int, message: str) -> None:
    raise SystemExit(f"{path}:{line}: {message}")


def read_stats(path: Path | None) -> dict[str, int]:
    if path is None:
        return {}
    text = path.read_text(encoding="utf-8", errors="replace")
    return {
        name: sum(int(match.group(1)) for match in pattern.finditer(text))
        for name, pattern in STAT_PATTERNS.items()
    }


def percent(numerator: int, denominator: int) -> float:
    return 100.0 * numerator / denominator if denominator else 0.0


def validate(path: Path, stats_path: Path | None) -> dict:
    metadata = None
    episodes: dict[int, dict] = {}
    observed = defaultdict(Counter)
    sequence_keys = set()
    instruction_rows = {}
    late_sequence_keys = set()
    record_counts = Counter()

    with path.open(encoding="utf-8") as handle:
        for line_number, text in enumerate(handle, 1):
            if not text.strip():
                continue
            try:
                row = json.loads(text)
            except json.JSONDecodeError as exc:
                fail(path, line_number, f"invalid JSON: {exc}")
            if row.get("schema") not in SCHEMAS:
                fail(path, line_number, f"unexpected schema {row.get('schema')!r}")
            if metadata is not None and row.get("schema") != metadata.get("schema"):
                fail(path, line_number, "schema changes inside one sidecar")
            kind = row.get("record")
            record_counts[kind] += 1
            if kind == "metadata":
                if metadata is not None:
                    fail(path, line_number, "duplicate metadata row")
                if row.get("oracle_only") is not True or row.get("fst_input") is not False:
                    fail(path, line_number, "metadata does not enforce oracle/FST separation")
                if row.get("schema") in {
                    "taotrace-wrong-path-oracle-v2",
                    "taotrace-wrong-path-oracle-v3",
                } and row.get("cpl_attribution") != "decoded-x86-mode":
                    fail(path, line_number, "v2/v3 metadata lacks decoded CPL attribution")
                if row.get("schema") == "taotrace-wrong-path-oracle-v3" and row.get(
                    "late_data_complete_attribution"
                ) is not True:
                    fail(path, line_number, "v3 metadata lacks late-completion attribution")
                metadata = row
                continue
            if metadata is None:
                fail(path, line_number, "data row precedes metadata")
            if kind == "episode":
                episode_id = int(row.get("episode_id", 0))
                if episode_id <= 0 or episode_id in episodes:
                    fail(path, line_number, f"invalid/duplicate episode_id {episode_id}")
                if row.get("cause") not in {
                    "branch_mispredict",
                    "memory_order",
                    "unattributed_commit_squash",
                }:
                    fail(path, line_number, f"unknown cause {row.get('cause')!r}")
                for field in STAGES:
                    if int(row.get(field, -1)) < 0:
                        fail(path, line_number, f"missing/negative {field}")
                if row.get("schema") in {
                    "taotrace-wrong-path-oracle-v2",
                    "taotrace-wrong-path-oracle-v3",
                }:
                    cause_cpl = int(row.get("cause_cpl", -1))
                    if cause_cpl not in (0, 1, 2, 3, 255):
                        fail(path, line_number, f"invalid cause_cpl {cause_cpl}")
                    for field in CPL_FIELDS:
                        if int(row.get(field, -1)) < 0:
                            fail(path, line_number, f"missing/negative {field}")
                episodes[episode_id] = row
                continue
            if kind == "late_data_complete":
                if row.get("schema") != "taotrace-wrong-path-oracle-v3":
                    fail(path, line_number, "late completion requires schema v3")
                episode_id = int(row.get("episode_id", 0))
                episode = episodes.get(episode_id)
                if episode is None:
                    fail(path, line_number, f"late completion references unknown episode {episode_id}")
                core_id = int(row.get("core_id", -1))
                hardware_thread_id = int(row.get("hardware_thread_id", -1))
                seq_num = int(row.get("seq_num", 0))
                instruction_key = (episode_id, core_id, seq_num)
                instruction = instruction_rows.get(instruction_key)
                if instruction is None:
                    fail(path, line_number, "late completion lacks prior instruction row")
                if instruction_key in late_sequence_keys:
                    fail(path, line_number, "duplicate late completion")
                late_sequence_keys.add(instruction_key)
                if core_id != int(episode.get("core_id", -2)) or core_id != int(
                    instruction.get("core_id", -3)
                ):
                    fail(path, line_number, "late completion core mismatch")
                if hardware_thread_id != int(
                    episode.get("hardware_thread_id", -2)
                ) or hardware_thread_id != int(
                    instruction.get("hardware_thread_id", -3)
                ):
                    fail(path, line_number, "late completion hardware thread mismatch")
                if int(row.get("cpl", -1)) != int(instruction.get("cpl", -2)):
                    fail(path, line_number, "late completion CPL mismatch")
                if int(instruction.get("data_complete_seen", -1)) != 0:
                    fail(path, line_number, "late completion duplicates pre-squash completion")
                if not any(
                    int(instruction.get(field, 0))
                    for field in ("is_load", "is_store", "is_atomic")
                ):
                    fail(path, line_number, "late completion is not a memory instruction")
                if int(row.get("measurement_gate_open", -1)) not in (0, 1):
                    fail(path, line_number, "measurement_gate_open is not boolean")
                squash_tick = int(row.get("squash_tick", 0))
                completion_tick = int(row.get("data_complete_tick", 0))
                if squash_tick != int(episode.get("squash_tick", -1)):
                    fail(path, line_number, "late completion squash tick mismatch")
                if completion_tick < squash_tick:
                    fail(path, line_number, "late completion precedes squash")
                observed[episode_id]["late_data_completed_instructions"] += 1
                observed[episode_id][
                    "late_data_completed_inside_measurement"
                ] += int(row["measurement_gate_open"])
                continue
            if kind != "instruction":
                fail(path, line_number, f"unknown record kind {kind!r}")

            episode_id = int(row.get("episode_id", 0))
            episode = episodes.get(episode_id)
            if episode is None:
                fail(path, line_number, f"instruction references unknown episode {episode_id}")
            if row.get("cause") != episode.get("cause"):
                fail(path, line_number, "instruction/episode cause mismatch")
            if int(row.get("core_id", -1)) != int(episode.get("core_id", -2)):
                fail(path, line_number, "instruction/episode core mismatch")
            if int(row.get("hardware_thread_id", -1)) != int(
                episode.get("hardware_thread_id", -2)
            ):
                fail(path, line_number, "instruction/episode hardware thread mismatch")
            key = (int(row["core_id"]), int(row["seq_num"]))
            if key in sequence_keys:
                fail(path, line_number, f"duplicate dynamic instruction {key}")
            sequence_keys.add(key)
            instruction_rows[(episode_id, int(row["core_id"]), int(row["seq_num"]))] = row
            seq_num = int(row["seq_num"])
            cutoff_seq = int(episode["cutoff_seq"])
            if seq_num <= cutoff_seq:
                fail(path, line_number, "instruction is not younger than cutoff")

            if row.get("schema") in {
                "taotrace-wrong-path-oracle-v2",
                "taotrace-wrong-path-oracle-v3",
            }:
                cpl = int(row.get("cpl", -1))
                if cpl not in (0, 1, 2, 3, 255):
                    fail(path, line_number, f"invalid cpl {cpl}")
                if cpl == 3:
                    observed[episode_id]["user_instruction_records"] += 1
                elif cpl <= 2:
                    observed[episode_id]["kernel_instruction_records"] += 1
                else:
                    observed[episode_id]["unknown_cpl_instruction_records"] += 1

            for flag, stage in FLAG_TO_STAGE.items():
                value = int(row.get(flag, -1))
                if value not in (0, 1):
                    fail(path, line_number, f"{flag} is not boolean")
                observed[episode_id][stage] += value
            is_load = int(row.get("is_load", 0))
            is_store = int(row.get("is_store", 0))
            is_atomic = int(row.get("is_atomic", 0))
            observed[episode_id]["load_instructions"] += is_load
            observed[episode_id]["store_instructions"] += is_store
            observed[episode_id]["memory_instructions"] += int(
                bool(is_load or is_store or is_atomic)
            )
            observed[episode_id]["instruction_records"] += 1

            ticks = [
                int(row.get(name, 0))
                for name in (
                    "fetch_tick",
                    "rename_tick",
                    "dispatch_tick",
                    "execute_probe_tick",
                    "to_commit_probe_tick",
                    "squash_tick",
                )
                if int(row.get(name, 0)) > 0
            ]
            if ticks != sorted(ticks):
                fail(path, line_number, "per-instruction probe ticks are not monotonic")

    if metadata is None:
        raise SystemExit(f"{path}: missing metadata")

    by_cause = defaultdict(Counter)
    for episode_id, episode in episodes.items():
        expected = Counter({field: int(episode[field]) for field in STAGES})
        actual = observed[episode_id]
        for field in STAGES:
            if actual[field] != expected[field]:
                raise SystemExit(
                    f"{path}: episode {episode_id} {field} expected "
                    f"{expected[field]} observed {actual[field]}"
                )
        if metadata["schema"] in {
            "taotrace-wrong-path-oracle-v2",
            "taotrace-wrong-path-oracle-v3",
        }:
            for field in CPL_FIELDS:
                expected_value = int(episode[field])
                if actual[field] != expected_value:
                    raise SystemExit(
                        f"{path}: episode {episode_id} {field} expected "
                        f"{expected_value} observed {actual[field]}"
                    )
            if sum(int(episode[field]) for field in CPL_FIELDS) != int(
                episode["instruction_records"]
            ):
                raise SystemExit(
                    f"{path}: episode {episode_id} CPL record counts do not "
                    "conserve instruction_records"
                )
        cause = str(episode["cause"])
        by_cause[cause]["episodes"] += 1
        for field in STAGES:
            by_cause[cause][field] += expected[field]
        if metadata["schema"] in {
            "taotrace-wrong-path-oracle-v2",
            "taotrace-wrong-path-oracle-v3",
        }:
            for field in CPL_FIELDS:
                by_cause[cause][field] += int(episode[field])
        for field in (
            "late_data_completed_instructions",
            "late_data_completed_inside_measurement",
        ):
            by_cause[cause][field] += int(actual[field])

    aggregate = Counter()
    for counters in by_cause.values():
        aggregate.update(counters)
    result = {
        "schema": "fastsim-wrong-path-oracle-validation-v3",
        "source": str(path.resolve()),
        "valid": True,
        "metadata": metadata,
        "records": dict(record_counts),
        "aggregate": dict(aggregate),
        "by_cause": {cause: dict(values) for cause, values in sorted(by_cause.items())},
        "rates_percent": {
            "rename_per_fetch": percent(
                aggregate["renamed_instructions"], aggregate["fetched_instructions"]
            ),
            "dispatch_per_fetch": percent(
                aggregate["dispatched_instructions"], aggregate["fetched_instructions"]
            ),
            "issue_per_fetch": percent(
                aggregate["issued_instructions"], aggregate["fetched_instructions"]
            ),
            "data_complete_per_memory": percent(
                aggregate["data_completed_instructions"],
                aggregate["memory_instructions"],
            ),
            "late_data_complete_per_memory": percent(
                aggregate["late_data_completed_instructions"],
                aggregate["memory_instructions"],
            ),
        },
        "gem5_stats": read_stats(stats_path),
    }
    return result


def write_markdown(result: dict, path: Path) -> None:
    aggregate = result["aggregate"]
    rates = result["rates_percent"]
    lines = [
        "# Wrong-path oracle validation",
        "",
        f"Valid: **{result['valid']}**",
        "",
        "| Cause | Episodes | Fetch | Rename | Dispatch | Issue | ToCommit | Memory | Data complete before squash | Data complete after squash |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for cause, row in result["by_cause"].items():
        lines.append(
            f"| {cause} | {row.get('episodes', 0):,} | "
            f"{row.get('fetched_instructions', 0):,} | "
            f"{row.get('renamed_instructions', 0):,} | "
            f"{row.get('dispatched_instructions', 0):,} | "
            f"{row.get('issued_instructions', 0):,} | "
            f"{row.get('completed_instructions', 0):,} | "
            f"{row.get('memory_instructions', 0):,} | "
            f"{row.get('data_completed_instructions', 0):,} | "
            f"{row.get('late_data_completed_instructions', 0):,} |"
        )
    lines.extend(
        [
            "",
            f"Aggregate episodes: {aggregate.get('episodes', 0):,}.",
            f"Rename/fetch: {rates['rename_per_fetch']:.3f}%.",
            f"Dispatch/fetch: {rates['dispatch_per_fetch']:.3f}%.",
            f"Issue/fetch: {rates['issue_per_fetch']:.3f}%.",
            f"Data-complete/memory: {rates['data_complete_per_memory']:.3f}%.",
            f"Late-data-complete/memory: {rates['late_data_complete_per_memory']:.3f}%.",
            "",
            "The sidecar is oracle-only and must not be supplied to FastSim inference.",
        ]
    )
    if result["gem5_stats"]:
        lines.extend(["", "## gem5 aggregate diagnostics", ""])
        for name, value in result["gem5_stats"].items():
            lines.append(f"- {name}: {value:,}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("sidecar", type=Path)
    parser.add_argument("--gem5-stats", type=Path)
    parser.add_argument("--json-out", type=Path)
    parser.add_argument("--markdown-out", type=Path)
    args = parser.parse_args()

    result = validate(args.sidecar, args.gem5_stats)
    encoded = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(encoded, encoding="utf-8")
    else:
        print(encoded, end="")
    if args.markdown_out:
        args.markdown_out.parent.mkdir(parents=True, exist_ok=True)
        write_markdown(result, args.markdown_out)


if __name__ == "__main__":
    main()
