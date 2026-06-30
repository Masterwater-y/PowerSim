#!/usr/bin/env bash
# Watch and auto-resume the v12 Qwen3-4B 8-GPU training run.
#
# Resume policy:
#   1. Prefer OUT/head_latest.pt + OUT/lora_latest/.
#   2. Fall back to OUT/head_best.pt + OUT/lora_best/ for old checkpoints.
#   3. Resume with --step-offset and --skip-train-batches equal to the saved
#      global step, so logs/checkpoints keep the 0..TARGET_STEPS numbering.
set -uo pipefail

ROOT=${ROOT:-/data00/yinhaolang/LLMSim}
cd "$ROOT" || exit 1

PY=${PY:-/data00/yinhaolang/infer/.venv/bin/python}
TORCHRUN=${TORCHRUN:-/data00/yinhaolang/infer/.venv/bin/torchrun}

DATA=${DATA:-data/windows_v12_summary_tq_train600_seedA_c01_c04_c08_c16/windows.jsonl}
OUT=${OUT:-ckpt/v12_summary_qwen3_4b_c01_c04_c08_c16_8000}
BASE_MODEL=${BASE_MODEL:-Qwen/Qwen3-4B}
TARGET_STEPS=${TARGET_STEPS:-8000}

GPUS=${GPUS:-0,1,2,3,4,5,6,7}
NPROC=${NPROC:-8}
BS=${BS:-1}
GRAD_ACCUM=${GRAD_ACCUM:-1}
MAX_LEN=${MAX_LEN:-32768}
VAL_FRAC=${VAL_FRAC:-0.15}
LOG_EVERY=${LOG_EVERY:-20}
EVAL_EVERY=${EVAL_EVERY:-500}
EVAL_BATCHES=${EVAL_BATCHES:-200}
NUM_WORKERS=${NUM_WORKERS:-2}

MONITOR_INTERVAL=${MONITOR_INTERVAL:-60}
STALL_SECONDS=${STALL_SECONDS:-1800}
MAX_RESTARTS=${MAX_RESTARTS:-20}
NO_PROGRESS_LIMIT=${NO_PROGRESS_LIMIT:-3}

export TMPDIR=${TMPDIR:-$ROOT/tmp}
export HF_HUB_OFFLINE=${HF_HUB_OFFLINE:-1}
export TOKENIZERS_PARALLELISM=${TOKENIZERS_PARALLELISM:-false}
export OMP_NUM_THREADS=${OMP_NUM_THREADS:-4}
export NCCL_P2P_DISABLE=${NCCL_P2P_DISABLE:-0}
export NCCL_IB_DISABLE=${NCCL_IB_DISABLE:-1}
export PYTORCH_CUDA_ALLOC_CONF=${PYTORCH_CUDA_ALLOC_CONF:-expandable_segments:True}

mkdir -p "$TMPDIR" "$OUT" logs logs/watchdog

RUN_NAME=${RUN_NAME:-$(basename "$OUT")}
LOCK_FILE="logs/watchdog/${RUN_NAME}.lock"
PID_FILE="logs/watchdog/${RUN_NAME}.watch.pid"
CURRENT_LOG="logs/${RUN_NAME}.current.log"

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  echo "[watch][ERROR] another watcher is already running for $RUN_NAME"
  exit 2
fi
echo "$$" > "$PID_FILE"

ckpt_info() {
  "$PY" - "$OUT" <<'PY'
import os
import sys
import torch

out = sys.argv[1]
for kind in ("latest", "best"):
    head = os.path.join(out, f"head_{kind}.pt")
    lora = os.path.join(out, f"lora_{kind}")
    if os.path.isfile(head) and os.path.isdir(lora):
        try:
            sd = torch.load(head, map_location="cpu")
            print(kind, int(sd.get("step") or 0), sd.get("val_loss", "nan"))
        except Exception as exc:
            print("error", 0, str(exc).replace(" ", "_"))
        raise SystemExit
print("none", 0, "nan")
PY
}

last_log_step() {
  local log=$1
  [[ -f "$log" ]] || { echo 0; return; }
  "$PY" - "$log" <<'PY'
import re
import sys

path = sys.argv[1]
try:
    with open(path, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - 1_000_000))
        text = f.read().decode("utf-8", "ignore")
except FileNotFoundError:
    print(0)
    raise SystemExit
steps = [int(m.group(1)) for m in re.finditer(r"\[(?:eval )?step (\d+)\]", text)]
print(max(steps) if steps else 0)
PY
}

terminate_group() {
  local pid=$1
  echo "[watch] terminating process group -$pid"
  kill -TERM "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
  sleep 30
  if kill -0 "$pid" 2>/dev/null; then
    echo "[watch][WARN] process still alive after TERM; sending KILL"
    kill -KILL "-$pid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null || true
  fi
}

echo "[watch] run_name=$RUN_NAME"
echo "[watch] out=$OUT"
echo "[watch] data=$DATA"
echo "[watch] base_model=$BASE_MODEL target_steps=$TARGET_STEPS"
echo "[watch] gpus=$GPUS nproc=$NPROC bs=$BS accum=$GRAD_ACCUM max_len=$MAX_LEN"
echo "[watch] eval_every=$EVAL_EVERY eval_batches=$EVAL_BATCHES monitor_interval=${MONITOR_INTERVAL}s stall=${STALL_SECONDS}s"

