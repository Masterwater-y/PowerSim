# v29 trace collection snapshot

这是从 TSim 复制并做路径可移植化处理的 v28 business workload 与采集工具。v29 的名字
描述模型/训练/推理合同；原始 workload 名仍保留 `W_v28_*`，两者不矛盾。

目录：

```text
workloads/v28/                     23 个静态多线程 workload 的同一 C 源码和 Makefile
scripts/collect_v28_workloads.sh   train/heldout/all workload 选择器
scripts/collect_parallel_500k.sh   probe、规模估计、并行 gem5 采集和逐核完整性检查
scripts/convert_trace_to_aligned_parquet.py
                                   records/labels JSONL 对齐为 Parquet
```

构建 workload：

```bash
make -C vendor/v29/trace_collection/workloads/v28 all
```

采集一个 seed/core slice：

```bash
GEM5_ROOT=../gem5 \
PY=/path/to/python3.11 \
NUM_CORES=4 SEED=0 PARALLEL=4 FF_ATOMIC=1 \
STRICT_NATURAL_ROI=1 RUN_TO_COMPLETION=1 \
TARGET_PER_CORE=750000 MIN_ACCEPT_PER_CORE=500000 MAX_ACCEPT_PER_CORE=1000000 \
OUT_BASE="$PWD/data/raw_v28_1_business_a2_sharedzipf_seed0_c04" \
bash vendor/v29/trace_collection/scripts/collect_v28_workloads.sh all
```

采集脚本只产出 JSONL。随后必须用 `convert_trace_to_aligned_parquet.py` 对齐，并运行
TCSim 的 `scripts/audit_v28_raw_dataset.py`。正式的 1/4/8/16/32 核串行编排仍使用项目内
`scripts/tmp/run_v28_business_serial_cores_collect.sh`，通过 `TSIM_ROOT` 指向本归档目录时，
路径应设置为 `vendor/v29/trace_collection`。

不要提交生成的 workload 二进制和 raw trace；它们属于本地构建/数据产物。
