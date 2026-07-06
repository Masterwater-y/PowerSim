#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
exec bash scripts/run_v9_eval_sweep.sh "$@"
