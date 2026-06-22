import json
from pathlib import Path

from .dsl import dump_json


def load_minesim_cfg(path):
    data = {}
    section = ""
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or line.startswith(";"):
                continue
            if line.startswith("[") and line.endswith("]"):
                section = line[1:-1].strip()
                continue
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            data[f"{section}.{key.strip()}"] = value.strip()
    return data


def u32(cfg, key, default):
    value = cfg.get(key, default)
    return int(str(value), 0)


def bval(cfg, key, default):
    value = str(cfg.get(key, default)).strip().lower()
    return value in ("1", "true", "yes", "on")


def generate_model_from_minesim_cfg(cfg_path, name="minesim_config_cone"):
    cfg = load_minesim_cfg(cfg_path)
    retire_width = max(1, u32(cfg, "core.retire_width", 4))
    issue_width = max(1, u32(cfg, "core.issue_width", 4))
    branch_penalty = u32(cfg, "core.branch_mispredict_penalty", 16)
    l1i_lat = u32(cfg, "cache.l1i_latency", 4)
    l1d_lat = u32(cfg, "cache.l1d_latency", 4)
    l2_lat = u32(cfg, "cache.l2_latency", 14)
    l3_lat = u32(cfg, "cache.l3_latency", 70)
    mem_lat = u32(cfg, "memory.latency", 100)
    walk_lat = u32(cfg, "memory.page_walk_latency", 50)
    partial_stlf_penalty = u32(cfg, "experimental.partial_stlf_penalty", 11)
    mcw_window_size = max(1, u32(cfg, "experimental.mcw_window_size", 512))
    dram_channels = max(1, u32(cfg, "experimental.num_dram_channels", 1))
    dram_burst = max(1, u32(cfg, "experimental.dram_burst_cycles", 4))
    enable_mcw_stats = bval(cfg, "experimental.enable_mcw_stats", False)
    enable_mcw_timing = bval(cfg, "experimental.enable_mcw_timing", False)
    min_retire_cpi = 1.0 / retire_width
    min_issue_cpi = 1.0 / issue_width
    dram_min = l3_lat + mem_lat
    dram_max = l3_lat + mem_lat + max(mem_lat, dram_burst * dram_channels)
    prefetch_l2_max = max(4.0, l2_lat * 0.5)
    prefetch_l3_max = max(8.0, l3_lat * 0.5)
    writeback_l2_max = max(4.0, l2_lat * 0.75)
    writeback_l3_max = max(8.0, l3_lat * 0.75)
    llc_hit_min = max(1.0, l3_lat * 0.25)
    llc_hit_max = max(float(l3_lat), l3_lat * 1.5)
    dram_overlap_min = max(8.0, l3_lat * 0.25)
    dram_overlap_max = max(float(dram_min), dram_min * 0.75)
    walk_overlap_min = max(4.0, walk_lat * 0.10)
    walk_overlap_max = max(float(walk_lat), walk_lat * 0.60)
    visible_branch_min = 0.0
    visible_branch_max = branch_penalty * 2.0
    visible_load_min = 0.0
    visible_load_max = l1d_lat + l2_lat + l3_lat + mem_lat
    dependency_cycle_max = max(4.0, l1d_lat + issue_width)
    frontend_miss_max = l1i_lat + l2_lat + 16.0
    sq_drain_max = max(4.0, l1d_lat + l2_lat)
    partial_stlf_max = max(1.0, partial_stlf_penalty * 2.0)
    port_pressure_max = max(4.0, issue_width * 2.0)
    structural_other_max = max(4.0, branch_penalty + l1d_lat)

    counters = [
        ("core.instructions", "backend_core"),
        ("core.cycles", "backend_core"),
        ("branch.misses", "frontend_branch"),
        ("cache.l1i.misses", "frontend_icache"),
        ("cache.l1d.misses", "l1_l2_cache"),
        ("cache.l2.accesses", "l1_l2_cache"),
        ("cache.l2.misses", "l1_l2_cache"),
        ("cache.l2.writebacks", "l1_l2_cache"),
        ("cache.l3.accesses", "llc_cha"),
        ("cache.llc.load_misses", "llc_cha"),
        ("memory.dram_accesses", "memory"),
        ("tlb.dtlb_load_misses", "mmu_tlb"),
        ("timing.base_cycles", "timing_mcw"),
        ("timing.visible_branch_recovery_cycles", "timing_mcw"),
        ("timing.hidden_branch_recovery_cycles", "timing_mcw"),
        ("timing.visible_memory_stall_cycles", "timing_mcw"),
        ("timing.visible_frontend_miss_cycles", "timing_mcw"),
        ("timing.visible_sq_drain_cycles", "timing_mcw"),
        ("timing.visible_partial_stlf_cycles", "timing_mcw"),
        ("timing.dependency_stall_cycles", "timing_mcw"),
        ("timing.port_pressure_cycles", "timing_mcw"),
        ("timing.structural_other_cycles", "timing_mcw"),
        ("timing.residual_other_cycles", "timing_mcw"),
        ("timing.overlap_overcount_delta_cycles", "timing_mcw"),
        ("timing.accounted_visible_cycles", "timing_mcw"),
        ("timing.backend_stall_visible_cycles", "timing_mcw"),
        ("timing.partial_stlf_stall_cycles", "timing_mcw"),
        ("timing.mcw_timing_cycles", "timing_mcw"),
        ("timing.mcw_visible_dependency_cycles", "timing_mcw"),
        ("timing.mcw_hidden_dependency_cycles", "timing_mcw"),
        ("timing.mcw_visible_load_miss_cycles", "timing_mcw"),
        ("timing.mcw_hidden_load_miss_cycles", "timing_mcw"),
        ("timing.mcw_visible_branch_cycles", "timing_mcw"),
        ("timing.mcw_hidden_branch_cycles", "timing_mcw"),
        ("timing.mcw_visible_total_cycles", "timing_mcw"),
        ("timing.mcw_hidden_total_cycles", "timing_mcw"),
    ]
    model = {
        "schema_version": "0.1",
        "name": name,
        "source": {"kind": "minesim-config", "path": str(cfg_path)},
        "target": {"simulator": "minesim", "config": Path(cfg_path).name},
        "counters": [
            {"name": name, "component_hint": component, "unit": "count"}
            for name, component in counters
        ],
        "components": [
            {"name": "backend_core", "description": "retire/issue width and core cycles"},
            {"name": "frontend_branch", "description": "branch predictor and recovery penalty"},
            {"name": "frontend_icache", "description": "instruction fetch cache path"},
            {"name": "l1_l2_cache", "description": "private L1/L2 cache hierarchy"},
            {"name": "llc_cha", "description": "L3/LLC path"},
            {"name": "memory", "description": "DRAM service for LLC misses"},
            {"name": "mmu_tlb", "description": "DTLB load miss page walks"},
            {"name": "timing_mcw", "description": "MCW timing/decomposition counters exported by IntervalCore"},
        ],
        "rules": [
            {
                "name": "retire_width_bound",
                "component": "backend_core",
                "when": {"from_config": "core.retire_width"},
                "signature": {
                    "core.instructions": 1.0,
                    "core.cycles": {"min": min(min_retire_cpi, min_issue_cpi), "max": max(4.0, l1d_lat)},
                },
            },
            {
                "name": "branch_mispredict_penalty",
                "component": "frontend_branch",
                "when": {"from_config": "core.branch_mispredict_penalty"},
                "signature": {
                    "branch.misses": 1.0,
                    "core.cycles": {"min": max(1.0, branch_penalty * 0.5), "max": branch_penalty * 2.0},
                },
            },
            {
                "name": "l1i_miss_allocates_l2",
                "component": "frontend_icache",
                "when": {"path": "L1I miss -> L2 access"},
                "signature": {
                    "cache.l1i.misses": 1.0,
                    "cache.l2.accesses": 1.0,
                    "core.cycles": {"min": l1i_lat + l2_lat, "max": l1i_lat + l2_lat + 16.0},
                },
            },
            {
                "name": "l1d_miss_allocates_l2",
                "component": "l1_l2_cache",
                "when": {"path": "L1D miss -> L2 access"},
                "signature": {
                    "cache.l1d.misses": 1.0,
                    "cache.l2.accesses": 1.0,
                    "core.cycles": {"min": l1d_lat + l2_lat, "max": l1d_lat + l2_lat + 32.0},
                },
            },
            {
                "name": "l2_miss_allocates_l3",
                "component": "l1_l2_cache",
                "when": {"path": "L2 miss -> L3 access"},
                "signature": {
                    "cache.l2.misses": 1.0,
                    "cache.l3.accesses": 1.0,
                    "core.cycles": {"min": l2_lat + l3_lat, "max": l2_lat + l3_lat + 64.0},
                },
            },
            {
                "name": "l2_prefetch_or_writeback_access",
                "component": "l1_l2_cache",
                "when": {"path": "prefetch/writeback -> L2 access"},
                "signature": {
                    "cache.l2.accesses": 1.0,
                    "core.cycles": {"min": 0.0, "max": prefetch_l2_max},
                },
            },
            {
                "name": "l1d_writeback_to_l2",
                "component": "l1_l2_cache",
                "when": {"path": "L1D dirty eviction/writeback -> L2"},
                "signature": {
                    "cache.l2.accesses": 1.0,
                    "core.cycles": {"min": 0.0, "max": writeback_l2_max},
                },
            },
            {
                "name": "l2_miss_overlapped_l3_hit",
                "component": "llc_cha",
                "when": {"path": "L2 miss -> L3 hit with overlap"},
                "signature": {
                    "cache.l2.misses": 1.0,
                    "cache.l3.accesses": 1.0,
                    "core.cycles": {"min": llc_hit_min, "max": llc_hit_max},
                },
            },
            {
                "name": "l2_writeback_self",
                "component": "l1_l2_cache",
                "when": {"path": "L2 dirty eviction/writeback"},
                "signature": {
                    "cache.l2.writebacks": 1.0,
                    "core.cycles": {"min": 0.0, "max": writeback_l2_max},
                },
            },
            {
                "name": "l2_writeback_to_l3",
                "component": "llc_cha",
                "when": {"path": "L2 dirty eviction/writeback -> L3"},
                "signature": {
                    "cache.l2.writebacks": 1.0,
                    "cache.l3.accesses": 1.0,
                    "core.cycles": {"min": 0.0, "max": writeback_l3_max},
                },
            },
            {
                "name": "l3_prefetch_or_writeback_access",
                "component": "llc_cha",
                "when": {"path": "prefetch/writeback -> L3 access"},
                "signature": {
                    "cache.l3.accesses": 1.0,
                    "core.cycles": {"min": 0.0, "max": prefetch_l3_max},
                },
            },
            {
                "name": "l3_miss_goes_to_dram",
                "component": "memory",
                "when": {"path": "L3 miss -> DRAM access"},
                "signature": {
                    "cache.llc.load_misses": 1.0,
                    "memory.dram_accesses": 1.0,
                    "core.cycles": {"min": dram_min, "max": dram_max},
                },
            },
            {
                "name": "l3_writeback_to_dram",
                "component": "memory",
                "when": {"path": "L3 dirty eviction/writeback -> DRAM"},
                "signature": {
                    "memory.dram_accesses": 1.0,
                    "core.cycles": {"min": 0.0, "max": writeback_l3_max},
                },
            },
            {
                "name": "l3_miss_goes_to_dram_overlapped",
                "component": "memory",
                "when": {"path": "L3 miss -> DRAM with MLP/bandwidth overlap"},
                "signature": {
                    "cache.llc.load_misses": 1.0,
                    "memory.dram_accesses": 1.0,
                    "core.cycles": {"min": dram_overlap_min, "max": dram_overlap_max},
                },
            },
            {
                "name": "dtlb_load_page_walk",
                "component": "mmu_tlb",
                "when": {"from_config": "memory.page_walk_latency"},
                "signature": {
                    "tlb.dtlb_load_misses": 1.0,
                    "core.cycles": {"min": walk_lat, "max": walk_lat + l1d_lat + l2_lat + l3_lat + mem_lat},
                },
            },
            {
                "name": "dtlb_load_page_walk_overlapped",
                "component": "mmu_tlb",
                "when": {"path": "DTLB walk overlapped/merged with backend stalls"},
                "signature": {
                    "tlb.dtlb_load_misses": 1.0,
                    "core.cycles": {"min": walk_overlap_min, "max": walk_overlap_max},
                },
            },
            {
                "name": "base_cycles_from_instructions",
                "component": "timing_mcw",
                "when": {"path": "dispatch/issue width floor for base cycles"},
                "signature": {
                    "core.instructions": 1.0,
                    "timing.base_cycles": {"min": min(min_retire_cpi, min_issue_cpi), "max": max(4.0, l1d_lat)},
                },
            },
            {
                "name": "visible_branch_recovery_from_branch_misses",
                "component": "timing_mcw",
                "when": {"path": "visible branch recovery contributes elapsed cycles"},
                "signature": {
                    "branch.misses": 1.0,
                    "timing.visible_branch_recovery_cycles": {"min": visible_branch_min, "max": visible_branch_max},
                    "timing.mcw_visible_branch_cycles": {"min": visible_branch_min, "max": visible_branch_max},
                    "core.cycles": {"min": visible_branch_min, "max": visible_branch_max},
                },
            },
            {
                "name": "hidden_branch_recovery_from_branch_misses",
                "component": "timing_mcw",
                "when": {"path": "hidden branch recovery is tracked but not directly elapsed"},
                "signature": {
                    "branch.misses": 1.0,
                    "timing.hidden_branch_recovery_cycles": {"min": 0.0, "max": visible_branch_max},
                    "timing.mcw_hidden_branch_cycles": {"min": 0.0, "max": visible_branch_max},
                },
            },
            {
                "name": "visible_memory_stall_from_l1d_misses",
                "component": "timing_mcw",
                "when": {"path": "visible backend memory stall / MCW visible load miss"},
                "signature": {
                    "cache.l1d.misses": 1.0,
                    "timing.visible_memory_stall_cycles": {"min": visible_load_min, "max": visible_load_max},
                    "timing.backend_stall_visible_cycles": {"min": visible_load_min, "max": visible_load_max},
                    "timing.mcw_visible_load_miss_cycles": {"min": visible_load_min, "max": visible_load_max},
                    "core.cycles": {"min": visible_load_min, "max": visible_load_max},
                },
            },
            {
                "name": "hidden_memory_stall_from_l1d_misses",
                "component": "timing_mcw",
                "when": {"path": "hidden MCW load miss due to overlap"},
                "signature": {
                    "cache.l1d.misses": 1.0,
                    "timing.mcw_hidden_load_miss_cycles": {"min": 0.0, "max": visible_load_max},
                },
            },
            {
                "name": "visible_frontend_miss_from_l1i_misses",
                "component": "timing_mcw",
                "when": {"path": "icache/frontend miss shows up in decomposition"},
                "signature": {
                    "cache.l1i.misses": 1.0,
                    "timing.visible_frontend_miss_cycles": {"min": l1i_lat, "max": frontend_miss_max},
                    "core.cycles": {"min": l1i_lat, "max": frontend_miss_max},
                },
            },
            {
                "name": "dependency_visible_from_instructions",
                "component": "timing_mcw",
                "when": {"path": "dependency chains attributed by MCW"},
                "signature": {
                    "core.instructions": 1.0,
                    "timing.dependency_stall_cycles": {"min": 0.0, "max": dependency_cycle_max},
                    "timing.mcw_visible_dependency_cycles": {"min": 0.0, "max": dependency_cycle_max},
                    "timing.mcw_hidden_dependency_cycles": {"min": 0.0, "max": dependency_cycle_max},
                    "core.cycles": {"min": 0.0, "max": dependency_cycle_max},
                },
            },
            {
                "name": "sq_drain_weak_bound",
                "component": "timing_mcw",
                "when": {"path": "store queue drain is bounded by L1D+L2 latency per drain event"},
                "signature": {
                    "timing.visible_sq_drain_cycles": {"min": 0.0, "max": sq_drain_max},
                },
            },
            {
                "name": "partial_stlf_from_instructions",
                "component": "timing_mcw",
                "when": {"path": "partial STLF timing terms"},
                "signature": {
                    "core.instructions": 1.0,
                    "timing.visible_partial_stlf_cycles": {"min": 0.0, "max": partial_stlf_max},
                    "timing.partial_stlf_stall_cycles": {"min": 0.0, "max": partial_stlf_max},
                    "core.cycles": {"min": 0.0, "max": partial_stlf_max},
                },
            },
            {
                "name": "port_and_structural_residual_from_instructions",
                "component": "timing_mcw",
                "when": {"path": "backend residual decomposition"},
                "signature": {
                    "core.instructions": 1.0,
                    "timing.port_pressure_cycles": {"min": 0.0, "max": port_pressure_max},
                    "timing.structural_other_cycles": {"min": 0.0, "max": structural_other_max},
                    "timing.residual_other_cycles": {"min": 0.0, "max": port_pressure_max + structural_other_max},
                },
            },
            {
                "name": "accounted_visible_cycles_from_instructions",
                "component": "timing_mcw",
                "when": {"path": "visible decomposition total"},
                "signature": {
                    "core.instructions": 1.0,
                    "timing.accounted_visible_cycles": {"min": min(min_retire_cpi, min_issue_cpi), "max": visible_load_max + visible_branch_max + dependency_cycle_max + frontend_miss_max},
                    "timing.mcw_visible_total_cycles": {"min": 0.0, "max": visible_load_max + visible_branch_max + dependency_cycle_max},
                    "timing.mcw_hidden_total_cycles": {"min": 0.0, "max": visible_load_max + visible_branch_max + dependency_cycle_max},
                },
            },
            {
                "name": "mcw_timing_matches_elapsed_cycles",
                "component": "timing_mcw",
                "when": {"path": "MCW timing enabled: recomposed timing equals elapsed cycles"},
                "signature": {
                    "core.cycles": 1.0,
                    "timing.mcw_timing_cycles": 1.0,
                },
            },
        ],
        "config_values": {
            "retire_width": retire_width,
            "issue_width": issue_width,
            "branch_mispredict_penalty": branch_penalty,
            "l1i_latency": l1i_lat,
            "l1d_latency": l1d_lat,
            "l2_latency": l2_lat,
            "l3_latency": l3_lat,
            "memory_latency": mem_lat,
            "page_walk_latency": walk_lat,
            "num_dram_channels": dram_channels,
            "dram_burst_cycles": dram_burst,
            "prefetch_l2_max": prefetch_l2_max,
            "prefetch_l3_max": prefetch_l3_max,
            "writeback_l2_max": writeback_l2_max,
            "writeback_l3_max": writeback_l3_max,
            "llc_hit_min": llc_hit_min,
            "llc_hit_max": llc_hit_max,
            "dram_overlap_min": dram_overlap_min,
            "dram_overlap_max": dram_overlap_max,
            "walk_overlap_min": walk_overlap_min,
            "walk_overlap_max": walk_overlap_max,
            "partial_stlf_penalty": partial_stlf_penalty,
            "mcw_window_size": mcw_window_size,
            "enable_mcw_stats": enable_mcw_stats,
            "enable_mcw_timing": enable_mcw_timing,
            "visible_branch_min": visible_branch_min,
            "visible_branch_max": visible_branch_max,
            "visible_load_min": visible_load_min,
            "visible_load_max": visible_load_max,
            "dependency_cycle_max": dependency_cycle_max,
            "frontend_miss_max": frontend_miss_max,
            "sq_drain_max": sq_drain_max,
            "partial_stlf_max": partial_stlf_max,
            "port_pressure_max": port_pressure_max,
            "structural_other_max": structural_other_max,
        },
    }
    return model


def save_generated_model(model, path):
    dump_json(model, path)
