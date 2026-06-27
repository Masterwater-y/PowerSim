# Raw Trace Pool

统一 raw trace 入口：

```text
data/raw_trace_pool/
```

该目录只放 symlink，不复制 raw trace 数据。

推荐路径：

```text
data/raw_trace_pool/activecore_train/c01_seedA
data/raw_trace_pool/activecore_train/c04_seedA
data/raw_trace_pool/activecore_train/c08_seedA
data/raw_trace_pool/activecore_eval/c06_seedC_infer17
data/raw_trace_pool/activecore_eval/c08_seedB_infer17
```

`build_windows.py --raw` 必须指向其中一个 leaf raw root，例如：

```bash
/data00/yinhaolang/infer/.venv/bin/python data/build_windows.py \
  --raw data/raw_trace_pool/activecore_train/c04_seedA \
  --out data/windows_v9_tq_smoke_c04 \
  --workloads W_false_sharing W_ads_ranking_proxy \
  --tq-max-len 32768 \
  --tq-target-windows 20 \
  --tq-min-uops-per-core 256 \
  --per-workload-cap 20 \
  --cache-max-len 32768 \
  --jobs 2
```

`data/raw_trace_pool/all/` 下面也挂了当前 checkout 里所有 `data/raw*`
根目录，只用于发现历史数据；正式实验优先使用上面的推荐路径。
