#!/usr/bin/env bash
set -euo pipefail

ROOT=${ROOT:-/data00/yinhaolang/LLMSim}
cd "$ROOT"

PY=${PY:-/data00/yinhaolang/infer/.venv/bin/python}
CKPT=${CKPT:-ckpt/v9_tq_train600_8gpu_8000_rerun}
RAW=${RAW:-data/raw_trace_pool/activecore_eval/c08_seedB_infer17}
WORKLOAD=${WORKLOAD:-W_ads_ranking_proxy}
GPU=${GPU:-0}
MAX_LEN=${MAX_LEN:-32768}
TRAIN_MAX_LEN=${TRAIN_MAX_LEN:-32768}
MAX_WINDOWS=${MAX_WINDOWS:-1000}
QUERY_PLACEMENT=${QUERY_PLACEMENT:-tail}
TAG=${TAG:-diag_v9_cpi_spread}
TS=${TS:-$(date +%Y%m%d_%H%M%S)}
OUTDIR=${OUTDIR:-logs/${TAG}_${WORKLOAD}_${TS}}

mkdir -p "$OUTDIR"

echo "[diag] root=$ROOT"
echo "[diag] ckpt=$CKPT"
echo "[diag] raw=$RAW"
echo "[diag] workload=$WORKLOAD"
echo "[diag] gpu=$GPU max_len=$MAX_LEN max_windows=$MAX_WINDOWS"
echo "[diag] query_placement=$QUERY_PLACEMENT"
echo "[diag] outdir=$OUTDIR"

if ! HF_HUB_OFFLINE=1 CUDA_VISIBLE_DEVICES="$GPU" "$PY" eval/eval_quota_cycles.py \
  --raw-root "$RAW" \
  --workload "$WORKLOAD" \
  --ckpt "$CKPT" \
  --max-len "$MAX_LEN" \
  --train-max-len "$TRAIN_MAX_LEN" \
  --max-windows "$MAX_WINDOWS" \
  --query-placement "$QUERY_PLACEMENT" \
  --dump-window-jsonl-dir "$OUTDIR" \
  > "$OUTDIR/run.log" 2>&1; then
  echo "[diag][error] eval failed; tail of $OUTDIR/run.log:" >&2
  tail -80 "$OUTDIR/run.log" >&2 || true
  exit 1
fi

DUMP="$OUTDIR/${WORKLOAD}.windows.jsonl"
ANALYSIS="$OUTDIR/analysis.txt"
if [[ ! -s "$DUMP" ]]; then
  echo "[diag][error] missing or empty dump: $DUMP" >&2
  exit 2
fi

"$PY" - "$DUMP" "$ANALYSIS" <<'PY'
import json
import math
import statistics as st
import sys

dump_path, out_path = sys.argv[1], sys.argv[2]

def finite(xs):
    return [float(x) for x in xs if x is not None and math.isfinite(float(x))]

def mean(xs):
    xs = finite(xs)
    return st.mean(xs) if xs else float("nan")

