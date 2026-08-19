#!/usr/bin/env python3
"""Validate the final-config-derived FS effective-target identity.

The final gem5 ``config.ini`` is authoritative. ``effective-target.json`` and
``tao_trace/uarch_profile.json`` must both name its SHA-256; request.json and
wrapper defaults are deliberately excluded from the P0 identity decision.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path


SCHEMA = "fastsim-fs-oracle-identity-validation-v2"
TARGET_SCHEMA = "fastsim-gem5-effective-target-v1"
PMU_CONTRACT_ID = "perf-gem5-fastsim-x86-fs-v1"
EVENT_DICTIONARY = (
    Path(__file__).resolve().parents[1]
    / "configs"
    / "pmu-event-dictionary-v1.json"
)


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def size_bytes(value: str | int) -> int:
    if isinstance(value, bool):
        raise ValueError(f"invalid byte size {value!r}")
    if isinstance(value, int):
        return value
    text = str(value).strip()
    units = {
        "KiB": 1024,
        "MiB": 1024**2,
        "GiB": 1024**3,
        "B": 1,
    }
    for suffix, multiplier in units.items():
        if text.endswith(suffix):
            return int(float(text[: -len(suffix)]) * multiplier)
    return int(text)


def frequency_ghz(value: str | int | float) -> float:
    if isinstance(value, bool):
        raise ValueError(f"invalid clock frequency {value!r}")
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    for suffix, divisor in (("GHz", 1.0), ("MHz", 1000.0)):
        if text.endswith(suffix):
            return float(text[: -len(suffix)]) / divisor
    return float(text)


def nested(document: dict, path: tuple[str, ...], source: Path):
    value = document
    for key in path:
        if not isinstance(value, dict) or key not in value:
            dotted = ".".join(path)
            raise ValueError(f"{source} lacks required field {dotted}")
        value = value[key]
    return value


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def semantic_json_sha256(value: dict) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_legacy_request_identity(result_dir: Path) -> dict:
    result_dir = result_dir.resolve()
    request_path = result_dir / "request.json"
    profile_path = result_dir / "tao_trace" / "uarch_profile.json"
    request = read_json(request_path)
    profile = read_json(profile_path)
    target = nested(request, ("boot_profile", "profile"), request_path)
    cache = nested(target, ("cache",), request_path)
    memory = nested(target, ("memory",), request_path)

    expected = {
        "core.freq_ghz": frequency_ghz(nested(target, ("clk",), request_path)),
        "core.num_cores": int(nested(target, ("num_cores",), request_path)),
        "cache.l1d.size_b": size_bytes(nested(cache, ("l1d_size",), request_path)),
        "cache.l1d.assoc": int(nested(cache, ("l1d_assoc",), request_path)),
        "cache.l1i.size_b": size_bytes(nested(cache, ("l1i_size",), request_path)),
        "cache.l1i.assoc": int(nested(cache, ("l1i_assoc",), request_path)),
        "cache.l2.size_b": size_bytes(nested(cache, ("l2_size",), request_path)),
        "cache.l2.assoc": int(nested(cache, ("l2_assoc",), request_path)),
        # uarch_profile schema v2 defines L3 size_b as total capacity, while
        # request.json records the stdlib Ruby capacity of each bank.
        "cache.l3.size_b": size_bytes(
            nested(cache, ("l3_size_per_bank",), request_path)
        )
        * int(nested(cache, ("num_l3_banks",), request_path)),
        "cache.l3.assoc": int(nested(cache, ("l3_assoc",), request_path)),
        "cache.l3.num_banks": int(
            nested(cache, ("num_l3_banks",), request_path)
        ),
        "coherence.protocol": str(nested(cache, ("protocol",), request_path)),
        "dram.size_b": size_bytes(nested(target, ("memory_size",), request_path)),
        "dram.num_channels": int(nested(memory, ("channels",), request_path)),
        "dram.interleaving_size_b": int(
            nested(memory, ("interleaving_size",), request_path)
        ),
    }
    actual = {
        key: nested(profile, tuple(key.split(".")), profile_path)
        for key in expected
    }
    mismatches = []
    for field, expected_value in expected.items():
        actual_value = actual[field]
        equal = actual_value == expected_value
        if isinstance(expected_value, float):
            try:
                equal = math.isclose(
                    float(actual_value), expected_value, rel_tol=0.0, abs_tol=1e-12
                )
            except (TypeError, ValueError):
                equal = False
        if not equal:
            mismatches.append(
                {
                    "field": field,
                    "profile": actual_value,
                    "target": expected_value,
                }
            )
    return {
        "schema": SCHEMA,
        "result_dir": str(result_dir),
        "request": str(request_path),
        "uarch_profile": str(profile_path),
        "valid": not mismatches,
        "mismatches": mismatches,
        "identity_source": "legacy-request-json",
    }


def validate_result_identity(
    result_dir: Path,
    allow_legacy_request_identity: bool = False,
    event_dictionary: Path = EVENT_DICTIONARY,
) -> dict:
    result_dir = result_dir.resolve()
    config_path = result_dir / "config.ini"
    profile_path = result_dir / "tao_trace" / "uarch_profile.json"
    target_path = result_dir / "effective-target.json"
    if not target_path.is_file():
        if allow_legacy_request_identity:
            return validate_legacy_request_identity(result_dir)
        return {
            "schema": SCHEMA,
            "result_dir": str(result_dir),
            "config_ini": str(config_path),
            "effective_target": str(target_path),
            "uarch_profile": str(profile_path),
            "identity_source": "final-config-ini",
            "valid": False,
            "mismatches": [
                {
                    "field": "effective-target.json",
                    "manifest": None,
                    "target": "required",
                }
            ],
        }
    target = read_json(target_path)
    profile = read_json(profile_path)
    mismatches = []

    def compare(field: str, actual, expected) -> None:
        equal = actual == expected
        if isinstance(expected, float):
            try:
                equal = math.isclose(
                    float(actual), expected, rel_tol=0.0, abs_tol=1e-12
                )
            except (TypeError, ValueError):
                equal = False
        if not equal:
            mismatches.append(
                {"field": field, "manifest": actual, "target": expected}
            )

    if target.get("schema") != TARGET_SCHEMA:
        compare("schema", target.get("schema"), TARGET_SCHEMA)
    if not config_path.is_file():
        compare("config.ini", None, "required")
        config_hash = None
    else:
        config_hash = sha256(config_path)
        compare(
            "source.config_ini_sha256",
            nested(target, ("source", "config_ini_sha256"), target_path),
            config_hash,
        )
        compare(
            "profile.source_config_sha256",
            profile.get("source_config_sha256"),
            config_hash,
        )
    compare(
        "source.pmu_contract_id",
        nested(target, ("source", "pmu_contract_id"), target_path),
        PMU_CONTRACT_ID,
    )
    event_dictionary = event_dictionary.resolve()
    dictionary = read_json(event_dictionary)
    compare(
        "event_dictionary.contract_id",
        dictionary.get("contract_id"),
        PMU_CONTRACT_ID,
    )
    compare(
        "source.event_dictionary_sha256",
        nested(target, ("source", "event_dictionary_sha256"), target_path),
        sha256(event_dictionary),
    )
    oracle_model = nested(
        target, ("source", "taotrace_oracle_model"), target_path
    )
    for path_field, hash_field in (
        ("uarch_profile_hh", "uarch_profile_hh_sha256"),
        ("cache_model_hh", "cache_model_hh_sha256"),
    ):
        source_path = Path(
            nested(oracle_model, (path_field,), target_path)
        ).resolve()
        recorded_hash = nested(oracle_model, (hash_field,), target_path)
        compare(
            f"source.taotrace_oracle_model.{path_field}.exists",
            source_path.is_file(),
            True,
        )
        if source_path.is_file():
            compare(
                f"source.taotrace_oracle_model.{hash_field}",
                recorded_hash,
                sha256(source_path),
            )
    compare(
        "source.uarch_profile_semantic_sha256",
        nested(
            target,
            ("source", "uarch_profile_semantic_sha256"),
            target_path,
        ),
        semantic_json_sha256(profile),
    )
    compare(
        "runtime_support.taotrace_cache_replacement_supported",
        nested(
            target,
            ("runtime_support", "taotrace_cache_replacement_supported"),
            target_path,
        ),
        True,
    )
    compare(
        "core.num_cores",
        nested(profile, ("core", "num_cores"), profile_path),
        nested(target, ("core", "count"), target_path),
    )
    compare(
        "core.freq_ghz",
        nested(profile, ("core", "freq_ghz"), profile_path),
        nested(target, ("clock", "frequency_ghz"), target_path),
    )
    for component in ("cache", "tlb"):
        compare(
            component,
            nested(profile, (component,), profile_path),
            nested(target, (component,), target_path),
        )
    compare(
        "coherence.protocol",
        nested(profile, ("coherence", "protocol"), profile_path),
        nested(target, ("coherence", "protocol"), target_path),
    )
    target_dram = nested(target, ("dram",), target_path)
    for field, value in nested(profile, ("dram",), profile_path).items():
        compare(f"dram.{field}", value, target_dram.get(field))
    return {
        "schema": SCHEMA,
        "result_dir": str(result_dir),
        "config_ini": str(config_path),
        "config_ini_sha256": config_hash,
        "effective_target": str(target_path),
        "uarch_profile": str(profile_path),
        "identity_source": "final-config-ini",
        "event_dictionary": str(event_dictionary),
        "event_dictionary_sha256": sha256(event_dictionary),
        "valid": not mismatches,
        "mismatches": mismatches,
    }


def pipeline_results(path: Path) -> list[Path]:
    document = read_json(path)
    cases = document.get("cases")
    if not isinstance(cases, list):
        raise ValueError(f"{path} lacks a cases array")
    results = []
    for index, case in enumerate(cases):
        if not isinstance(case, dict) or not case.get("result_dir"):
            raise ValueError(f"{path}: cases[{index}] lacks result_dir")
        results.append(Path(case["result_dir"]))
    return results


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--allow-legacy-request-identity",
        action="store_true",
        help=(
            "Permit request.json-based identity only when effective-target.json "
            "is absent. Such a result is diagnostic, not P0 formal."
        ),
    )
    parser.add_argument(
        "--event-dictionary",
        type=Path,
        default=EVENT_DICTIONARY,
        help=(
            "PMU contract dictionary to validate against. Formal dataset "
            "validation defaults to the repository contract; a run-local "
            "audit may pass its immutable launch-time snapshot."
        ),
    )
    parser.add_argument(
        "--result", action="append", type=Path, default=[],
        help="FS result directory; repeat for multiple cases.",
    )
    parser.add_argument(
        "--pipeline", action="append", type=Path, default=[],
        help="Accuracy pipeline.json whose result directories are audited.",
    )
    parser.add_argument(
        "--result-root", action="append", type=Path, default=[],
        help=(
            "Recursively audit result directories below this root. A result "
            "must contain request.json and tao_trace/uarch_profile.json."
        ),
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--report-only", action="store_true",
        help="Emit mismatches but return success instead of enforcing the gate.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    results = list(args.result)
    for pipeline in args.pipeline:
        results.extend(pipeline_results(pipeline))
    for root in args.result_root:
        for profile in root.resolve().rglob("tao_trace/uarch_profile.json"):
            result = profile.parent.parent
            if (result / "request.json").is_file():
                results.append(result)
    unique_results = sorted({path.resolve() for path in results})
    if not unique_results:
        raise SystemExit(
            "at least one result from --result, --pipeline, or --result-root "
            "is required"
        )
    validations = [
        validate_result_identity(
            path,
            args.allow_legacy_request_identity,
            args.event_dictionary,
        )
        for path in unique_results
    ]
    report = {
        "schema": SCHEMA,
        "cases": len(validations),
        "valid_cases": sum(item["valid"] for item in validations),
        "mismatched_cases": sum(not item["valid"] for item in validations),
        "valid": all(item["valid"] for item in validations),
        "results": validations,
    }
    text = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text)
    print(text, end="")
    return 0 if report["valid"] or args.report_only else 1


if __name__ == "__main__":
    raise SystemExit(main())
