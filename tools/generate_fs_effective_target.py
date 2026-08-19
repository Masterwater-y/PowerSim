#!/usr/bin/env python3
"""Generate canonical FS target sidecars from gem5's final config.ini.

The generated ``uarch_profile.json`` contains only fields consumed by the
TaoTrace functional oracle.  ``effective-target.json`` is the authoritative,
broader baseline identity used by FastSim validation.  Values are read from
the final SimObject dump; command-line defaults and request.json are never
used as substitutes.
"""

from __future__ import annotations

import argparse
import configparser
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Callable


PROFILE_SCHEMA = 2
MANIFEST_SCHEMA = "fastsim-gem5-effective-target-v1"
PMU_CONTRACT = "perf-gem5-fastsim-x86-fs-v1"
RUBY_PREFIX = "board.cache_hierarchy.ruby_system"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--uarch-profile", type=Path, required=True)
    parser.add_argument("--effective-target", type=Path, required=True)
    parser.add_argument(
        "--event-dictionary",
        type=Path,
        default=Path(__file__).resolve().parents[1]
        / "configs"
        / "pmu-event-dictionary-v1.json",
    )
    parser.add_argument(
        "--taotrace-shared-root",
        type=Path,
        default=Path(
            os.environ.get("TAOGEN_SHARED", "/data00/yinhaolang/taogen/shared")
        ),
        help=(
            "Shared TaoTrace oracle-model headers. Their hashes and supported "
            "replacement policies become part of the effective identity."
        ),
    )
    return parser.parse_args()