def median(xs):
    xs = sorted(finite(xs))
    return xs[len(xs) // 2] if xs else float("nan")

def quantile(xs, q):
    xs = sorted(finite(xs))
    if not xs:
        return float("nan")
    if len(xs) == 1:
        return xs[0]
    pos = q * (len(xs) - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return xs[lo]
    return xs[lo] * (hi - pos) + xs[hi] * (pos - lo)

def cv(xs):
    xs = finite(xs)
    if not xs:
        return float("nan")
    m = st.mean(xs)
    return st.pstdev(xs) / abs(m) if abs(m) > 1e-12 else float("nan")

def corr(a, b):
    a = finite(a)
    b = finite(b)
    if len(a) != len(b) or len(a) < 2:
        return float("nan")
    ma, mb = st.mean(a), st.mean(b)
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((y - mb) ** 2 for y in b)
    if va <= 1e-24 or vb <= 1e-24:
        return float("nan")
    return sum((x - ma) * (y - mb) for x, y in zip(a, b)) / math.sqrt(va * vb)

def summarize(name, xs):
    xs = finite(xs)
    if not xs:
        return f"{name}: n=0"
    return (
        f"{name}: n={len(xs)} mean={mean(xs):.6g} p10={quantile(xs, 0.10):.6g} "
        f"p50={median(xs):.6g} p90={quantile(xs, 0.90):.6g} "
        f"p95={quantile(xs, 0.95):.6g} min={min(xs):.6g} max={max(xs):.6g}"
    )

pred_cv = []
label_cv = []
pred_range = []
label_range = []
planned_counts_cv = []
actual_uops_cv = []
planned_counts_mean = []
actual_uops_mean = []
token_len_est = []
tail_skew = []
pred_label_corr = []
pred_count_corr = []
low_count_spread_32 = 0
low_count_spread_64 = 0
slow_hit = 0
fast_hit = 0
rank_windows = 0
rows = 0
sum_pred_cyc = 0.0
sum_label_cyc = 0.0
sum_uops = 0.0
mismatch_examples = []

with open(dump_path, "r", encoding="utf-8") as fh:
    for line in fh:
        if not line.strip():
            continue
        r = json.loads(line)
        cores = r.get("cores") or []
        if not cores:
            continue
        pred = [float(c["pred"]["cpi_uop"]) for c in cores]
        label = [float(c["label"].get("cpi_uop", float("nan"))) for c in cores]
        if any(not math.isfinite(x) for x in pred + label):
            continue
        counts = [int(r.get("planned_counts", {}).get(str(c["core_id"]), 0)) for c in cores]
        uops = [float(c["uops"]) for c in cores]

        rows += 1
        pred_cv.append(cv(pred))
        label_cv.append(cv(label))
        pred_range.append(max(pred) - min(pred))
        label_range.append(max(label) - min(label))
        planned_counts_cv.append(cv(counts))
        actual_uops_cv.append(cv(uops))
        planned_counts_mean.append(mean(counts))
        actual_uops_mean.append(mean(uops))
        token_len_est.append(float(r.get("fit", {}).get("token_len_est", float("nan"))))
        tail_skew.append(float(r.get("planner", {}).get("tail_skew", float("nan"))))
        pred_label_corr.append(corr(pred, label))
        pred_count_corr.append(corr(pred, counts))

        if max(counts) - min(counts) <= 32:
            low_count_spread_32 += 1
        if max(counts) - min(counts) <= 64:
            low_count_spread_64 += 1

        slow_hit += int(pred.index(max(pred)) == label.index(max(label)))
        fast_hit += int(pred.index(min(pred)) == label.index(min(label)))
        rank_windows += 1

        win_uops = sum(uops)
        sum_uops += win_uops
        sum_pred_cyc += sum(p * u for p, u in zip(pred, uops))
        sum_label_cyc += sum(y * u for y, u in zip(label, uops))

        mismatch_score = (cv(label) if math.isfinite(cv(label)) else 0.0) - (
            cv(pred) if math.isfinite(cv(pred)) else 0.0
        )
        mismatch_examples.append((
            mismatch_score,
            int(r.get("window", -1)),
            mean(pred),
            mean(label),
            cv(pred),
            cv(label),
            max(counts) - min(counts),
            max(uops) - min(uops),
        ))

agg_pred = sum_pred_cyc / sum_uops if sum_uops > 0 else float("nan")
agg_label = sum_label_cyc / sum_uops if sum_uops > 0 else float("nan")
agg_relerr = abs(agg_pred - agg_label) / abs(agg_label) if abs(agg_label) > 1e-12 else float("nan")

lines = []
lines.append(f"dump={dump_path}")
lines.append(f"windows_used={rows}")
lines.append(f"agg_pred_cpi_uop={agg_pred:.6g}")
lines.append(f"agg_label_cpi_uop={agg_label:.6g}")
lines.append(f"agg_relerr={agg_relerr:.6g}")
lines.append("")
lines.append(summarize("pred_core_cv", pred_cv))
lines.append(summarize("label_core_cv", label_cv))
lines.append(summarize("pred_core_range", pred_range))
lines.append(summarize("label_core_range", label_range))
lines.append(summarize("pred_label_corr", pred_label_corr))
lines.append("")
lines.append(summarize("planned_counts_cv", planned_counts_cv))
lines.append(summarize("actual_uops_cv", actual_uops_cv))
lines.append(summarize("planned_counts_mean_per_core", planned_counts_mean))
lines.append(summarize("actual_uops_mean_per_core", actual_uops_mean))
lines.append(summarize("token_len_est", token_len_est))
lines.append(summarize("planner_tail_skew", tail_skew))
lines.append(summarize("pred_vs_planned_count_corr", pred_count_corr))
lines.append("")
if rank_windows:
    lines.append(f"slowest_core_hit_rate={slow_hit / rank_windows:.6g}")
    lines.append(f"fastest_core_hit_rate={fast_hit / rank_windows:.6g}")
    lines.append(f"random_baseline_8core=0.125")
    lines.append(f"count_spread_le_32_frac={low_count_spread_32 / rank_windows:.6g}")
    lines.append(f"count_spread_le_64_frac={low_count_spread_64 / rank_windows:.6g}")
lines.append("")
lines.append("top_mismatch_windows:")
for score, win, pm, lm, pcv, lcv, cspread, uspread in sorted(mismatch_examples, reverse=True)[:10]:
    lines.append(
        f"  window={win} mismatch={score:.6g} pred_mean={pm:.6g} "
        f"label_mean={lm:.6g} pred_cv={pcv:.6g} label_cv={lcv:.6g} "
        f"planned_count_spread={cspread} actual_uop_spread={uspread:.6g}"
    )
lines.append("")
if mean(pred_cv) < 0.6 * mean(label_cv) and mean(planned_counts_cv) < 0.10:
    lines.append("diagnosis=predicted per-core CPI spread is much smaller than label spread; planner counts are nearly uniform.")
elif mean(planned_counts_cv) < 0.10:
    lines.append("diagnosis=planner counts are nearly uniform; inspect pred_core_cv vs label_core_cv to decide whether CPI collapse is the cause.")
else:
    lines.append("diagnosis=planner does allocate uneven counts; CPI collapse is not the only cause.")

text = "\n".join(lines) + "\n"
with open(out_path, "w", encoding="utf-8") as fh:
    fh.write(text)
print(text, end="")
PY

echo "[diag] run_log=$OUTDIR/run.log"
echo "[diag] dump=$DUMP"
echo "[diag] analysis=$ANALYSIS"
