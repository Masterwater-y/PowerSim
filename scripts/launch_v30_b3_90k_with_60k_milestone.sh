#!/usr/bin/env bash
set -euo pipefail

ROOT=${ROOT:-/data00/yinhaolang/TCSim}
cd "$ROOT"

PY=${PY:-/data00/yinhaolang/infer/.venv/bin/python}
MANIFEST=${MANIFEST:-data/v30_branch_replay_dataset/manifest.json}
CONFIG=${CONFIG:-configs/v30_branch_b3_replay_history_100m.yaml}
OUT=${OUT:-ckpt/tcsim_v30_branch_b3_replay_history_100m_8gpu_60000}
GPUS=${GPUS:-0,1,2,3,4,5,6,7}
NPROC=${NPROC:-8}
MILESTONE_STEP=${MILESTONE_STEP:-60000}
TARGET_STEPS=${TARGET_STEPS:-90000}
RUN_NAME=${RUN_NAME:-tcsim_v30_branch_b3_100m_8gpu_90000_m60k}
MILESTONE_CKPT=${MILESTONE_CKPT:-$OUT/step_${MILESTONE_STEP}.pt}
CONTROLLER_LOG=${CONTROLLER_LOG:-logs/watchdog/${RUN_NAME}.nohup.log}
CONTROLLER_PID=${CONTROLLER_PID:-logs/watchdog/${RUN_NAME}.controller.pid}
CONTROLLER_LOCK=${CONTROLLER_LOCK:-logs/watchdog/${RUN_NAME}.controller.lock}

fail() {
  echo "[v30-b3-90k][ERROR] $*" >&2
  exit 2
}

checkpoint_step() {
  "$PY" - "$1" <<'PY'
import sys
import torch

path = sys.argv[1]
try:
    payload = torch.load(path, map_location="cpu", weights_only=False)
except TypeError:
    payload = torch.load(path, map_location="cpu")
if not isinstance(payload, dict):
    raise SystemExit(f"checkpoint is not a mapping: {path}")
print(int(payload.get("step") or 0))
PY
}

