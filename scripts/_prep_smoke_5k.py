#!/usr/bin/env python3
# scripts/_prep_smoke_5k.py
# 一次性切一个 5K rows 的 functional parquet，用于 smoke / bench 共用。

from pathlib import Path
import os
import sys
import pyarrow.parquet as pq

TAO_ROOT = Path(os.environ.get("TAO_ROOT", str(Path(__file__).resolve().parents[1])))
src = TAO_ROOT / "infer/data/W11_stream_mix/functional_parquet/functional.core0.parquet"
out_dir = TAO_ROOT / "infer/tmp_smoke_5k/functional_parquet"

if not src.exists():
    print(f"[prep_smoke_5k] missing source: {src}", file=sys.stderr)
    sys.exit(1)

out_dir.mkdir(parents=True, exist_ok=True)
tbl = pq.read_table(str(src)).slice(0, 5000)
out_file = out_dir / "functional.core0.parquet"
pq.write_table(tbl, str(out_file))
print(f"[prep_smoke_5k] rows={tbl.num_rows} -> {out_file}")
