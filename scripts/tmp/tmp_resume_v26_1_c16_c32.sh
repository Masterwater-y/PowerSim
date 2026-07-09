#!/usr/bin/env bash
set -euo pipefail

cd /data00/yinhaolang/TSim
mkdir -p logs

if ps -ef | grep -E 'build_v26\.1_balanced|build_windows\.py|prepare_dataset_cache\.py' | grep -v grep >/dev/null; then
  echo "[error] existing build/cache process found; stop it before launching resume" >&2
  ps -ef | grep -E 'build_v26\.1_balanced|build_windows\.py|prepare_dataset_cache\.py' | grep -v grep >&2
  exit 1
fi

rmdir tmp/build_v26.1_balanced.lock 2>/dev/null || true
rm -rf data/windows_v26.1_balanced_c16

LOG="logs/build_v26.1_balanced_resume_c16_c32_$(date +%Y%m%d_%H%M%S).nohup.log"
MALLOC_ARENA_MAX=2 \
PYTHONUNBUFFERED=1 \
MAX_LEN=32768 \
TARGET_WINDOWS=10000 \
RUN_CORES=c16,c32 \
CORE_PARALLEL=1 \
JOBS_C16=17 \
JOBS_C32=17 \
CACHE_JOBS=96 \
CLEAN=1 \
BUILD_CACHE=1 \
DIRECT_CACHE=1 \
DIRECT_CACHE_SHARD_SIZE=512 \
nohup bash scripts/build_v26.1_balanced.sh > "$LOG" 2>&1 &

echo "pid=$!"
echo "log=$LOG"
echo "tail -f $LOG logs/build_v26.1_balanced_c16.log logs/build_v26.1_balanced_c32.log"
