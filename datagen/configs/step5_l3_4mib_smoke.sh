#!/usr/bin/env bash
# step5_l3_4mib_smoke.sh — V9.5 A2/A3 后的 L3=4MiB 冒烟：验证 uarch_profile
# 参数化生效（profile.cache.l3.size_b == 4 MiB），oracle ↔ ref_sim 仍 100%。
set -euo pipefail

ROOT="${TAO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
TAOGEN="${TAO_DATAGEN_ROOT:-$ROOT/datagen}"
GEM5="${TAO_GEM5_ROOT:-$ROOT/gem5}/build/X86_MESI_Three_Level/gem5.opt"
CFG=$TAOGEN/configs/run_mt_mvp.py
REFSIM=$TAOGEN/mesi_ref_sim/build/mesi_ref_sim
COMPARE=$TAOGEN/mesi_ref_sim/scripts/compare_oracle.py
COMPARE_I=$TAOGEN/mesi_ref_sim/scripts/compare_ifetch.py
PMU=$TAOGEN/mesi_ref_sim/scripts/pmu_report.py
WL=$TAOGEN/workloads

export PATH=/opt/gcc-11/bin:$PATH
export LD_LIBRARY_PATH=/root/.pyenv/versions/3.8.0/lib:/opt/gcc-11/lib64:${LD_LIBRARY_PATH:-}

OUT=${1:-$ROOT/tmp/step5_l3_4mib}
rm -rf "$OUT"
mkdir -p "$OUT"

echo "=== W1 with L3=4MiB ==="
"$GEM5" --outdir="$OUT" "$CFG" \
    --cmd "$WL/mt_compute_int/mt_compute_int" \
    --workload-args 4 800 \
    --num-cores 4 \
    --l3-size 4MiB \
    > "$OUT/gem5.log" 2>&1

echo "profile size_b:"
python3 -c "import json; p=json.load(open('$OUT/uarch_profile.json')); \
print('l3.size_b =', p['cache']['l3']['size_b'], '(expected 4194304)')"

cat "$OUT/tao_trace/"*.mem_events.jsonl | python3 -c "
import json,sys
rows=[]
for ln in sys.stdin:
    s=ln.strip()
    if not s.startswith('{'): continue
    try: rows.append(json.loads(s))
    except Exception: pass
rows.sort(key=lambda r:(r.get('commit_tick',0), r.get('seq',0)))
for r in rows: print(json.dumps(r, separators=(',',':')))
" > "$OUT/mem_events.merged.jsonl"

"$REFSIM" "$OUT/uarch_profile.json" \
    "$OUT/mem_events.merged.jsonl" "$OUT/pred.jsonl" 2> "$OUT/refsim.log"

echo "----- d-side -----"
python3 "$COMPARE" "$OUT/mem_events.merged.jsonl" "$OUT/pred.jsonl" \
    | head -10
echo "----- i-side -----"
python3 "$COMPARE_I" "$OUT/mem_events.merged.jsonl" "$OUT/pred.jsonl" \
    | head -6
echo "----- PMU -----"
python3 "$PMU" "$OUT/mem_events.merged.jsonl" "$OUT/pred.jsonl" \
    --uarch-profile "$OUT/uarch_profile.json" | tail -18

echo "out: $OUT"
