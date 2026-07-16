"""Strict, versioned contracts for the v29 global-time model."""
from __future__ import annotations

from typing import Any, Dict, Iterable


RAW_TRACE_SCHEMA_VERSION = "v28.1-branch-roi-percore"
DATASET_SCHEMA_VERSION = "global-time-v29-packed-1"
FEATURE_SCHEMA_VERSION = (
    "v29-base12-branch9-resource5-dynamic8-state5-summary38-relation22"
)
MODEL_INPUT_CONTRACT = "functional_only_v29_global_time_prefix"
BRANCH_CONTRACT_VERSION = "canonical_branch_token_v29"
RESOURCE_DECODER_SCHEMA_VERSION = "gem5-resource-decoder-v29-1"
CHECKPOINT_SCHEMA_VERSION = "tcsim-v29-checkpoint-1"


# local_pc_id/local_line_id and nominal set/bank/channel IDs are intentionally
# absent.  Equality and contention are built from non-model-facing exact keys.
BASE_FIELD_NAMES = (
    "op_class",
    "reg_dependency",
    "mem_kind",
    "producer_distance",
    "reuse_distance",
    "stride",
    "macro_position",
    "same_core_history",
    "mem_size",
    "line_offset",
    "recent_ws_short",
    "recent_ws_long",
)
BASE_FIELD_SIZES = (90, 64, 5, 17, 9, 10, 5, 12, 10, 10, 14, 18)

BRANCH_FIELD_NAMES = (
    "branch_kind",
    "branch_taken",
    "branch_successor_delta",
    "branch_history_low8",
    "branch_history_high8",
    "branch_pc_reuse",
    "predictor_index_alias",
    "branch_target_reuse",
    "ras_depth",
)
BRANCH_FIELD_SIZES = (32, 3, 34, 257, 257, 9, 10, 9, 18)

RESOURCE_FIELD_NAMES = (
    "paddr_valid",
    "dram_row_reuse",
    "l1_set_pressure",
    "l2_set_pressure",
    "llc_set_pressure",
)
RESOURCE_FIELD_SIZES = (3, 10, 10, 10, 10)

FIELD_NAMES = BASE_FIELD_NAMES + BRANCH_FIELD_NAMES + RESOURCE_FIELD_NAMES
FIELD_SIZES = BASE_FIELD_SIZES + BRANCH_FIELD_SIZES + RESOURCE_FIELD_SIZES
FIELD_PAD_IDS = tuple(FIELD_SIZES)
FIELD_INDEX = {name: index for index, name in enumerate(FIELD_NAMES)}
FIELD_GROUP_INDICES = {
    "base": tuple(FIELD_INDEX[name] for name in BASE_FIELD_NAMES),
    "branch": tuple(FIELD_INDEX[name] for name in BRANCH_FIELD_NAMES),
    "resource": tuple(FIELD_INDEX[name] for name in RESOURCE_FIELD_NAMES),
}

DYNAMIC_FIELD_NAMES = (
    "xcore_line_role",
    "xcore_line_fanout",
    "llc_set_fanout",
    "llc_bank_fanout",
    "dram_channel_fanout",
    "dram_bank_fanout",
    "same_row_support",
    "different_row_conflict",
)
DYNAMIC_FIELD_SIZES = (8, 8, 8, 8, 8, 8, 8, 8)
DYNAMIC_PAD_IDS = tuple(DYNAMIC_FIELD_SIZES)
DYNAMIC_FIELD_INDEX = {
    name: index for index, name in enumerate(DYNAMIC_FIELD_NAMES)
}

# These integer keys are never embedded or returned to the model.  They exist
# solely to derive permutation-invariant equality/alias/pressure relations.
RESOURCE_KEY_NAMES = (
    "physical_line",
    "l1_set",
    "l2_set",
    "llc_set",
    "llc_bank",
    "dram_channel",
    "dram_rank",
    "dram_bank",
    "dram_row",
    "dram_column",
)
RESOURCE_KEY_INDEX = {
    name: index for index, name in enumerate(RESOURCE_KEY_NAMES)
}
RESOURCE_KEY_INVALID = -1

STATE_FEATURE_NAMES = (
    "log1p_head_age",
    "log1p_elapsed_since_last_commit",
    "log1p_roi_age",
    "cold_start",
    "active_core_fraction",
)

