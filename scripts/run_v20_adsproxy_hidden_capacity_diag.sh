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
TS=${TS:-$(date +%Y%m%d_%H%M%S)}

TAG=${WORKLOAD//[^A-Za-z0-9_]/_}
OUTDIR=${OUTDIR:-logs/v20_hidden_capacity_${TAG}_${TS}}
mkdir -p "$OUTDIR"

export CUDA_VISIBLE_DEVICES="$GPU"
export HF_HUB_OFFLINE=${HF_HUB_OFFLINE:-1}
export TOKENIZERS_PARALLELISM=${TOKENIZERS_PARALLELISM:-false}

echo "[hidden-capacity] root=$ROOT"
echo "[hidden-capacity] ckpt=$CKPT"
echo "[hidden-capacity] data=$DATA"
echo "[hidden-capacity] workload=$WORKLOAD n_core=$N_CORE max_samples=$MAX_SAMPLES gpu=$GPU"
echo "[hidden-capacity] outdir=$OUTDIR"

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

echo
"$PY" - "$OUTDIR/hidden_similarity.json" "$OUTDIR/hidden_probe.json" <<'PY'
import json
import math
import sys
from pathlib import Path

sim_path = Path(sys.argv[1])
probe_path = Path(sys.argv[2])
sim = json.loads(sim_path.read_text())
probe = json.loads(probe_path.read_text())
summary = sim.get("summary", {})
metrics = summary.get("metrics", {})

def get_metric(name, field="mean"):
    obj = metrics.get(name) or {}
    v = obj.get(field, float("nan"))
    try:
        return float(v)
    except Exception:
        return float("nan")

def fmt(v):
    return "nan" if not math.isfinite(v) else f"{v:.4f}"

print("===== hidden capacity summary =====")
print(f"samples={probe.get('samples')} core_samples={probe.get('core_samples')} target={probe.get('target')}")
print(f"label_cpi_cv_mean={fmt(get_metric('label_cpi_cv'))} pred_cpi_cv_mean={fmt(get_metric('pred_cpi_cv'))}")
print(f"trained_head_pred_label_corr_mean={fmt(get_metric('pred_label_cpi_corr'))}")
print(f"trained_head_slowest_hit={fmt(float(summary.get('slowest_hit_rate', float('nan'))))} fastest_hit={fmt(float(summary.get('fastest_hit_rate', float('nan'))))}")
print()
print("probe R2/corr/slow/fast:")
for p in probe.get("probes", []):
    print(
        f"  {p['name']:<24} "
        f"R2={fmt(float(p.get('r2_vs_collapse', float('nan'))))} "
        f"corr={fmt(float(p.get('corr', float('nan'))))} "
        f"win_corr={fmt(float(p.get('window_corr_mean', float('nan'))))} "
        f"slow={fmt(float(p.get('slowest_hit_rate', float('nan'))))} "
        f"fast={fmt(float(p.get('fastest_hit_rate', float('nan'))))}"
    )
print()
print("hidden geometry:")
for stage in ("query", "pre_adapter", "post_adapter"):
    print(
        f"  {stage:<12} "
        f"cos_mean={fmt(get_metric(stage + '_pair_cos_mean'))} "
        f"center_rel={fmt(get_metric(stage + '_center_rel_norm'))} "
        f"eff_rank={fmt(get_metric(stage + '_effective_rank'))}"
    )
print()
print(f"files: {sim_path.parent}")
PY

echo
echo "[hidden-capacity] done"
