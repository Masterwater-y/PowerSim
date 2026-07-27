"""Functional trace adapters for the standalone replay.

Only predictor-independent columns are requested from parquet.  In particular,
``mispredicted``, timing fields and any gem5 predictor debug state are never
read by this module.
"""
from __future__ import annotations

import os
from typing import Any, Iterable, Iterator, Mapping, Sequence

from .replay import BranchEvent, TournamentBPUReplay
from .config import ReplayConfig


REPLAY_ALIGNED_COLUMNS = (
    "core_id",
    "thread_id",
    "macro_pc",
    "micro_pc",
    "is_branch",
    "is_branch_cond",
    "is_branch_indirect",
    "is_call",
    "is_return",
    "branch_taken",
    "branch_target",
    "branch_next_pc",
    "branch_history",
)
REPLAY_REQUIRED_COLUMNS = frozenset(REPLAY_ALIGNED_COLUMNS) - {"thread_id"}
REPLAY_CACHE_ARRAY_NAMES = (
    "replay_branch_index",
    "replay_branch_target",
    "replay_branch_next_pc",
    "replay_branch_history",
    "replay_branch_thread_id",
)
REPLAY_CACHE_CONTRACT = "functional-branch-replay-v1"


def event_from_mapping(row: Mapping[str, Any]) -> BranchEvent:
    if not bool(int(row.get("is_branch", 0) or 0)):
        raise ValueError("cannot construct BranchEvent from a non-branch row")
    pc = int(row.get("macro_pc", row.get("micro_pc", 0)) or 0)
    return BranchEvent(
        pc=pc,
        taken=bool(int(row.get("branch_taken", 0) or 0)),
        target=int(row.get("branch_target", 0) or 0),
        next_pc=int(row.get("branch_next_pc", 0) or 0),
        conditional=bool(int(row.get("is_branch_cond", 0) or 0)),
        indirect=bool(int(row.get("is_branch_indirect", 0) or 0)),
        call=bool(int(row.get("is_call", 0) or 0)),
        return_=bool(int(row.get("is_return", 0) or 0)),
        thread_id=int(row.get("thread_id", 0) or 0),
        branch_history=int(row.get("branch_history", 0) or 0),
    )


def iter_aligned_events(path: str) -> Iterator[BranchEvent]:
    try:
        import pyarrow.parquet as pq  # type: ignore
    except Exception as exc:  # pragma: no cover - environment dependent
        raise RuntimeError("pyarrow is required to replay aligned parquet") from exc
    parquet = pq.ParquetFile(path)
    available = set(parquet.schema_arrow.names)
    missing = sorted(REPLAY_REQUIRED_COLUMNS - available)
    if missing:
        raise RuntimeError(
            f"functional replay parquet is missing {missing}: {path}"
        )
    columns = [name for name in REPLAY_ALIGNED_COLUMNS if name in available]
    forbidden = {"mispredicted", "commit_tick", "fetch_tick"}
    if forbidden.intersection(columns):  # defensive invariant
        raise AssertionError("oracle/timing column entered functional replay reader")
    local_threads: dict[int, int] = {}
    for batch in parquet.iter_batches(batch_size=65536, columns=columns):
        for row in batch.to_pylist():
            if bool(int(row.get("is_branch", 0) or 0)):
                raw_thread = int(row.get("thread_id", 0) or 0)
                if raw_thread not in local_threads:
                    local_threads[raw_thread] = len(local_threads)
                row["thread_id"] = local_threads[raw_thread]
                yield event_from_mapping(row)


def events_from_cache_arrays(arrays: Mapping[str, Any]) -> Iterator[BranchEvent]:
    """Read the compact replay arrays emitted by the v29 cache builder."""
    missing = [name for name in REPLAY_CACHE_ARRAY_NAMES if name not in arrays]
    if missing:
        raise RuntimeError(
            "v29 cache predates exact functional branch replay arrays: "
            + ", ".join(missing)
        )
    fields = arrays["fields"]
    macro_pc = arrays["macro_pc"]
    indices = arrays["replay_branch_index"]
    targets = arrays["replay_branch_target"]
    successors = arrays["replay_branch_next_pc"]
    histories = arrays["replay_branch_history"]
    threads = arrays["replay_branch_thread_id"]
    from tcsim.v29.contracts import FIELD_INDEX

    lengths = {len(indices), len(targets), len(successors), len(histories), len(threads)}
    if len(lengths) != 1:
        raise RuntimeError("v29 compact branch replay arrays have inconsistent lengths")
    for position, raw_index in enumerate(indices):
        index = int(raw_index)
        kind = int(fields[index, FIELD_INDEX["branch_kind"]])
        taken = int(fields[index, FIELD_INDEX["branch_taken"]]) == 2
        yield BranchEvent(
            pc=int(macro_pc[index]),
            taken=taken,
            target=int(targets[position]),
            next_pc=int(successors[position]),
            conditional=bool(kind & 0x2),
            indirect=bool(kind & 0x4),
            call=bool(kind & 0x8),
            return_=bool(kind & 0x10),
            thread_id=int(threads[position]),
            branch_history=int(histories[position]),
        )


def _rate(count: int, opportunities: int) -> float:
    return float(count) / opportunities if opportunities else float("nan")


