#!/usr/bin/env bash
# tmp_build_cache_and_train_20k_8gpu.sh — one-shot combined launcher:
#   1) build the macro-v29 native-token cache in the foreground (blocking)
#   2) hand off to tmp_run_macro_v29_20k_8gpu.sh for the 8-GPU 20k-step run
#
# Both stages write every temporary artefact under $REPO/tmp so /tmp stays
# clean.
#
# Usage:
#
#   cd /data00/yinhaolang/LLMSim && \
#     mkdir -p tmp/macro_v29_real_8gpu_20k && \
#     nohup bash scripts/tmp_build_cache_and_train_20k_8gpu.sh \
#       > tmp/macro_v29_real_8gpu_20k/nohup.log 2>&1 & \
#     echo "launcher_pid=$!"
#
# Env vars override behaviour of each stage (see the two underlying scripts).
# Notable ones:
#   FORCE_REBUILD_CACHE=1   force the cache builder even if a manifest exists
#   CACHE_NUM_WORKERS       parallel workers used only during cache build
#   TARGET_STEPS, GPUS, MASTER_PORT, BATCH_SIZE, ...  forwarded to training

set -euo pipefail

REPO=/data00/yinhaolang/LLMSim
cd "$REPO"

CACHE_ROOT=${CACHE_ROOT:-$REPO/data/v29_macro_token_cache}
VARIANTS=${VARIANTS:-real}
SPLITS=${SPLITS:-train,validation,development_heldout,seed0_inference,deployment_inference,final_untouched}
CACHE_NUM_WORKERS=${CACHE_NUM_WORKERS:-8}
FORCE_REBUILD_CACHE=${FORCE_REBUILD_CACHE:-0}

RUN_NAME=${RUN_NAME:-macro_v29_real_8gpu_20k}
TMP_ROOT=$REPO/tmp/$RUN_NAME
LOG_DIR=$TMP_ROOT/logs
mkdir -p "$TMP_ROOT" "$LOG_DIR"

STAMP=$(date +%Y%m%dT%H%M%S)
COMBINED_LOG="$LOG_DIR/combined_${STAMP}.log"
CACHE_LOG="$LOG_DIR/cache_build_${STAMP}.log"

log() { echo "[$(date '+%Y-%m-%dT%H:%M:%S')] $*" | tee -a "$COMBINED_LOG"; }

log "== combined build-cache + 8-GPU 20k-step launcher =="
log "  repo         = $REPO"
log "  cache_root   = $CACHE_ROOT"
log "  variants     = $VARIANTS"
log "  splits       = $SPLITS"
log "  workers      = $CACHE_NUM_WORKERS  (cache build only)"
log "  force        = $FORCE_REBUILD_CACHE"
log "  combined_log = $COMBINED_LOG"
log "  cache_log    = $CACHE_LOG"

need_build=0
if [[ "$FORCE_REBUILD_CACHE" == "1" ]]; then
  log "cache build: forced by FORCE_REBUILD_CACHE=1"
  need_build=1
elif [[ ! -f "$CACHE_ROOT/manifest.json" ]]; then
  log "cache build: manifest missing ($CACHE_ROOT/manifest.json) — building"
  need_build=1
else
  log "cache build: manifest present — skipping (set FORCE_REBUILD_CACHE=1 to rebuild)"
fi

if [[ "$need_build" == "1" ]]; then
  log "stage 1: build macro-v29 native-token cache"
  CACHE_ROOT="$CACHE_ROOT" \
  VARIANTS="$VARIANTS" \
  SPLITS="$SPLITS" \
  NUM_WORKERS="$CACHE_NUM_WORKERS" \
  FORCE="$FORCE_REBUILD_CACHE" \
  bash "$REPO/scripts/build_macro_v29_token_cache.sh" \
    > "$CACHE_LOG" 2>&1
  cache_rc=$?
  log "stage 1 finished rc=$cache_rc  cache_log=$CACHE_LOG"
  if [[ $cache_rc -ne 0 ]]; then
    log "[ERROR] cache build failed; aborting training launch"
    exit $cache_rc
  fi
fi

if [[ ! -f "$CACHE_ROOT/manifest.json" ]]; then
  log "[ERROR] cache manifest still missing after build attempt; aborting"
  exit 2
fi

log "stage 2: launch 8-GPU ${TARGET_STEPS:-20000}-step training (via tmp_run_macro_v29_20k_8gpu.sh)"
# Foreground exec — nohup covers the whole combined launcher already, so we
# do not want the training launcher to spawn another detached process.
exec bash "$REPO/scripts/tmp_run_macro_v29_20k_8gpu.sh"
