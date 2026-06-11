#!/usr/bin/env bash
# taogen/scripts/validate_workload.sh
# V9.5 hold-out 端到端验证一键脚本：
#   gem5 detailed run
#     -> ref_sim 重放 (8-field commit oracle)
#     -> oracle vs ref_sim 17/17 bit-exact 校验
#     -> build_inference_input.py 构造模型推理输入
#     -> ml/infer.py 跑模型预测 (fetch/exec/mispred)
#     -> tools/synthesize_cpi.py 合成 sum/sum 全局 CPI + per-core 对比
#     -> pmu_report.py（含 model-derived mispred section）
#     -> SUMMARY.txt
#
# 用法：
#   bash scripts/validate_workload.sh \
#        --name <NAME> --bin <WORKLOAD_BIN> --args "<a1> <a2> ..." \
#        --ckpt <ckpt_path> --vocab <vocab.json> \
#        [--out-base <dir>] [--num-cores 4]
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

GEM5="${GEM5:-$REPO/gem5/build/X86_MESI_Three_Level/gem5.opt}"
CFG="$REPO/configs/run_mt_mvp.py"
REFSIM="$REPO/mesi_ref_sim/build/mesi_ref_sim"
COMPARE="$REPO/mesi_ref_sim/scripts/compare_oracle.py"
COMPARE_I="$REPO/mesi_ref_sim/scripts/compare_ifetch.py"
PMU="$REPO/mesi_ref_sim/scripts/pmu_report.py"
BUILD_INFER="$REPO/tools/build_inference_input.py"
INFER="$REPO/ml/infer.py"
SYNTH_CPI="$REPO/tools/synthesize_cpi.py"

# torch 装在 pyenv 3.11.14
PYBIN="${PYBIN:-/root/.pyenv/versions/3.11.14/bin/python3.11}"

# refsim 链接 gcc-11 libstdc++
export LD_LIBRARY_PATH="/opt/gcc-11/lib64:${LD_LIBRARY_PATH:-}"

NUM_CORES=4
NAME=""
BIN=""
WLARGS=""
CKPT=""
VOCAB=""
OUT_BASE=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --name)        NAME="$2"; shift 2 ;;
    --bin)         BIN="$2"; shift 2 ;;
    --args)        WLARGS="$2"; shift 2 ;;
    --ckpt)        CKPT="$2"; shift 2 ;;
    --vocab)       VOCAB="$2"; shift 2 ;;
    --out-base)    OUT_BASE="$2"; shift 2 ;;
    --num-cores)   NUM_CORES="$2"; shift 2 ;;
    *) echo "unknown arg: $1"; exit 2 ;;
  esac
done

if [[ -z "$NAME" || -z "$BIN" || -z "$CKPT" ]]; then
  echo "usage: $0 --name <NAME> --bin <BIN> --args \"<a1> ...\" --ckpt <ckpt> [--vocab <vocab.json>]"
  exit 2
fi

OUT_BASE="${OUT_BASE:-$REPO/tmp/validate_$(date +%Y%m%d_%H%M%S)}"
OUT="$OUT_BASE/$NAME"
mkdir -p "$OUT"
SUMMARY="$OUT/SUMMARY.txt"
: > "$SUMMARY"

log() { echo "$@" | tee -a "$SUMMARY"; }

log "=== validate_workload: $NAME ==="
log "out=$OUT"
log "ckpt=$CKPT"
log "vocab=$VOCAB"
log "bin=$BIN  args=$WLARGS  num_cores=$NUM_CORES"

# ----- 1) gem5 detailed run -----
log ""
log "[1/6] gem5 detailed run ..."
# shellcheck disable=SC2086
"$GEM5" --outdir="$OUT" "$CFG" \
    --cmd "$BIN" --workload-args $WLARGS --num-cores "$NUM_CORES" \
    > "$OUT/gem5.log" 2>&1
log "  gem5 done -> $OUT/stats.txt"

# ----- 2) merge mem_events + ref_sim 重放 -----
log ""
log "[2/6] merge mem_events + ref_sim replay ..."
cat "$OUT/tao_trace/"*.mem_events.jsonl 2>/dev/null \
    | "$PYBIN" -c "
import json, sys
rows=[]
for ln in sys.stdin:
    s=ln.strip()
    if not s.startswith('{'): continue
    try: rows.append(json.loads(s))
    except Exception: pass