validate_common() {
  [[ -x "$PY" ]] || fail "missing Python: $PY"
  [[ -f "$MANIFEST" ]] || fail "missing manifest: $MANIFEST"
  [[ -f "$CONFIG" ]] || fail "missing config: $CONFIG"
  [[ -f data/v30_branch_replay_dataset/build_report.json ]] || \
    fail "missing v30 branch-cache build report"
  (( MILESTONE_STEP > 0 )) || fail "MILESTONE_STEP must be positive"
  (( TARGET_STEPS > MILESTONE_STEP )) || \
    fail "TARGET_STEPS must be greater than MILESTONE_STEP"
  IFS=',' read -r -a gpu_items <<< "$GPUS"
  (( ${#gpu_items[@]} == NPROC )) || \
    fail "GPUS count does not match NPROC: $GPUS vs $NPROC"
}

run_stage() {
  local stage_target=$1
  local stage_name="${RUN_NAME}_to${stage_target}"
  echo "[v30-b3-90k] starting stage target=$stage_target run_name=$stage_name"
  env \
    ROOT="$ROOT" \
    PY="$PY" \
    VARIANT=b3 \
    TRAIN_SCRIPT=scripts/run_v30_branch_ddp8.sh \
    MANIFEST="$MANIFEST" \
    CONFIG="$CONFIG" \
    OUT="$OUT" \
    TARGET_STEPS="$stage_target" \
    GPUS="$GPUS" \
    NPROC="$NPROC" \
    RUN_NAME="$stage_name" \
    SDPA_BACKEND="${SDPA_BACKEND:-auto}" \
    AMP_DTYPE="${AMP_DTYPE:-bf16}" \
    PROFILE_ATTENTION="${PROFILE_ATTENTION:-1}" \
    MONITOR_INTERVAL="${MONITOR_INTERVAL:-60}" \
    STALL_SECONDS="${STALL_SECONDS:-3600}" \
    MAX_RESTARTS="${MAX_RESTARTS:-20}" \
    NO_PROGRESS_LIMIT="${NO_PROGRESS_LIMIT:-3}" \
    OMP_NUM_THREADS="${OMP_NUM_THREADS:-4}" \
    NCCL_P2P_DISABLE="${NCCL_P2P_DISABLE:-0}" \
    NCCL_IB_DISABLE="${NCCL_IB_DISABLE:-1}" \
    PYTORCH_CUDA_ALLOC_CONF="${PYTORCH_CUDA_ALLOC_CONF:-expandable_segments:True}" \
    bash scripts/watch_mvp_train.sh
}

preserve_milestone() {
  local source="$OUT/last.pt"
  local temporary="${MILESTONE_CKPT}.tmp-$$"
  [[ -f "$source" ]] || fail "missing resumable checkpoint after milestone: $source"
  local source_step
  source_step=$(checkpoint_step "$source")
  (( source_step == MILESTONE_STEP )) || \
    fail "expected last.pt at step $MILESTONE_STEP, found $source_step"

  mkdir -p "$(dirname "$MILESTONE_CKPT")"
  rm -f "$temporary"
  cp --reflink=auto --preserve=mode,timestamps "$source" "$temporary"
  local copied_step
  copied_step=$(checkpoint_step "$temporary")
  if (( copied_step != MILESTONE_STEP )); then
    rm -f "$temporary"
    fail "copied milestone has wrong step: $copied_step"
  fi
  mv -f "$temporary" "$MILESTONE_CKPT"
  echo "[v30-b3-90k] preserved milestone step=$copied_step path=$MILESTONE_CKPT"
}

run_controller() {
  mkdir -p "$OUT" logs/watchdog
  exec 9>"$CONTROLLER_LOCK"
  flock -n 9 || fail "another controller holds $CONTROLLER_LOCK"
  printf '%s\n' "$$" >"$CONTROLLER_PID"
  trap 'rm -f "$CONTROLLER_PID"' EXIT

  local current_step=0
  if [[ -f "$OUT/last.pt" ]]; then
    current_step=$(checkpoint_step "$OUT/last.pt")
  fi
  echo "[v30-b3-90k] current_step=$current_step milestone=$MILESTONE_STEP target=$TARGET_STEPS"

  if (( current_step < MILESTONE_STEP )); then
    run_stage "$MILESTONE_STEP"
    preserve_milestone
    current_step=$MILESTONE_STEP
  elif [[ -f "$MILESTONE_CKPT" ]]; then
    local saved_step
    saved_step=$(checkpoint_step "$MILESTONE_CKPT")
    (( saved_step == MILESTONE_STEP )) || \
      fail "existing milestone has wrong step: $MILESTONE_CKPT step=$saved_step"
    echo "[v30-b3-90k] milestone already present: $MILESTONE_CKPT"
  elif (( current_step == MILESTONE_STEP )); then
    preserve_milestone
  else
    fail "training is already past $MILESTONE_STEP but $MILESTONE_CKPT is missing"
  fi

  if (( current_step < TARGET_STEPS )); then
    run_stage "$TARGET_STEPS"
  fi

  local final_step
  final_step=$(checkpoint_step "$OUT/last.pt")
  (( final_step >= TARGET_STEPS )) || \
    fail "controller ended below target: step=$final_step target=$TARGET_STEPS"
  echo "[v30-b3-90k] PASS final_step=$final_step milestone=$MILESTONE_CKPT"
}

validate_common

case "${1:-}" in
  --controller)
    run_controller
    ;;
  --check)
    current_step=0
    [[ ! -f "$OUT/last.pt" ]] || current_step=$(checkpoint_step "$OUT/last.pt")
    echo "[v30-b3-90k] current_step=$current_step milestone=$MILESTONE_STEP target=$TARGET_STEPS"
    echo "[v30-b3-90k] out=$OUT"
    echo "[v30-b3-90k] milestone_checkpoint=$MILESTONE_CKPT"
    echo "[v30-b3-90k] controller_log=$CONTROLLER_LOG"
    ;;
  "")
    mkdir -p "$OUT" logs/watchdog
    if [[ -f "$CONTROLLER_PID" ]]; then
      existing_pid=$(<"$CONTROLLER_PID")
      if [[ "$existing_pid" =~ ^[0-9]+$ ]] && kill -0 "$existing_pid" 2>/dev/null; then
        fail "controller is already running: pid=$existing_pid log=$CONTROLLER_LOG"
      fi
    fi
    nohup env \
      ROOT="$ROOT" PY="$PY" MANIFEST="$MANIFEST" CONFIG="$CONFIG" \
      OUT="$OUT" GPUS="$GPUS" NPROC="$NPROC" \
      MILESTONE_STEP="$MILESTONE_STEP" TARGET_STEPS="$TARGET_STEPS" \
      RUN_NAME="$RUN_NAME" MILESTONE_CKPT="$MILESTONE_CKPT" \
      CONTROLLER_LOG="$CONTROLLER_LOG" CONTROLLER_PID="$CONTROLLER_PID" \
      CONTROLLER_LOCK="$CONTROLLER_LOCK" \
      SDPA_BACKEND="${SDPA_BACKEND:-auto}" AMP_DTYPE="${AMP_DTYPE:-bf16}" \
      PROFILE_ATTENTION="${PROFILE_ATTENTION:-1}" \
      MONITOR_INTERVAL="${MONITOR_INTERVAL:-60}" \
      STALL_SECONDS="${STALL_SECONDS:-3600}" \
      MAX_RESTARTS="${MAX_RESTARTS:-20}" \
      NO_PROGRESS_LIMIT="${NO_PROGRESS_LIMIT:-3}" \
      OMP_NUM_THREADS="${OMP_NUM_THREADS:-4}" \
      NCCL_P2P_DISABLE="${NCCL_P2P_DISABLE:-0}" \
      NCCL_IB_DISABLE="${NCCL_IB_DISABLE:-1}" \
      PYTORCH_CUDA_ALLOC_CONF="${PYTORCH_CUDA_ALLOC_CONF:-expandable_segments:True}" \
      bash "$0" --controller >"$CONTROLLER_LOG" 2>&1 &
    controller_pid=$!
    printf '%s\n' "$controller_pid" >"$CONTROLLER_PID"
    echo "[v30-b3-90k] started controller_pid=$controller_pid"
    echo "[v30-b3-90k] log=$CONTROLLER_LOG"
    echo "[v30-b3-90k] milestone=$MILESTONE_CKPT"
    echo "[v30-b3-90k] final=$OUT/last.pt"
    ;;
  *)
    echo "usage: $0 [--check|--controller]" >&2
    exit 2
    ;;
esac