restart_count=0
no_progress_count=0

while true; do
  read -r ckpt_kind ckpt_step ckpt_val <<<"$(ckpt_info)"
  if [[ "$ckpt_step" =~ ^[0-9]+$ ]] && (( ckpt_step >= TARGET_STEPS )); then
    echo "[watch] target reached: ckpt_kind=$ckpt_kind step=$ckpt_step val_loss=$ckpt_val"
    exit 0
  fi

  if (( restart_count >= MAX_RESTARTS )); then
    echo "[watch][ERROR] reached MAX_RESTARTS=$MAX_RESTARTS before target"
    exit 1
  fi

  if [[ ! "$ckpt_step" =~ ^[0-9]+$ ]]; then
    echo "[watch][WARN] invalid checkpoint step '$ckpt_step'; restart from 0"
    ckpt_kind=none
    ckpt_step=0
  fi

  remaining=$((TARGET_STEPS - ckpt_step))
  attempt=$((restart_count + 1))
  run_log="logs/${RUN_NAME}.attempt_${attempt}_from${ckpt_step}.log"
  ln -sfn "$(basename "$run_log")" "$CURRENT_LOG"

  train_args=(
    --data "$DATA"
    --out "$OUT"
    --base-model "$BASE_MODEL"
    --steps "$remaining"
    --bs "$BS"
    --grad-accum "$GRAD_ACCUM"
    --max-len "$MAX_LEN"
    --val-frac "$VAL_FRAC"
    --log-every "$LOG_EVERY"
    --eval-every "$EVAL_EVERY"
    --eval-batches "$EVAL_BATCHES"
    --num-workers "$NUM_WORKERS"
    --step-offset "$ckpt_step"
  )
  if [[ "$ckpt_kind" != "none" && "$ckpt_kind" != "error" && "$ckpt_step" -gt 0 ]]; then
    train_args+=(--init-ckpt "$OUT" --skip-train-batches "$ckpt_step")
  fi

  echo "[watch] attempt=$attempt start_step=$ckpt_step remaining=$remaining ckpt_kind=$ckpt_kind val_loss=$ckpt_val"
  echo "[watch] train_log=$run_log current_log=$CURRENT_LOG"

  setsid env \
    TMPDIR="$TMPDIR" \
    HF_HUB_OFFLINE="$HF_HUB_OFFLINE" \
    TOKENIZERS_PARALLELISM="$TOKENIZERS_PARALLELISM" \
    OMP_NUM_THREADS="$OMP_NUM_THREADS" \
    NCCL_P2P_DISABLE="$NCCL_P2P_DISABLE" \
    NCCL_IB_DISABLE="$NCCL_IB_DISABLE" \
    PYTORCH_CUDA_ALLOC_CONF="$PYTORCH_CUDA_ALLOC_CONF" \
    CUDA_VISIBLE_DEVICES="$GPUS" \
    "$TORCHRUN" --standalone --nproc_per_node="$NPROC" \
      train/train_lora.py "${train_args[@]}" \
      > "$run_log" 2>&1 &
  child=$!
  echo "[watch] spawned torchrun pid=$child"

  while kill -0 "$child" 2>/dev/null; do
    sleep "$MONITOR_INTERVAL"
    now=$(date +%s)
    mtime=$(stat -c %Y "$run_log" 2>/dev/null || echo "$now")
    age=$((now - mtime))
    log_step=$(last_log_step "$run_log")
    read -r live_kind live_step live_val <<<"$(ckpt_info)"
    echo "[watch] alive pid=$child log_step=$log_step ckpt=${live_kind}:${live_step} val_loss=$live_val log_age=${age}s"
    if (( age > STALL_SECONDS )); then
      echo "[watch][WARN] no log update for ${age}s; restarting from latest checkpoint"
      terminate_group "$child"
      break
    fi
  done

  wait "$child"
  rc=$?
  read -r new_kind new_step new_val <<<"$(ckpt_info)"
  echo "[watch] torchrun exited rc=$rc checkpoint=${new_kind}:${new_step} val_loss=$new_val"

  if [[ "$new_step" =~ ^[0-9]+$ ]] && (( new_step >= TARGET_STEPS )); then
    echo "[watch] target reached after attempt=$attempt"
    exit 0
  fi

  if [[ "$new_step" =~ ^[0-9]+$ ]] && (( new_step <= ckpt_step )); then
    no_progress_count=$((no_progress_count + 1))
    echo "[watch][WARN] no checkpoint progress (${no_progress_count}/${NO_PROGRESS_LIMIT})"
    if (( no_progress_count >= NO_PROGRESS_LIMIT )); then
      echo "[watch][ERROR] repeated restarts without checkpoint progress; inspect $run_log"
      exit 1
    fi
  else
    no_progress_count=0
  fi

  restart_count=$((restart_count + 1))
  sleep 20
done