CHUNK_SUMMARY_NAMES = (
    "load_frac",
    "store_frac",
    "atomic_frac",
    "branch_frac",
    "int_frac",
    "fp_frac",
    "simd_frac",
    "serialize_frac",
    "int_mul_frac",
    "int_div_frac",
    "fp_alu_frac",
    "fp_fma_frac",
    "fp_divsqrt_frac",
    "conditional_branch_frac",
    "indirect_branch_frac",
    "distinct_lines_per_uop",
    "distinct_pages_per_uop",
    "mean_log1p_producer_distance",
    "max_log1p_producer_distance",
    "mem_hot_frac",
    "mem_cold_frac",
    "stream_stride_frac",
    "large_stride_frac",
    "short_dependency_frac",
    "pc_entropy",
    "mean_basic_block_len_log",
    "tail_fraction",
    "taken_branch_frac",
    "branch_direction_switch_rate",
    "paddr_valid_mem_frac",
    "distinct_l1_sets_per_mem",
    "distinct_l2_sets_per_mem",
    "distinct_llc_sets_per_mem",
    "llc_set_conflict_frac",
    "llc_bank_hhi",
    "dram_channel_hhi",
    "dram_bank_hhi",
    "dram_row_reuse_frac",
)

RELATION_FEATURE_NAMES = (
    "log1p_active_cores",
    "shared_line_frac",
    "read_after_other_write_frac",
    "write_to_other_access_frac",
    "multiwriter_frac",
    "shared_read_line_frac",
    "shared_write_line_frac",
    "mean_other_reader_fanout",
    "mean_other_writer_fanout",
    "max_other_accessor_fanout",
    "writer_core_coverage",
    "global_lines_per_kuop_log",
    "core_global_line_coverage",
    "aggregate_mem_density",
    "same_llc_set_frac",
    "same_llc_bank_frac",
    "same_dram_channel_frac",
    "same_dram_bank_frac",
    "same_dram_row_frac",
    "different_row_same_bank_frac",
    "mean_llc_set_other_fanout",
    "mean_dram_bank_other_fanout",
)

UARCH_FEATURE_NAMES = (
    "freq_ghz",
    "log2_num_cores",
    "log2_fetch_width",
    "log2_decode_width",
    "log2_issue_width",
    "log2_commit_width",
    "log2_rob_entries",
    "log2_iq_entries",
    "log2_lq_entries",
    "log2_sq_entries",
    "log2_l1d_size",
    "log2_l1d_assoc",
    "log2_l2_size",
    "log2_l2_assoc",
    "log2_l3_size",
    "log2_l3_assoc",
    "log2_l3_banks",
    "log2_dtlb_entries",
    "log2_dtlb_assoc",
    "log2_itlb_entries",
    "log2_itlb_assoc",
    "log2_l1d_mshr",
    "log2_l2_mshr",
    "log2_l3_mshr",
    "log2_dram_channels",
    "log2_dram_banks_per_rank",
    "log2_dram_ranks_per_channel",
    "log2_dram_row_buffer_size",
    "log2_dram_burst",
)


def normalized_horizons(values: Iterable[float]) -> tuple[float, ...]:
    out = tuple(sorted({float(value) for value in values if float(value) > 0}))
    if not out:
        raise ValueError("v29 requires at least one positive horizon")
    return out


def feature_contract_metadata(
    *,
    predictor_hash: str,
    resource_decoder_hash: str,
    horizons: Iterable[float],
    sample_period_cycles: float,
) -> Dict[str, Any]:
    horizon_tuple = normalized_horizons(horizons)
    return {
        "raw_trace_schema": RAW_TRACE_SCHEMA_VERSION,
        "dataset_schema": DATASET_SCHEMA_VERSION,
        "model_input_contract": MODEL_INPUT_CONTRACT,
        "feature_schema": FEATURE_SCHEMA_VERSION,
        "branch_contract": BRANCH_CONTRACT_VERSION,
        "resource_decoder_schema": RESOURCE_DECODER_SCHEMA_VERSION,
        "resource_decoder_hash": str(resource_decoder_hash),
        "predictor_hash": str(predictor_hash),
        "horizons": list(horizon_tuple),
        "sample_period_cycles": float(sample_period_cycles),
        "dimensions": {
            "static_fields": len(FIELD_NAMES),
            "dynamic_fields": len(DYNAMIC_FIELD_NAMES),
            "resource_keys": len(RESOURCE_KEY_NAMES),
            "state": len(STATE_FEATURE_NAMES),
            "chunk_summary": len(CHUNK_SUMMARY_NAMES),
            "relation": len(RELATION_FEATURE_NAMES),
            "uarch": len(UARCH_FEATURE_NAMES),
            "horizons": len(horizon_tuple),
        },
    }