rows.sort(key=lambda r:(r.get('commit_tick',0), r.get('seq',0)))
for r in rows: print(json.dumps(r, separators=(',',':')))
" > "$OUT/mem_events.merged.jsonl"

PROFILE="$OUT/uarch_profile.json"
"$REFSIM" "$PROFILE" "$OUT/mem_events.merged.jsonl" "$OUT/pred.jsonl" \
    2> "$OUT/refsim.log"
log "  ref_sim done -> $OUT/pred.jsonl"

# ----- 3) oracle vs ref_sim bit-exact 校验 -----
log ""
log "[3/6] compare oracle vs ref_sim ..."
"$PYBIN" "$COMPARE"   "$OUT/mem_events.merged.jsonl" "$OUT/pred.jsonl" > "$OUT/compare.log"        2>&1 || true
"$PYBIN" "$COMPARE_I" "$OUT/mem_events.merged.jsonl" "$OUT/pred.jsonl" > "$OUT/compare.ifetch.log" 2>&1 || true
DM=$(grep -E "^matched\s"     "$OUT/compare.log"        | head -1 || true)
DX=$(grep -E "^mismatched\s"  "$OUT/compare.log"        | head -1 || true)
IM=$(grep -E "^matched\s"     "$OUT/compare.ifetch.log" | head -1 || true)
IX=$(grep -E "^mismatched\s"  "$OUT/compare.ifetch.log" | head -1 || true)
log "  dside: $DM | $DX"
log "  iside: $IM | $IX"

# ----- 4) build inference input -----
log ""
log "[4/6] build inference input ..."
INFER_IN="$OUT/infer_in.jsonl"
"$PYBIN" "$BUILD_INFER" \
    --detailed-dir "$OUT/tao_trace" \
    --pred-jsonl   "$OUT/pred.jsonl" \
    --workload     "$NAME" \
    --out          "$INFER_IN" 2>&1 | tee "$OUT/build_infer.log" >/dev/null
N_INFER=$(wc -l < "$INFER_IN")
log "  infer_in rows = $N_INFER"

# ----- 5) ml/infer.py -----
log ""
log "[5/6] ml/infer.py ..."
PRED_OUT="$OUT/model_pred.jsonl"
VOCAB_ARGS=()
[[ -n "$VOCAB" ]] && VOCAB_ARGS+=(--vocab-json "$VOCAB")
"$PYBIN" "$INFER" \
    --ckpt "$CKPT" \
    --input-jsonl "$INFER_IN" \
    --out-jsonl   "$PRED_OUT" \
    "${VOCAB_ARGS[@]}" \
    2> "$OUT/infer.log"
N_PRED=$(wc -l < "$PRED_OUT")
log "  model_pred rows = $N_PRED"

# ----- 6) synthesize CPI + PMU report -----
log ""
log "[6/6] synthesize CPI + PMU report ..."
CPI_JSON="$OUT/cpi_report.json"
"$PYBIN" "$SYNTH_CPI" \
    --pred-jsonl  "$PRED_OUT" \
    --input-jsonl "$INFER_IN" \
    --gem5-stats  "$OUT/stats.txt" \
    --require-inst-match \
    --out-json    "$CPI_JSON" \
    > "$OUT/cpi_report.log" 2>&1

"$PYBIN" "$PMU" \
    "$OUT/mem_events.merged.jsonl" \
    "$OUT/pred.jsonl" \
    "$OUT/stats.txt" \
    --uarch-profile "$PROFILE" \
    --model-pred-jsonl "$PRED_OUT" \
    > "$OUT/pmu.log" 2>&1 || true

# ----- SUMMARY -----
log ""
log "=== SUMMARY: $NAME ==="
log "[bit-exact d-side]  $DM | $DX"
log "[bit-exact i-side]  $IM | $IX"
log ""
log "--- CPI synthesis (from $CPI_JSON) ---"
grep -E '^(=== |cycles_pred|n_macro_pred|CPI_pred|CPI_truth|CPI_err|--- per-core|core|^\s*[0-9]+ )' \
     "$OUT/cpi_report.log" | head -40 | tee -a "$SUMMARY" >/dev/null
log ""
log "--- PMU report tail ---"
tail -20 "$OUT/pmu.log" | tee -a "$SUMMARY" >/dev/null

log ""
log "ALL ARTIFACTS -> $OUT"
echo "[done] $NAME -> $OUT"
