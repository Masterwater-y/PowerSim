#!/usr/bin/env bash
# Generic watchdog for LLMSim training scripts.
#
# Usage:
#   TRAIN_SCRIPT=scripts/run_xxx.sh OUT=ckpt/xxx TARGET_STEPS=8000 \
#     nohup bash scripts/watch_train.sh > logs/watchdog/xxx.nohup.log 2>&1 &
#
# Extra positional args are forwarded to TRAIN_SCRIPT:
#   TRAIN_SCRIPT=scripts/run_xxx.sh OUT=ckpt/xxx bash scripts/watch_train.sh -- --eval-batches 20
#
# Resume policy:
#   1. Prefer highest OUT/step_XXXXXX snapshot with head_best.pt + lora_best/.
#   2. Fall back to OUT/head_best.pt + OUT/lora_best/.
#   3. Resume with INIT_CKPT, STEP_OFFSET, SKIP_TRAIN_BATCHES set to checkpoint step.
set -uo pipefail

trap '' HUP

ROOT=${ROOT:-/data00/yinhaolang/LLMSim}
cd "$ROOT" || exit 1

PY=${PY:-/data00/yinhaolang/infer/.venv/bin/python}
TRAIN_SCRIPT=${TRAIN_SCRIPT:-}
OUT=${OUT:-}
TARGET_STEPS=${TARGET_STEPS:-${STEPS:-8000}}

if [[ -z "$TRAIN_SCRIPT" ]]; then
  echo "[watch][ERROR] TRAIN_SCRIPT is required" >&2
  exit 2
fi
if [[ -z "$OUT" ]]; then
  echo "[watch][ERROR] OUT is required" >&2
  exit 2
fi

if [[ "${1:-}" == "--" ]]; then
  shift
fi
TRAIN_ARGS=("$@")

MONITOR_INTERVAL=${MONITOR_INTERVAL:-60}
STALL_SECONDS=${STALL_SECONDS:-3600}
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

RUN_NAME=${RUN_NAME:-$(basename "$OUT")_watch}
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
import glob
import os
import re
import sys
import torch

out = sys.argv[1]
items = []

def add_candidate(kind, path):
    head = os.path.join(path, "head_best.pt")
    lora = os.path.join(path, "lora_best")
    if not (os.path.isfile(head) and os.path.isdir(lora)):
        return
    try:
        sd = torch.load(head, map_location="cpu")
        step = int(sd.get("step") or 0)
        val = sd.get("val_loss", "nan")
        items.append((step, kind, path, val))
    except Exception as exc:
        items.append((0, "error", path, str(exc).replace(" ", "_")))

for path in glob.glob(os.path.join(out, "step_*")):
    if os.path.isdir(path) and re.search(r"step_\d+$", path):
        add_candidate("snapshot", path)

add_candidate("best", out)

if not items:
    print("none", "-", 0, "nan")
    raise SystemExit

items.sort(key=lambda x: (x[0], 1 if x[1] == "snapshot" else 0))
step, kind, path, val = items[-1]
print(kind, path, step, val)
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

cleanup() {
  rm -f "$PID_FILE"
}
trap cleanup EXIT

echo "[watch] run_name=$RUN_NAME"
echo "[watch] train_script=$TRAIN_SCRIPT"
echo "[watch] out=$OUT target_steps=$TARGET_STEPS"
echo "[watch] monitor_interval=${MONITOR_INTERVAL}s stall=${STALL_SECONDS}s"
if ((${#TRAIN_ARGS[@]})); then
  printf '[watch] forwarded_args='
  printf '%q ' "${TRAIN_ARGS[@]}"
  printf '\n'
fi

restart_count=0
no_progress_count=0

while true; do
  read -r ckpt_kind ckpt_path ckpt_step ckpt_val <<<"$(ckpt_info)"
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
    ckpt_path=-
    ckpt_step=0
  fi

  remaining=$((TARGET_STEPS - ckpt_step))
  attempt=$((restart_count + 1))
  run_log="logs/${RUN_NAME}.attempt_${attempt}_from${ckpt_step}.log"
  ln -sfn "$(basename "$run_log")" "$CURRENT_LOG"

  echo "[watch] attempt=$attempt start_step=$ckpt_step remaining=$remaining ckpt_kind=$ckpt_kind ckpt_path=$ckpt_path val_loss=$ckpt_val"
  echo "[watch] train_log=$run_log current_log=$CURRENT_LOG"

  run_env=(
    ROOT="$ROOT"
    TMPDIR="$TMPDIR"
    OUT="$OUT"
    STEPS="$remaining"
    HF_HUB_OFFLINE="$HF_HUB_OFFLINE"
    TOKENIZERS_PARALLELISM="$TOKENIZERS_PARALLELISM"
    OMP_NUM_THREADS="$OMP_NUM_THREADS"
    NCCL_P2P_DISABLE="$NCCL_P2P_DISABLE"
    NCCL_IB_DISABLE="$NCCL_IB_DISABLE"
    PYTORCH_CUDA_ALLOC_CONF="$PYTORCH_CUDA_ALLOC_CONF"
  )
  if [[ "$ckpt_kind" != "none" && "$ckpt_kind" != "error" && "$ckpt_step" -gt 0 ]]; then
    run_env+=(
      INIT_CKPT="$ckpt_path"
      SKIP_TRAIN_BATCHES="$ckpt_step"
      STEP_OFFSET="$ckpt_step"
      RESET_LOSS_STATE=0
    )
  fi

  setsid env "${run_env[@]}" bash "$TRAIN_SCRIPT" "${TRAIN_ARGS[@]}" > "$run_log" 2>&1 &
  child=$!
  echo "[watch] spawned train pid=$child"

  while kill -0 "$child" 2>/dev/null; do
    sleep "$MONITOR_INTERVAL"
    now=$(date +%s)
    mtime=$(stat -c %Y "$run_log" 2>/dev/null || echo "$now")
    age=$((now - mtime))
    log_step=$(last_log_step "$run_log")
    read -r live_kind live_path live_step live_val <<<"$(ckpt_info)"
    echo "[watch] alive pid=$child log_step=$log_step ckpt=${live_kind}:${live_step} val_loss=$live_val log_age=${age}s"
    if (( age > STALL_SECONDS )); then
      echo "[watch][WARN] no log update for ${age}s; restarting from latest checkpoint"
      terminate_group "$child"
      break
    fi
  done

  wait "$child"
  rc=$?
  read -r new_kind new_path new_step new_val <<<"$(ckpt_info)"
  echo "[watch] train exited rc=$rc checkpoint=${new_kind}:${new_step} val_loss=$new_val path=$new_path"

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