def load_ini(path: Path) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.optionxform = str
    with path.open(encoding="utf-8") as source:
        parser.read_file(source)
    return parser


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def semantic_json_sha256(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def sections_matching(
    ini: configparser.ConfigParser, pattern: str
) -> list[str]:
    compiled = re.compile(pattern)
    return sorted(section for section in ini.sections() if compiled.fullmatch(section))


def require(ini: configparser.ConfigParser, section: str, field: str) -> str:
    if not ini.has_section(section) or not ini.has_option(section, field):
        raise ValueError(f"config.ini lacks [{section}] {field}")
    return ini.get(section, field)


def homogeneous(
    ini: configparser.ConfigParser,
    sections: list[str],
    field: str,
    convert: Callable[[str], Any] = str,
) -> Any:
    if not sections:
        raise ValueError(f"no config.ini sections found for {field}")
    values = {convert(require(ini, section, field)) for section in sections}
    if len(values) != 1:
        raise ValueError(f"non-homogeneous {field}: {sorted(values, key=str)}")
    return values.pop()


def replacement_policy(
    ini: configparser.ConfigParser, cache_sections: list[str]
) -> str:
    references = {
        require(ini, section, "replacement_policy") for section in cache_sections
    }
    policies = {require(ini, reference, "type") for reference in references}
    if len(policies) != 1:
        raise ValueError(f"non-homogeneous replacement policy: {sorted(policies)}")
    return {
        "LRURP": "lru",
        "TreePLRURP": "tree_plru",
    }.get(policies.pop(), "unsupported")


def cache_profile(
    ini: configparser.ConfigParser,
    sections: list[str],
    line_bytes: int,
    banks: int,
    total_capacity: bool,
) -> dict[str, Any]:
    size = homogeneous(ini, sections, "size", int)
    if total_capacity:
        size *= banks
    return {
        "size_b": size,
        "assoc": homogeneous(ini, sections, "assoc", int),
        "line_b": line_bytes,
        "num_banks": banks,
        "bank_select_low_bit": line_bytes.bit_length() - 1,
        "policy": replacement_policy(ini, sections),
    }


def main() -> int:
    args = parse_args()
    config_path = args.config.resolve()
    dictionary_path = args.event_dictionary.resolve()
    ini = load_ini(config_path)
    dictionary = json.loads(dictionary_path.read_text(encoding="utf-8"))
    if dictionary.get("contract_id") != PMU_CONTRACT:
        raise SystemExit(
            f"{dictionary_path}: expected PMU contract {PMU_CONTRACT!r}"
        )

    shared_root = args.taotrace_shared_root.resolve()
    uarch_header = shared_root / "uarch_profile.hh"
    cache_header = shared_root / "lru_banked.hh"
    try:
        uarch_text = uarch_header.read_text(encoding="utf-8")
        cache_text = cache_header.read_text(encoding="utf-8")
    except OSError as exc:
        raise SystemExit(f"cannot inspect TaoTrace shared model: {exc}") from exc
    tree_plru_supported = (
        'c.policy != "lru" && c.policy != "tree_plru"' in uarch_text
        and "treeVictim" in cache_text
        and 'cfg_.policy == "tree_plru"' in cache_text
    )
    supported_replacement_policies = ["lru"]
    if tree_plru_supported:
        supported_replacement_policies.append("tree_plru")

    cores = sections_matching(ini, r"board\.processor\.switch\d+\.core")
    l1_controllers = sections_matching(
        ini, re.escape(RUBY_PREFIX) + r"\.l1_controllers\d+"
    )
    l2_controllers = sections_matching(
        ini, re.escape(RUBY_PREFIX) + r"\.l2_controllers\d+"
    )
    l3_controllers = sections_matching(
        ini, re.escape(RUBY_PREFIX) + r"\.l3_controllers\d+"
    )
    directories = sections_matching(
        ini, re.escape(RUBY_PREFIX) + r"\.directory_controllers\d+"
    )
    memory_controllers = sections_matching(ini, r"board\.memory\.mem_ctrl\d+")
    dram_interfaces = [f"{section}.dram" for section in memory_controllers]
    l1d_sections = [f"{section}.Dcache" for section in l1_controllers]
    l1i_sections = [f"{section}.Icache" for section in l1_controllers]
    l2_sections = [f"{section}.cache" for section in l2_controllers]
    l3_sections = [f"{section}.L2cache" for section in l3_controllers]

    core_ids = [homogeneous(ini, [section], "cpu_id", int) for section in cores]
    if sorted(core_ids) != list(range(len(cores))):
        raise ValueError(f"core IDs must be dense, found {sorted(core_ids)}")
    if len(l1_controllers) != len(cores) or len(l2_controllers) != len(cores):
        raise ValueError("private-cache controller counts do not match core count")
    if len(l3_controllers) != len(directories):
        raise ValueError("LLC slice and directory controller counts differ")
    if len(memory_controllers) != len(directories):
        raise ValueError("memory channel and directory controller counts differ")

    line_bytes = int(require(ini, RUBY_PREFIX, "block_size_bytes"))
    clock_period_ticks = int(require(ini, "board.clk_domain", "clock"))
    frequency_ghz = 1000.0 / clock_period_ticks
    network_section = f"{RUBY_PREFIX}.network"
    network_type = require(ini, network_section, "type")
    virtual_networks = int(
        require(ini, RUBY_PREFIX, "number_of_virtual_networks")
    )

    l1d = cache_profile(ini, l1d_sections, line_bytes, 1, False)
    l1i = cache_profile(ini, l1i_sections, line_bytes, 1, False)
    l2 = cache_profile(ini, l2_sections, line_bytes, 1, False)
    l3 = cache_profile(
        ini, l3_sections, line_bytes, len(l3_controllers), True
    )
    dtlb_sections = [f"{section}.mmu.dtb" for section in cores]
    itlb_sections = [f"{section}.mmu.itb" for section in cores]
    dtlb_entries = homogeneous(ini, dtlb_sections, "size", int)
    itlb_entries = homogeneous(ini, itlb_sections, "size", int)

    ranges = [require(ini, section, "range") for section in dram_interfaces]
    range_bounds = set()
    interleave_matches = set()
    for range_text in ranges:
        range_parts = range_text.split(":")
        if len(range_parts) < 2:
            raise ValueError(f"unsupported DRAM range {range_text!r}")
        range_bounds.add((int(range_parts[0]), int(range_parts[1])))
        if len(range_parts) >= 3:
            interleave_matches.add(int(range_parts[2]))
    if len(range_bounds) != 1:
        raise ValueError(f"DRAM interfaces disagree on bounds: {ranges}")
    if interleave_matches and interleave_matches != set(
        range(len(memory_controllers))
    ):
        raise ValueError(
            "DRAM interleave match values are not dense channel IDs: "
            f"{sorted(interleave_matches)}"
        )
    range_start, range_end = range_bounds.pop()
    dram_size = range_end - range_start
    row_size = homogeneous(ini, dram_interfaces, "device_rowbuffer_size", int)
    row_size *= homogeneous(ini, dram_interfaces, "devices_per_rank", int)
    burst = (
        homogeneous(ini, dram_interfaces, "burst_length", int)
        * homogeneous(ini, dram_interfaces, "device_bus_width", int)
        * homogeneous(ini, dram_interfaces, "devices_per_rank", int)
        // 8
    )
    read_queue = homogeneous(ini, dram_interfaces, "read_buffer_size", int)
    write_queue = homogeneous(ini, dram_interfaces, "write_buffer_size", int)
    controller_fields = {
        "mem_sched_policy": homogeneous(
            ini, memory_controllers, "mem_sched_policy"
        ),
        "read_buffer_size": read_queue,
        "write_buffer_size": write_queue,
        "write_high_thresh_perc": homogeneous(
            ini, memory_controllers, "write_high_thresh_perc", int
        ),
        "write_low_thresh_perc": homogeneous(
            ini, memory_controllers, "write_low_thresh_perc", int
        ),
        "page_policy": homogeneous(ini, dram_interfaces, "page_policy"),
        "addr_mapping": homogeneous(ini, dram_interfaces, "addr_mapping"),
        "ranks_per_channel": homogeneous(
            ini, dram_interfaces, "ranks_per_channel", int
        ),
        "banks_per_rank": homogeneous(
            ini, dram_interfaces, "banks_per_rank", int
        ),
        "bank_groups_per_rank": homogeneous(
            ini, dram_interfaces, "bank_groups_per_rank", int
        ),
        "tREFI_ticks": homogeneous(ini, dram_interfaces, "tREFI", int),
        "tRFC_ticks": homogeneous(ini, dram_interfaces, "tRFC", int),
    }

    protocol_types = {
        require(ini, section, "type") for section in l1_controllers
    } | {require(ini, section, "type") for section in l2_controllers} | {
        require(ini, section, "type") for section in l3_controllers
    }
    if not all("MESI_Three_Level" in item for item in protocol_types):
        raise ValueError(f"unsupported mixed coherence types: {sorted(protocol_types)}")

    profile = {
        "schema_version": PROFILE_SCHEMA,
        "source": "generated from final gem5 config.ini",
        "source_config_sha256": sha256(config_path),
        "core": {
            "isa": "X86",
            "num_cores": len(cores),
            "freq_ghz": frequency_ghz,
        },
        "cache": {"l1d": l1d, "l1i": l1i, "l2": l2, "l3": l3},
        "tlb": {
            "dtlb": {"entries": dtlb_entries, "assoc": dtlb_entries},
            "itlb": {"entries": itlb_entries, "assoc": itlb_entries},
            "stlb": None,
        },
        "page_walker": {
            "levels": 4,
            "page_size_bits": 12,
            "walk_attaches_to": "sequencer",
            "pwc_entries": 0,
        },
        "coherence": {"protocol": "MESI_Three_Level"},
        "dram": {
            "model": homogeneous(ini, dram_interfaces, "type"),
            "size_b": dram_size,
            "num_channels": len(memory_controllers),
            "banks_per_channel": controller_fields["banks_per_rank"]
            * controller_fields["ranks_per_channel"],
            "row_size_b": row_size,
            "burst_b": burst,
            "interleaving_size_b": line_bytes,
            "read_queue_entries": read_queue,
            "write_queue_entries": write_queue,
        },
    }

    core_fields = (
        "fetchWidth",
        "decodeWidth",
        "renameWidth",
        "dispatchWidth",
        "issueWidth",
        "wbWidth",
        "commitWidth",
        "numROBEntries",
        "LQEntries",
        "SQEntries",
        "numPhysIntRegs",
        "numPhysFloatRegs",
        "numPhysVecRegs",
        "numPhysCCRegs",
    )
    pipeline = {
        field: homogeneous(ini, cores, field, int) for field in core_fields
    }
    iq_sections = [f"{section}.instQueues" for section in cores]
    pipeline["numIQEntries"] = homogeneous(ini, iq_sections, "numEntries", int)

    effective = {
        "schema": MANIFEST_SCHEMA,
        "source": {
            "config_ini": str(config_path),
            "config_ini_sha256": sha256(config_path),
            "event_dictionary": str(dictionary_path),
            "event_dictionary_sha256": sha256(dictionary_path),
            "pmu_contract_id": PMU_CONTRACT,
            "uarch_profile_semantic_sha256": semantic_json_sha256(profile),
            "taotrace_oracle_model": {
                "uarch_profile_hh": str(uarch_header),
                "uarch_profile_hh_sha256": sha256(uarch_header),
                "cache_model_hh": str(cache_header),
                "cache_model_hh_sha256": sha256(cache_header),
            },
        },
        "clock": {
            "clock_period_ticks": clock_period_ticks,
            "frequency_ghz": frequency_ghz,
        },
        "core": {"count": len(cores), "pipeline": pipeline},
        "cache": profile["cache"],
        "tlb": profile["tlb"],
        "coherence": {
            "protocol": "MESI_Three_Level",
            "l1_controller_tbes": homogeneous(
                ini, l1_controllers, "number_of_TBEs", int
            ),
            "l2_controller_tbes": homogeneous(
                ini, l2_controllers, "number_of_TBEs", int
            ),
            "llc_controller_tbes": homogeneous(
                ini, l3_controllers, "number_of_TBEs", int
            ),
            "l1_transitions_per_cycle": homogeneous(
                ini, l1_controllers, "transitions_per_cycle", int
            ),
            "l2_transitions_per_cycle": homogeneous(
                ini, l2_controllers, "transitions_per_cycle", int
            ),
            "llc_transitions_per_cycle": homogeneous(
                ini, l3_controllers, "transitions_per_cycle", int
            ),
        },
        "network": {
            "type": network_type,
            "virtual_networks": virtual_networks,
            "endpoint_bandwidth": int(
                require(ini, network_section, "endpoint_bandwidth")
            ),
            "buffer_size": int(require(ini, network_section, "buffer_size")),
        },
        "dram": {
            **profile["dram"],
            **controller_fields,
        },
        "runtime_support": {
            "taotrace_cache_replacement_supported": all(
                cache["policy"] in supported_replacement_policies
                for cache in (l1d, l1i, l2, l3)
            ),
            "taotrace_supported_replacement_policies": (
                supported_replacement_policies
            ),
            "note": (
                "false means the current TaoTrace BankedSetAssocLRU must be "
                "extended before this profile can be a cache-PMU oracle"
            ),
        },
    }

    args.uarch_profile.parent.mkdir(parents=True, exist_ok=True)
    args.effective_target.parent.mkdir(parents=True, exist_ok=True)
    args.uarch_profile.write_text(
        json.dumps(profile, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    args.effective_target.write_text(
        json.dumps(effective, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "schema": "fastsim-effective-target-generation-v1",
                "config": str(config_path),
                "uarch_profile": str(args.uarch_profile.resolve()),
                "effective_target": str(args.effective_target.resolve()),
                "core_count": len(cores),
                "cache_policies": {
                    name: profile["cache"][name]["policy"]
                    for name in ("l1d", "l1i", "l2", "l3")
                },
                "taotrace_cache_replacement_supported": effective[
                    "runtime_support"
                ]["taotrace_cache_replacement_supported"],
                "taotrace_supported_replacement_policies": (
                    supported_replacement_policies
                ),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
