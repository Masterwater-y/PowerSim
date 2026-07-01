#!/usr/bin/env bash
set -euo pipefail

EXTRA_ARGS=${EXTRA_ARGS:-"--planner-state-source tq"}
TAG_PREFIX=${TAG_PREFIX:-v14_headonly_qwen3_0p6b_step7000_tq_planner}

export EXTRA_ARGS
export TAG_PREFIX

exec bash scripts/run_v14_planner_label_all.sh
