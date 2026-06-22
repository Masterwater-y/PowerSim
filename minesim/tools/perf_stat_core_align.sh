#!/usr/bin/env bash
set -euo pipefail

# MineSim ↔ perf event alignment after the PMU-fidelity rework.
#
# Strong alignment (same event semantics):
#   instructions                         -> MineSim Macro-Instructions
#   cpu-cycles                           -> MineSim Total Cycles
#   L1-icache-load-misses                -> MineSim L1I Misses (per 64-B fetch block)
#   iTLB-load-misses                     -> MineSim "Inst Page Walks" (= ITLB_MISSES.WALK_COMPLETED)
#   L1-dcache-loads + L1-dcache-stores   -> MineSim L1D Accesses (incl. line-straddle splits)
#   dTLB-loads + dTLB-stores             -> MineSim DTLB_4K + DTLB_2M + DTLB_1G Accesses
#   dTLB-load-misses + dTLB-store-misses -> MineSim "Data Page Walks" (= DTLB_*_MISSES.WALK_COMPLETED,
#                                                                       i.e. STLB miss + page walk)
#   branch-misses                        -> MineSim total Branch Mispredicts
#                                            (cond + indirect/BTB + return/RSB)
#
# Note: on Intel, perf "dTLB-*-misses" reports walk completions, NOT raw L1
# DTLB misses; MineSim's "L1 DTLB Misses" is therefore exposed for debugging
# only and does not align with perf.

EVENTS="instructions,cpu-cycles,L1-icache-load-misses,iTLB-load-misses,L1-dcache-loads,L1-dcache-stores,dTLB-loads,dTLB-stores,dTLB-load-misses,dTLB-store-misses,branch-misses"

if [ "$#" -eq 0 ]; then
  echo "Usage: $0 <command> [args...]" >&2
  echo >&2
  echo "Example:" >&2
  echo "  $0 ./your_workload arg1 arg2" >&2
  exit 1
fi

exec perf stat -e "$EVENTS" -- "$@"
