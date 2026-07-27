#!/usr/bin/env python3
"""Compare serial functional replay with a current gem5 Branch debug log.

The script filters speculative/wrong-path predictions by committed sequence
number, then feeds only committed architectural outcomes to the standalone
replay.  Exact conditional-direction agreement is required.  Full-BPU
differences are reported rather than hidden because wrong-path BTB pollution
and exact call fallthrough are not present in functional trace.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import Any

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from tcsim.branch_replay import BranchEvent, ReplayConfig, TournamentBPUReplay  # noqa: E402
from tcsim.utils.io import dump_json  # noqa: E402


COMMIT_RE = re.compile(
    r"Commit branch: sn:(\d+), PC:(0x[0-9a-fA-F]+) (\w+), "
    r"pred:(\d), taken:(\d), target:(0x[0-9a-fA-F]+)"
)
DIRECTION_RE = re.compile(
    r"sn:(\d+)\] Branch predictor predicted (\d) for PC:"
    r"(0x[0-9a-fA-F]+) (\w+)"
)
PREDICT_RE = re.compile(
    r"predict\(tid:\d+, sn:(\d+), PC:(0x[0-9a-fA-F]+), (\w+)\) "
    r"-> taken:(\d), target:\(?(0x[0-9a-fA-F]+).* provider:(\w+)"
)


TYPE_FLAGS = {
    "DirectCond": (True, False, False, False),
    "DirectUncond": (False, False, False, False),
    "CallDirect": (False, False, True, False),
    "CallIndirect": (False, True, True, False),
    "IndirectCond": (True, True, False, False),
    "IndirectUncond": (False, True, False, False),
    "Return": (False, True, False, True),
}


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--branch-log", required=True)
    parser.add_argument("--config-json", required=True)
    parser.add_argument("--output")
    parser.add_argument("--gem5-revision", default="unknown")
    return parser.parse_args()


def _parse(path: str) -> tuple[list[dict[str, Any]], dict[int, Any], dict[int, Any]]:
    commits = []
    directions: dict[int, Any] = {}
    predictions: dict[int, Any] = {}
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            match = DIRECTION_RE.search(line)
            if match:
                seq, predicted, pc, branch_type = match.groups()
                directions[int(seq)] = {
                    "predicted": bool(int(predicted)),
                    "pc": int(pc, 16),
                    "branch_type": branch_type,
                }
            match = PREDICT_RE.search(line)
            if match:
                seq, pc, branch_type, taken, target, provider = match.groups()
                predictions[int(seq)] = {
                    "pc": int(pc, 16),
                    "branch_type": branch_type,
                    "predicted_taken": bool(int(taken)),
                    "predicted_target": int(target, 16),
                    "provider": provider,
                }
            match = COMMIT_RE.search(line)
            if match:
                seq, pc, branch_type, predicted, taken, target = match.groups()
                commits.append({
                    "seq": int(seq),
                    "pc": int(pc, 16),
                    "branch_type": branch_type,
                    "commit_predicted_taken": bool(int(predicted)),
                    "actual_taken": bool(int(taken)),
                    "actual_target": int(target, 16),
                })
    return commits, directions, predictions


def _event(commit: dict[str, Any]) -> BranchEvent:
    branch_type = str(commit["branch_type"])
    if branch_type not in TYPE_FLAGS:
        raise RuntimeError(f"unknown gem5 BranchType {branch_type!r}")
    conditional, indirect, call, return_ = TYPE_FLAGS[branch_type]
    taken = bool(commit["actual_taken"])
    successor = int(commit["actual_target"])
    return BranchEvent(
        pc=int(commit["pc"]),
        taken=taken,
        target=successor if taken else 0,
        next_pc=successor,
        conditional=conditional,
        indirect=indirect,
        call=call,
        return_=return_,
    )


def main() -> int:
    args = _arguments()
    with open(args.config_json, "r", encoding="utf-8") as handle:
        config = ReplayConfig.from_mapping(json.load(handle))
    commits, directions, gem5_predictions = _parse(args.branch_log)
    if not commits:
        raise RuntimeError("gem5 Branch log contains no committed branches")
    replay = TournamentBPUReplay(config)
    comparisons = []
    conditional_checks = 0
    conditional_matches = 0
    final_direction_matches = 0
    provider_matches = 0
    full_miss_matches = 0
    for commit in commits:
        seq = int(commit["seq"])
        gem5_prediction = gem5_predictions.get(seq)
        gem5_direction = directions.get(seq)
        if gem5_prediction is None or gem5_direction is None:
            raise RuntimeError(f"missing gem5 prediction record for committed sn={seq}")
        prediction = replay.process(_event(commit))
        conditional = bool(TYPE_FLAGS[str(commit["branch_type"])][0])
        conditional_match = None
        if conditional:
            conditional_checks += 1
            conditional_match = (
                prediction.conditional_prediction
                == bool(gem5_direction["predicted"])
            )
            conditional_matches += int(conditional_match)
        final_direction_match = (
            prediction.predicted_taken
            == bool(gem5_prediction["predicted_taken"])
        )
        provider_match = prediction.target_provider == gem5_prediction["provider"]
        gem5_full_miss = bool(
            bool(gem5_prediction["predicted_taken"])
            != bool(commit["actual_taken"])
            or (
                bool(gem5_prediction["predicted_taken"])
                and bool(commit["actual_taken"])
                and int(gem5_prediction["predicted_target"])
                != int(commit["actual_target"])
            )
        )
        full_miss_match = prediction.full_miss == gem5_full_miss
        final_direction_matches += int(final_direction_match)
        provider_matches += int(provider_match)
        full_miss_matches += int(full_miss_match)
        comparisons.append({
            "gem5_seq": seq,
            "pc": f"0x{int(commit['pc']):x}",
            "branch_type": commit["branch_type"],
            "conditional_direction_match": conditional_match,
            "final_direction_match": final_direction_match,
            "provider_match": provider_match,
            "full_miss_match": full_miss_match,
            "gem5": {
                **gem5_prediction,
                "full_miss": gem5_full_miss,
                "actual_taken": bool(commit["actual_taken"]),
                "actual_target": int(commit["actual_target"]),
            },
            "replay": {
                "conditional_prediction": prediction.conditional_prediction,
                "predicted_taken": prediction.predicted_taken,
                "predicted_target": prediction.predicted_target,
                "provider": prediction.target_provider,
                "full_miss": prediction.full_miss,
            },
        })

    count = len(commits)
    report = {
        "status": "pass" if conditional_matches == conditional_checks else "fail",
        "gem5_revision": args.gem5_revision,
        "config_hash": config.stable_hash(),
        "committed_branches": count,
        "conditional_direction": {
            "checks": conditional_checks,
            "matches": conditional_matches,
            "agreement": conditional_matches / max(1, conditional_checks),
            "required_exact": True,
        },
        "full_serial_replay": {
            "final_direction_matches": final_direction_matches,
            "final_direction_agreement": final_direction_matches / count,
            "provider_matches": provider_matches,
            "provider_agreement": provider_matches / count,
            "full_miss_matches": full_miss_matches,
            "full_miss_agreement": full_miss_matches / count,
        },
        "known_non_exact_inputs": [
            "gem5 wrong-path branches can update BTB at squash",
            "functional trace has no exact taken-call fallthrough for first RAS use",
            "functional replay serializes branch resolution",
        ],
        "comparisons": comparisons,
    }
    if args.output:
        dump_json(args.output, report)
    else:
        print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
