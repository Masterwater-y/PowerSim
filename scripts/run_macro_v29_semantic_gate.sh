#!/usr/bin/env bash
# Capacity-matched macro-v29 semantic gate.  This supersedes the historical
# scalar-CPI Phase-1 driver for the new 256-dynamic-macro contract.
set -euo pipefail

REPO=${REPO:-/data00/yinhaolang/LLMSim}
PY=${PY:-/data00/yinhaolang/infer/.venv/bin/python}
TORCHRUN=${TORCHRUN:-/data00/yinhaolang/infer/.venv/bin/torchrun}
GPUS=${GPUS:-1}
RUNS_ROOT=${RUNS_ROOT:-$REPO/ckpt/macro_v29_semantic_gate}
LOG_ROOT=${LOG_ROOT:-$REPO/logs/macro_v29_semantic_gate}
VARIANTS=${VARIANTS:-real,pseudo,mnemonic_shuffle,random_init,side_only,llm_only,register_rename}
STEPS=${STEPS:-200}
BATCH_SIZE=${BATCH_SIZE:-1}
CORES=${CORES:-1}
MAX_TRAIN_SOURCES=${MAX_TRAIN_SOURCES:-0}
MAX_VALIDATION_SOURCES=${MAX_VALIDATION_SOURCES:-0}
EVAL_BATCHES=${EVAL_BATCHES:-35}
MIN_WORKLOAD_CLUSTERS=${MIN_WORKLOAD_CLUSTERS:-7}
BOOTSTRAP_SAMPLES=${BOOTSTRAP_SAMPLES:-10000}
BOOTSTRAP_SEED=${BOOTSTRAP_SEED:-20260717}
BOOTSTRAP_CONFIDENCE=${BOOTSTRAP_CONFIDENCE:-0.95}
BASE_MODEL=${BASE_MODEL:-Qwen/Qwen2.5-Coder-1.5B-Instruct}

cd "$REPO"
mkdir -p "$RUNS_ROOT" "$LOG_ROOT"
export PYTHONPATH="$REPO:/data00/yinhaolang/TCSim:${PYTHONPATH:-}"
export TOKENIZERS_PARALLELISM=false
export HF_HUB_OFFLINE=${HF_HUB_OFFLINE:-1}
export TRANSFORMERS_OFFLINE=${TRANSFORMERS_OFFLINE:-1}

IFS=',' read -r -a VARIANT_LIST <<< "$VARIANTS"
for variant in "${VARIANT_LIST[@]}"; do
    output="$RUNS_ROOT/$variant"
    log="$LOG_ROOT/$variant.log"
    if [[ -f "$output/final_report.json" ]]; then
        if [[ -f "$output/run.json" ]] && "$PY" -c \
            'import json,sys; raise SystemExit(0 if json.load(open(sys.argv[1])).get("semantic_run_contract") == "macro-v29-semantic-run-v4" else 1)' \
            "$output/run.json"; then
            echo "[skip] $variant already complete at $output"
            continue
        fi
        echo "[stale] $variant uses an obsolete run contract at $output" >&2
        echo "Choose a fresh RUNS_ROOT instead of mixing evidence." >&2
        exit 4
    fi
    mkdir -p "$output"
    if [[ "$GPUS" -gt 1 ]]; then
        launcher=("$TORCHRUN" --standalone --nproc-per-node="$GPUS")
    else
        launcher=("$PY")
    fi
    echo "[macro semantic gate] variant=$variant steps=$STEPS"
    "${launcher[@]}" train/train_macro_v29.py \
        --semantic-variant "$variant" \
        --semantic-input-mode native_token \
        --base-model "$BASE_MODEL" \
        --freeze-backbone \
        --cores "$CORES" \
        --validation-split seed0_inference \
        --validation-workload-roles business_heldout \
        --max-train-sources "$MAX_TRAIN_SOURCES" \
        --max-validation-sources "$MAX_VALIDATION_SOURCES" \
        --batch-size "$BATCH_SIZE" \
        --max-steps "$STEPS" \
        --eval-batches "$EVAL_BATCHES" \
        --eval-every "$STEPS" \
        --output "$output" \
        2>&1 | tee "$log"
done

"$PY" eval/macro_v29_semantic_gate.py \
    --runs-root "$RUNS_ROOT" \
    --variants "$VARIANTS" \
    --min-workload-clusters "$MIN_WORKLOAD_CLUSTERS" \
    --bootstrap-samples "$BOOTSTRAP_SAMPLES" \
    --bootstrap-seed "$BOOTSTRAP_SEED" \
    --bootstrap-confidence "$BOOTSTRAP_CONFIDENCE" \
    --output "$LOG_ROOT/gate_report.json" \
    2>&1 | tee "$LOG_ROOT/gate_report.log"
