#!/usr/bin/env bash
# One command for the c8 mainline: build/verify frozen semantics, then launch
# a fresh 8000-step real-label-only distributed training run.
set -euo pipefail

REPO=${REPO:-/data00/yinhaolang/LLMSim}
cd "$REPO"

bash scripts/build_macro_v29_semantic_cache.sh
exec bash scripts/train_macro_v29_semantic_supervised.sh