def aggregate_core_reports(reports: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    count_keys = (
        "branches",
        "conditional_branches",
        "conditional_direction_misses",
        "final_direction_misses",
        "target_misses",
        "target_unavailable_misses",
        "target_side_misses",
        "full_misses",
        "btb_lookups",
        "btb_hits",
        "indirect_lookups",
        "indirect_hits",
        "ras_target_unknown",
        "mispredict_due_to_btb_miss",
        "functional_history_checks",
        "functional_history_mismatches",
    )
    combined = {key: sum(int(report.get(key, 0)) for report in reports) for key in count_keys}
    providers: dict[str, int] = {}
    by_type: dict[str, dict[str, int]] = {}
    for report in reports:
        for name, value in dict(report.get("target_providers", {})).items():
            providers[str(name)] = providers.get(str(name), 0) + int(value)
        for name, values in dict(report.get("by_type", {})).items():
            item = by_type.setdefault(str(name), {"branches": 0, "misses": 0})
            item["branches"] += int(values.get("branches", 0))
            item["misses"] += int(values.get("misses", 0))
    combined.update({
        "conditional_direction_miss_rate": _rate(
            combined["conditional_direction_misses"], combined["conditional_branches"]
        ),
        "full_miss_rate": _rate(combined["full_misses"], combined["branches"]),
        "btb_hit_rate": _rate(combined["btb_hits"], combined["btb_lookups"]),
        "indirect_hit_rate": _rate(
            combined["indirect_hits"], combined["indirect_lookups"]
        ),
        "target_providers": providers,
        "by_type": {
            name: {
                **values,
                "miss_rate": _rate(values["misses"], values["branches"]),
            }
            for name, values in sorted(by_type.items())
        },
    })
    return combined


def replay_core_streams(
    streams: Iterable[tuple[int, Iterable[BranchEvent]]],
    config: ReplayConfig,
) -> dict[str, Any]:
    """Replay each physical core with an independent predictor instance."""
    per_core = []
    for core_id, events in streams:
        replay = TournamentBPUReplay(config)
        report = replay.run(events)
        per_core.append({"core_id": int(core_id), **report})
    return {
        "name": "standalone_tournament_full_bpu_replay",
        "config_hash": config.stable_hash(),
        "config": config.to_dict(),
        **aggregate_core_reports(per_core),
        "per_core": per_core,
        "oracle_labels_consumed_as_input": False,
        "functional_replay_contract": REPLAY_CACHE_CONTRACT,
    }


def attach_v29_meta_evaluation(
    report: dict[str, Any], meta: Mapping[str, Any]
) -> dict[str, Any]:
    """Join aggregate gem5 labels after replay for count/rate evaluation."""
    by_core = {int(item["core_id"]): item for item in report.get("per_core", [])}
    true_misses = 0
    true_branches = 0
    for core_meta in meta.get("cores", []):
        core_id = int(core_meta["core_id"])
        if core_id not in by_core:
            raise RuntimeError(f"evaluation meta has unknown replay core {core_id}")
        item = by_core[core_id]
        branches = int(core_meta["n_branches"])
        misses = int(core_meta["n_branch_misses"])
        if int(item["branches"]) != branches:
            raise RuntimeError(
                f"branch count mismatch core={core_id}: "
                f"replay={item['branches']} labels={branches}"
            )
        true_rate = _rate(misses, branches)
        item.update({
            "true_misses": misses,
            "true_rate": true_rate,
            "miss_count_abs_error": abs(int(item["full_misses"]) - misses),
            "miss_count_abs_relative_error": (
                abs(int(item["full_misses"]) - misses) / max(1, misses)
            ),
            "miss_rate_abs_error_pp": abs(
                float(item["full_miss_rate"]) - true_rate
            ) * 100.0,
        })
        true_misses += misses
        true_branches += branches
    if int(report.get("branches", -1)) != true_branches:
        raise RuntimeError("aggregate branch count does not match evaluation meta")
    true_rate = _rate(true_misses, true_branches)
    predicted_misses = int(report["full_misses"])
    report.update({
        "true_misses": true_misses,
        "true_rate": true_rate,
        "predicted_misses": predicted_misses,
        "predicted_rate": float(report["full_miss_rate"]),
        "miss_count_abs_error": abs(predicted_misses - true_misses),
        "miss_count_abs_relative_error": (
            abs(predicted_misses - true_misses) / max(1, true_misses)
        ),
        "miss_rate_abs_error_pp": abs(
            float(report["full_miss_rate"]) - true_rate
        ) * 100.0,
        "oracle_labels_consumed_as_input": False,
        "oracle_labels_used_post_replay_for_evaluation": True,
    })
    return report


def discover_aligned_files(trace_dir: str) -> list[tuple[int, str]]:
    import glob
    import re

    paths = sorted(glob.glob(os.path.join(trace_dir, "*.aligned.parquet")))
    if not paths:
        raise FileNotFoundError(f"no aligned parquet under {trace_dir}")
    core_re = re.compile(r"(?:cores|switch)(\d*)\.core")
    out = []
    for path in paths:
        match = core_re.search(os.path.basename(path))
        out.append((int(match.group(1) or "0") if match else 0, path))
    return sorted(out)
