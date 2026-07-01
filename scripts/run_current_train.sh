#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
exec bash scripts/run_v9_train_qwen3_0p6b.sh "$@"
