# Core Summary Feature Schema: v7 -> v8

Date: 2026-06-25

This document records the old v7 per-core summary feature scheme and the v8
replacement used by `data/build_windows.py` and `model/tokenizer.py`.

## Goal

The CPI residual analysis in
`docs/eval_v7_c08_absmiss_ddp8_roi_pmu_fix_results_20260625.md` showed that
the old summary had enough coarse memory/control signals but not enough
functional detail for:

- integer/floating-point long-latency operation mix,
- memory locality split by load/store and hot/cold behavior,
- dependency-chain depth after long-latency producers,
- indirect-branch target fanout/switching.

v8 replaces coarse histograms and coarse branch/dependency proxies with these
more direct functional features. Phase/context features are intentionally not
included, to avoid overfitting to the current workload phases.

## v7 Old Scheme

v7 emitted 35 summary tokens per core:

- scalar fractions/log counts: 16 fields
- `rd_hist`: 9 reuse-distance buckets
- `stride_hist`: 10 stride buckets

Old scalar fields:

| Field | Meaning |
| --- | --- |
| `mem_ratio` | memory uops / all uops |
| `load_frac_mem` | loads / memory uops |
| `store_frac_mem` | stores / memory uops |
| `distinct_lines` | distinct functional cache lines in window |
| `distinct_pages` | distinct functional pages in window |
| `seen_line_rate_8k` | memory refs whose line appeared in recent 8K memory refs |
| `seen_line_rate_64k` | memory refs whose line appeared in recent 64K memory refs |
| `recent_ws_size_64k` | recent 64K-memory-ref working-set size |
| `branch_density` | branch uops / all uops |
| `indirect_branch_rate` | indirect branches / branch uops |
| `pc_entropy` | normalized entropy of macro PCs |
| `basic_block_len_mean` | mean dynamic basic-block length |
| `branch_target_reuse_rate` | repeated next-PC successor ratio after branches |
| `short_reg_raw_rate` | producer distance <= 4 ratio |
| `reg_raw_distance_mean` | mean producer distance |
| `load_use_short_rate` | short dependency on a load producer |

Old histograms:

- `rd_hist[0..8]`: nonmem/cold/le8/le64/le512/le4k/le32k/le256k/far.
- `stride_hist[0..9]`: nonmem/first/same/+1/-1/+2..8/-2..8/+9..64/-9..64/large.

## v8 New Scheme

v8 emits 36 summary tokens per core. All fields are computed from functional
trace records only: `op_class`, architectural flags, program order,
`macro_pc`/`micro_pc`, `vaddr` or normalized cacheline fallback, and
`producer_dists`/`producer_classes`. It does not use timing, miss labels,
`path_class`, `d_mshr_depth`, TLB hit bits, oracle coherence fields, or PMU
labels.

### Op Mix, 12 Fields

All are ratios over all uops in the core window.

| Field | Calculation |
| --- | --- |
| `op_int_alu_ratio` | non-memory, non-branch integer ALU uops / uops |
| `op_int_mul_ratio` | integer multiply uops / uops |
| `op_int_divmod_ratio` | integer divide/mod uops / uops |
| `op_fp_alu_ratio` | FP add/cmp/cvt/misc uops / uops |
| `op_fp_mul_fma_ratio` | FP multiply or FMA uops / uops |
| `op_fp_divsqrt_ratio` | FP divide/sqrt uops / uops |
| `op_simd_ratio` | SIMD uops / uops |
| `op_load_ratio` | load uops / uops |
| `op_store_ratio` | store uops / uops |
| `op_cond_branch_ratio` | conditional branch uops / uops |
| `op_indirect_branch_ratio` | indirect branch uops / uops |
| `op_atomic_fence_sys_ratio` | atomic, serialize/fence, or system uops / uops |

These replace `mem_ratio`, `load_frac_mem`, `store_frac_mem`,
`branch_density`, and `indirect_branch_rate` with a broader instruction mix.

### Memory Locality Refinement, 7 Fields

