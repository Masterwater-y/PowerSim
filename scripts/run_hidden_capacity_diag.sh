#!/usr/bin/env bash
set -euo pipefail

ROOT=${ROOT:-/data00/yinhaolang/LLMSim}
cd "$ROOT"

PY=${PY:-/data00/yinhaolang/infer/.venv/bin/python}
CKPT=${CKPT:-ckpt/v20_local_core_direct_fixed_8gpu_8000_spreadfix/step_003500}
DATA=${DATA:-data/windows_v17_bc_split_heads_nophase_all/windows.jsonl}
WORKLOAD=${WORKLOAD:-W_ads_ranking_proxy}
N_CORE=${N_CORE:-8}
MAX_SAMPLES=${MAX_SAMPLES:-128}
BATCH_SIZE=${BATCH_SIZE:-1}
MAX_LEN=${MAX_LEN:-32768}
GPU=${GPU:-0}
VAL_FRAC=${VAL_FRAC:-0.25}
RIDGE=${RIDGE:-10.0}
RUN_SIMILARITY=${RUN_SIMILARITY:-1}
RUN_PROBE=${RUN_PROBE:-1}
TS=${TS:-$(date +%Y%m%d_%H%M%S)}

safe_workload=${WORKLOAD//[^A-Za-z0-9_]/_}
safe_ckpt=$(basename "$CKPT")
TAG=${TAG:-hidden_capacity_${safe_workload}_${safe_ckpt}}
OUTDIR=${OUTDIR:-logs/${TAG}_${TS}}
mkdir -p "$OUTDIR"

export CUDA_VISIBLE_DEVICES="$GPU"
export HF_HUB_OFFLINE=${HF_HUB_OFFLINE:-1}
export TOKENIZERS_PARALLELISM=${TOKENIZERS_PARALLELISM:-false}

echo "[hidden-capacity] root=$ROOT"
echo "[hidden-capacity] ckpt=$CKPT"
echo "[hidden-capacity] data=$DATA"
echo "[hidden-capacity] workload=$WORKLOAD n_core=$N_CORE max_samples=$MAX_SAMPLES gpu=$GPU"
echo "[hidden-capacity] outdir=$OUTDIR"

if [[ "$RUN_SIMILARITY" == "1" ]]; then
  echo
  echo "[1/2] hidden geometry + trained head diagnostics"
  "$PY" scripts/analyze_core_hidden_similarity.py \
    --ckpt "$CKPT" \
    --data "$DATA" \
    --workload "$WORKLOAD" \
    --n-core "$N_CORE" \
    --max-len "$MAX_LEN" \
    --max-samples "$MAX_SAMPLES" \
    --batch-size "$BATCH_SIZE" \
    --out "$OUTDIR/hidden_similarity.json" \
    > "$OUTDIR/hidden_similarity.log" 2>&1
  echo "[1/2] wrote $OUTDIR/hidden_similarity.json"
fi

if [[ "$RUN_PROBE" == "1" ]]; then
  echo
  echo "[2/2] frozen hidden ridge probes"
  "$PY" scripts/probe_core_hidden_cpi.py \
    --ckpt "$CKPT" \
    --data "$DATA" \
    --workload "$WORKLOAD" \
    --n-core "$N_CORE" \
    --max-len "$MAX_LEN" \
    --max-samples "$MAX_SAMPLES" \
    --batch-size "$BATCH_SIZE" \
    --val-frac "$VAL_FRAC" \
    --ridge "$RIDGE" \
    --out "$OUTDIR/hidden_probe.json" \
    > "$OUTDIR/hidden_probe.log" 2>&1
  echo "[2/2] wrote $OUTDIR/hidden_probe.json"
fi

echo
"$PY" - "$OUTDIR" <<'PY'
import json
import math
import sys
from pathlib import Path

root = Path(sys.argv[1])
sim_path = root / "hidden_similarity.json"
probe_path = root / "hidden_probe.json"

def fmt(v):
    try:
        v = float(v)
    except Exception:
        return "nan"
    return "nan" if not math.isfinite(v) else f"{v:.4f}"

print("===== hidden capacity summary =====")
if probe_path.exists():
    probe = json.loads(probe_path.read_text())
    print(f"samples={probe.get('samples')} core_samples={probe.get('core_samples')} target={probe.get('target')}")
    print("probe R2/corr/slow/fast:")
    for p in probe.get("probes", []):
        print(
            f"  {p['name']:<24} "
            f"R2={fmt(p.get('r2_vs_collapse'))} "
            f"corr={fmt(p.get('corr'))} "
            f"win_corr={fmt(p.get('window_corr_mean'))} "
            f"slow={fmt(p.get('slowest_hit_rate'))} "
            f"fast={fmt(p.get('fastest_hit_rate'))}"
        )
else:
    print("probe: skipped")

if sim_path.exists():
    sim = json.loads(sim_path.read_text()).get("summary", {})
    metrics = sim.get("metrics", {})
    def met(name, field="mean"):
        return (metrics.get(name) or {}).get(field, float("nan"))
    print()
    print("trained head:")
    print(f"  pred_label_corr={fmt(met('pred_label_cpi_corr'))} slow={fmt(sim.get('slowest_hit_rate'))} fast={fmt(sim.get('fastest_hit_rate'))}")
    print(f"  label_cpi_cv={fmt(met('label_cpi_cv'))} pred_cpi_cv={fmt(met('pred_cpi_cv'))}")
    print()
    print("hidden geometry:")
    for stage in ("query", "pre_adapter", "post_adapter"):
        print(
            f"  {stage:<12} "
            f"cos_mean={fmt(met(stage + '_pair_cos_mean'))} "
            f"center_rel={fmt(met(stage + '_center_rel_norm'))} "
            f"eff_rank={fmt(met(stage + '_effective_rank'))}"
        )
else:
    print("similarity: skipped")
print()
print(f"files: {root}")
PY

echo
echo "[hidden-capacity] done"
