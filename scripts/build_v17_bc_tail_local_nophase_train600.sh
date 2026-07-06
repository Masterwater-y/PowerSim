#!/usr/bin/env bash
set -euo pipefail

ROOT=${ROOT:-/data00/yinhaolang/LLMSim}
cd "$ROOT"

export OUT_C01=${OUT_C01:-data/windows_v17_bc_split_heads_nophase_c01}
export OUT_C04=${OUT_C04:-data/windows_v17_bc_split_heads_nophase_c04}
export OUT_C08=${OUT_C08:-data/windows_v17_bc_split_heads_nophase_c08}
export OUT_C16=${OUT_C16:-data/windows_v17_bc_split_heads_nophase_c16}
export COMB=${COMB:-data/windows_v17_bc_split_heads_nophase_all}

# scripts/build_v16_v9core_tail_local_train600.sh now defaults to the mainline
# 16-workload set with W_phased_mix excluded.
exec bash scripts/build_v16_v9core_tail_local_train600.sh "$@"