| Field | Calculation |
| --- | --- |
| `load_rd_hot_ratio` | load refs with RD `le8` or `le64` / loads |
| `load_rd_cold_ratio` | load refs with RD `cold` or `far` / loads |
| `store_rd_hot_ratio` | store refs with RD `le8` or `le64` / stores |
| `store_rd_cold_ratio` | store refs with RD `cold` or `far` / stores |
| `stream_stride_ratio` | memory refs with stride `+/-1` or `+/-2..8` / memory refs |
| `large_stride_ratio` | memory refs with stride `+/-9..64` or `large` / memory refs |
| `addr_dep_load_ratio` | loads that consume a previous load-produced value / loads |

`addr_dep_load_ratio` is a functional pointer-chase proxy derived from
producer distances. It does not know whether the consumed value is definitely
the address operand unless the trace later emits operand-level dependency type.

These replace `rd_hist[9]` and `stride_hist[10]` with semantically targeted
load/store hot/cold and streaming/large-stride features.

### Dependency Chain, 6 Fields

| Field | Calculation |
| --- | --- |
| `short_dep_ratio` | producer distance <= 4 / all producer-distance entries |
| `dep_dist_mean_log` | `log2(mean(producer_distance) + 1)` |
| `raw_chain_depth_p95` | p95 RAW dependency-chain edge depth in the window |
| `raw_chain_depth_max_log` | `log2(max RAW dependency-chain edge depth + 1)` |
| `load_use_chain_p95` | p95 nonzero downstream chain depth rooted at load producers |
| `div_use_chain_p95` | p95 nonzero downstream chain depth rooted at div/sqrt producers |

These replace `short_reg_raw_rate`, `reg_raw_distance_mean`, and
`load_use_short_rate`.

### Indirect Target Behavior, 4 Fields

For each indirect branch, v8 uses the next committed macro PC as the functional
successor target proxy.

| Field | Calculation |
| --- | --- |
| `indirect_target_entropy` | occurrence-weighted normalized entropy of targets per indirect branch PC |
| `indirect_target_fanout_log` | `log2(mean(unique targets per indirect branch PC) + 1)` |
| `indirect_target_switch_rate` | target changes / consecutive indirect-branch observations at same PC |
| `indirect_top_target_ratio` | most common indirect successor target count / all indirect successors |

These replace `branch_target_reuse_rate` and add fanout/switching signals that
matter for indirect-branch CPI outliers.

### Retained Fields, 7 Fields

| Field | Reason |
| --- | --- |
| `distinct_lines` | coarse memory footprint |
| `distinct_pages` | coarse TLB/working-set footprint |
| `seen_line_rate_8k` | short-window warmness |
| `seen_line_rate_64k` | longer-window warmness |
| `recent_ws_size_64k` | recent working-set size |
| `pc_entropy` | code-footprint/control diversity |
| `basic_block_len_mean` | branch/control granularity |

## Replacement Map

| v7 feature(s) | v8 replacement |
| --- | --- |
| `mem_ratio`, `load_frac_mem`, `store_frac_mem` | `op_load_ratio`, `op_store_ratio`, plus full op mix |
| `branch_density`, `indirect_branch_rate` | `op_cond_branch_ratio`, `op_indirect_branch_ratio` |
| `rd_hist[9]` | load/store hot/cold RD ratios |
| `stride_hist[10]` | `stream_stride_ratio`, `large_stride_ratio` |
| `branch_target_reuse_rate` | indirect entropy/fanout/switch/top-target fields |
| `short_reg_raw_rate`, `reg_raw_distance_mean`, `load_use_short_rate` | dependency-chain fields |
| `distinct_*`, `seen_*`, `recent_ws_size_64k`, `pc_entropy`, `basic_block_len_mean` | retained |

## Token Budget

- v7: 35 summary tokens per core.
- v8: 36 summary tokens per core.

For 8 cores this adds 8 tokens per sample, which is negligible compared with a
32K token context. Existing v7 tokenizer/cache/checkpoints are schema
incompatible with v8; rebuild windows and ids cache after this change.
