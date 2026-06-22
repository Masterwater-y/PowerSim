# LLMSim 命令整理

本文档整理当前这条实验线常用的采集、数据集构建、训练、续训、验证与检查命令，避免换对话后丢失上下文。

约定：

- 仓库根目录：`/data00/yinhaolang/LLMSim`
- Python：`/data00/yinhaolang/infer/.venv/bin/python`
- 当前主 baseline ckpt：`ckpt/quota_32k_balanced_v1`
- 当前“旧负载下采样 + 新负载全量”的续训集：`data/windows_train10fp_mix_continue64_w512/windows.jsonl`
- 当前推荐 resume 续训集：`data/windows_continue_v2_ads_rank_ladder_quota_maxlen32768/windows.jsonl`

## 1. 环境准备

```bash
cd /data00/yinhaolang/LLMSim
export PY=/data00/yinhaolang/infer/.venv/bin/python
export HF_HUB_OFFLINE=1
```

## 2. 编译 workloads

```bash
cd /data00/yinhaolang/LLMSim/workloads
make -j
```

## 3. 采集新负载

当前新负载：

- `fp_compute_dense`
- `fp_lite`
- `ads_lookup_mix`
- `rank_score_filter`
- `lookup_latency_ladder`

采集命令：

```bash
cd /data00/yinhaolang/LLMSim && \
mkdir -p logs && \
OUT_BASE=$PWD/data/raw_train11_8c_500k \
TARGET_PER_CORE=500000 \
MIN_ACCEPT_PER_CORE=400000 \
MAX_ACCEPT_PER_CORE=700000 \
SCALE_MARGIN_PCT=110 \
PARALLEL=2 \
VALIDATE_WINDOWS=0 \
nohup bash scripts/collect_parallel_500k.sh fp_compute_dense fp_lite \
  </dev/null > logs/collect_fp_new6.log 2>&1 &
echo "started pid=$!"
```

只重采 `fp_lite`：

```bash
cd /data00/yinhaolang/LLMSim && \
mkdir -p logs && \
OUT_BASE=$PWD/data/raw_train11_8c_500k \
TARGET_PER_CORE=500000 \
MIN_ACCEPT_PER_CORE=400000 \
MAX_ACCEPT_PER_CORE=700000 \
SCALE_MARGIN_PCT=110 \
PARALLEL=1 \
VALIDATE_WINDOWS=0 \
nohup bash scripts/collect_parallel_500k.sh fp_lite \
  </dev/null > logs/collect_fp_lite_rewrite.log 2>&1 &
echo "started pid=$!"
```

删除旧采集结果后重采：

```bash
rm -rf \
  /data00/yinhaolang/LLMSim/data/raw_train11_8c_500k/W_fp_compute_dense \
  /data00/yinhaolang/LLMSim/data/raw_train11_8c_500k/W_fp_lite \
  /data00/yinhaolang/LLMSim/data/raw_train11_8c_500k/probe_fp_compute_dense \
  /data00/yinhaolang/LLMSim/data/raw_train11_8c_500k/probe_fp_lite \
  /data00/yinhaolang/LLMSim/data/raw_train11_8c_500k/_tmp_fp_compute_dense_a1 \
  /data00/yinhaolang/LLMSim/data/raw_train11_8c_500k/_tmp_fp_compute_dense_a2 \
  /data00/yinhaolang/LLMSim/data/raw_train11_8c_500k/_tmp_fp_compute_dense_a3 \
  /data00/yinhaolang/LLMSim/data/raw_train11_8c_500k/_tmp_fp_lite_a1 \
  /data00/yinhaolang/LLMSim/data/raw_train11_8c_500k/_tmp_fp_lite_a2 \
  /data00/yinhaolang/LLMSim/data/raw_train11_8c_500k/_tmp_fp_lite_a3
```

看采集进度：

```bash
tail -f /data00/yinhaolang/LLMSim/logs/collect_fp_new6.log
```

### 3.2 Ads/rank/lookup ladder 负载

这 3 个负载用于修复当前 11 负载验证中 `W_ads_ctr / W_feed_ranking` CPI 偏高的问题：

- `ads_lookup_mix`：cache-resident sparse feature lookup + hash/probe + branch filter
- `rank_score_filter`：feature gather + score accumulate + threshold/top-k branch
- `lookup_latency_ladder`：同一 lookup skeleton 覆盖 L1/L2/LLC/DRAM latency ladder

编译：

```bash
cd /data00/yinhaolang/LLMSim && \
make -C workloads ads_lookup_mix rank_score_filter lookup_latency_ladder
```

采集命令：

```bash
cd /data00/yinhaolang/LLMSim && \
mkdir -p logs && \
OUT_BASE=$PWD/data/raw_train12_ads_rank_ladder_8c_500k \
TARGET_PER_CORE=500000 \
MIN_ACCEPT_PER_CORE=400000 \
MAX_ACCEPT_PER_CORE=1000000 \
PROBE_SCALE=1 \
SCALE_MARGIN_PCT=110 \
PARALLEL=3 \
VALIDATE_WINDOWS=0 \
PROGRESS_INTERVAL=60 \
bash scripts/collect_parallel_500k.sh \
  ads_lookup_mix rank_score_filter lookup_latency_ladder \
  2>&1 | tee logs/collect_train12_ads_rank_ladder.log
```

当前实际采用的是 probe 结果，因为这 3 个 workload 的 `scale=1` 已经约 `0.90M-0.93M records/core`，低于 500k 的整数 scale 不存在：

```bash
cd /data00/yinhaolang/LLMSim && \
rm -rf data/raw_train12_ads_rank_ladder_8c_probe && \
mkdir -p data/raw_train12_ads_rank_ladder_8c_probe && \
ln -sfn ../raw_train12_ads_rank_ladder_8c_500k/probe_ads_lookup_mix \
  data/raw_train12_ads_rank_ladder_8c_probe/W_ads_lookup_mix && \
ln -sfn ../raw_train12_ads_rank_ladder_8c_500k/probe_rank_score_filter \
  data/raw_train12_ads_rank_ladder_8c_probe/W_rank_score_filter && \
ln -sfn ../raw_train12_ads_rank_ladder_8c_500k/probe_lookup_latency_ladder \
  data/raw_train12_ads_rank_ladder_8c_probe/W_lookup_latency_ladder
```

### 3.3 Fast-forward 模式（`FF_ATOMIC=1`）

当 workload 的 init 阶段（顺序 store 几 MB 大表）在 O3+Ruby 下耗时过长（>10 min）时，
启用 fast-forward：AtomicSimpleCPU + `atomic_noncaching` 跑完 init，首次
`m5_work_begin` 触发 `simulator.switch_processor()` 切到 O3+Ruby。

实现位于 [run_mt_mvp.py](../../taogen/configs/run_mt_mvp.py)（`--ff-atomic` flag）。

**SimObject 路径变化（重要）：**

- 普通模式 (`SimpleProcessor`)：trace 文件 = `board.processor.cores{i}.core.tao_trace.*`
- ff 模式 (`SimpleSwitchableProcessor`)：trace 文件 = `board.processor.switch{i}.core.tao_trace.*`
  - 字典 key `"switch"` 是 stdlib 给 "切换目标组" 的命名（不是动作），由 `setattr` 注册为 SimObject child name
  - TaoTrace probe 是 O3 commit-stage 专属，`run_mt_mvp.py` 显式挂在 `_switchable_cores["switch"]` 上
  - Atomic 启动组（`start{i}`）路径下不会出现任何 trace 文件，PMU `is_store ≈ 0` 验证

下游脚本均已双兼容（同时认 `cores{i}` 和 `switch{i}`）：

- [scripts/collect_parallel_500k.sh](../scripts/collect_parallel_500k.sh) 的 `find_trace_file`
- [data/build_windows.py](../data/build_windows.py) 的 `CORE_RE`
- [scripts/convert_trace_to_aligned_parquet.py](../scripts/convert_trace_to_aligned_parquet.py) 的 `CORE_RE`
- [scripts/_diag_quota_bootstrap.py](../scripts/_diag_quota_bootstrap.py) 的 `CORE_RE`
- [scripts/_diag_timewin.py](../scripts/_diag_timewin.py) 的 `CORE_RE`
- [eval/eval_cycles.py](../eval/eval_cycles.py) 的 `_CORE_NUMCYC` / `_CORE_INSTS`（stats.txt 同样会出现 `switch{i}.core.*`）

**单 workload 烟测**（先验证管道，~85s wall-clock）：

```bash
cd /data00/yinhaolang && \
mkdir -p LLMSim/logs && \
FF_ATOMIC=1 \
OUT_BASE=/data00/yinhaolang/LLMSim/data/raw_train12_v2_8c_500k_ff \
TARGET_PER_CORE=500000 \
MIN_ACCEPT_PER_CORE=400000 \
MAX_ACCEPT_PER_CORE=900000 \
PROBE_SCALE=1 \
PROBE_STOP_REC=700000 \
TIMEOUT_SECS=900 \
PARALLEL=1 \
VALIDATE_WINDOWS=0 \
nohup bash LLMSim/scripts/collect_parallel_500k.sh phased_mix \
  < /dev/null > LLMSim/logs/ff_smoke_phased_mix.log 2>&1 & disown
echo "started pid=$!"
```

**4 workload 并行重采（init 慢的那一批）**：

```bash
cd /data00/yinhaolang && \
mkdir -p LLMSim/logs && \
FF_ATOMIC=1 \
OUT_BASE=/data00/yinhaolang/LLMSim/data/raw_train12_v2_8c_500k_ff \
TARGET_PER_CORE=500000 \
MIN_ACCEPT_PER_CORE=400000 \
MAX_ACCEPT_PER_CORE=900000 \
PROBE_SCALE=1 \
PROBE_STOP_REC=700000 \
TIMEOUT_SECS=1800 \
PARALLEL=4 \
VALIDATE_WINDOWS=0 \
nohup bash LLMSim/scripts/collect_parallel_500k.sh \
  phased_mix ads_lookup_mix rank_score_filter lookup_latency_ladder \
  < /dev/null > LLMSim/logs/collect_ff_4w.log 2>&1 & disown
echo "started pid=$!"
```

注意：必须用 `< /dev/null` + `disown` 而不是只用 `nohup ... &`，否则当前 IDE / bwrap session
退出会把 SIGHUP 传给后台脚本，触发 `trap cleanup_children` 把所有 gem5 进程一起带走
（参见 [scripts/collect_parallel_500k.sh#L65-71](../scripts/collect_parallel_500k.sh#L65-L71)）。

**phased_mix 已知约束：** [bench_phased_mix.c](../workloads/src/bench_phased_mix.c) 的 `rounds = scale * 6`
（hot loop 每 round 每核约 117k µops × 6 rounds × 8 cores ≈ 5.6M 总，单核 ~700k 落在 [500k, 900k] 接受窗）。
旧 `scale * 48` 会产出 5.6M/核，远超 900k 上限。

## 4. 检查新负载数据

看目录是否落盘：

```bash
find /data00/yinhaolang/LLMSim/data/raw_train11_8c_500k -maxdepth 2 -type f \
  \( -name stats.txt -o -name counts.txt -o -name uarch_profile.json \) | sort
```

看某个 workload 的统计：

```bash
sed -n '1,120p' /data00/yinhaolang/LLMSim/data/raw_train11_8c_500k/W_fp_compute_dense/stats.txt
sed -n '1,120p' /data00/yinhaolang/LLMSim/data/raw_train11_8c_500k/W_fp_lite/stats.txt
```

看 `fp_lite` 的标签分布样本：

```bash
head -n 20 /data00/yinhaolang/LLMSim/data/raw_train11_8c_500k/W_fp_lite/tao_trace/board.processor.cores0.core.tao_trace.tao_trace.records.micro.jsonl
```

## 5. 构建窗口数据集

### 5.1 旧 train8 基础窗口集

```bash
cd /data00/yinhaolang/LLMSim
bash scripts/build_windows_train8.sh
```

### 5.2 10 个 workload 混合窗口集

先准备 merged raw：

```bash
cd /data00/yinhaolang/LLMSim && \
mkdir -p data/raw_train10fp_mix_8c_500k && \
ln -sfn $PWD/data/raw_fix3_8c_500k/W_branch_storm      data/raw_train10fp_mix_8c_500k/W_branch_storm && \
ln -sfn $PWD/data/raw_8w_8c_500k/W_chase_dram          data/raw_train10fp_mix_8c_500k/W_chase_dram && \
ln -sfn $PWD/data/raw_8w_8c_500k/W_compute_int         data/raw_train10fp_mix_8c_500k/W_compute_int && \
ln -sfn $PWD/data/raw_8w_8c_500k/W_false_sharing       data/raw_train10fp_mix_8c_500k/W_false_sharing && \
ln -sfn $PWD/data/raw_8w_8c_500k/W_indirect            data/raw_train10fp_mix_8c_500k/W_indirect && \
ln -sfn $PWD/data/raw_fix3_8c_500k/W_int_div           data/raw_train10fp_mix_8c_500k/W_int_div && \
ln -sfn $PWD/data/raw_fix3_8c_500k/W_phased_mix        data/raw_train10fp_mix_8c_500k/W_phased_mix && \
ln -sfn $PWD/data/raw_8w_8c_500k/W_stream              data/raw_train10fp_mix_8c_500k/W_stream && \
ln -sfn $PWD/data/raw_train11_8c_500k/W_fp_compute_dense data/raw_train10fp_mix_8c_500k/W_fp_compute_dense && \
ln -sfn $PWD/data/raw_train11_8c_500k/W_fp_lite          data/raw_train10fp_mix_8c_500k/W_fp_lite
```

再构建窗口：

```bash
cd /data00/yinhaolang/LLMSim && \
$PY data/build_windows.py \
  --raw data/raw_train10fp_mix_8c_500k \
  --out data/windows_train10fp_mix_w512 \
  --window 512 \
  --stride 256 \
  --jobs 8 \
  --workloads \
    W_branch_storm W_chase_dram W_compute_int W_false_sharing \
    W_indirect W_int_div W_phased_mix W_stream \
    W_fp_compute_dense W_fp_lite
```

生成 cache：

```bash
cd /data00/yinhaolang/LLMSim && \
$PY scripts/prepare_dataset_cache.py \
  --data data/windows_train10fp_mix_w512/windows.jsonl \
  --max-len 32768 \
  --jobs 8
```

### 5.3 续训集：旧负载下采样 + 新负载全量

当前使用的是：

- 旧 8 负载各 cap 到 `600`
- `W_fp_compute_dense` 全量
- `W_fp_lite` 全量

命令：

```bash
cd /data00/yinhaolang/LLMSim && \
$PY scripts/downsample_workload.py \
  --in data/windows_train10fp_mix_w512/windows.jsonl \
  --out data/windows_train10fp_mix_continue64_w512 \
  --cap W_branch_storm=600 \
  --cap W_chase_dram=600 \
  --cap W_compute_int=600 \
  --cap W_false_sharing=600 \
  --cap W_indirect=600 \
  --cap W_int_div=600 \
  --cap W_phased_mix=600 \
  --cap W_stream=600 \
  --max-len 32768
```

看样本数：

```bash
$PY - <<'PY'
import json
from collections import Counter
cnt = Counter()
path = "/data00/yinhaolang/LLMSim/data/windows_train10fp_mix_continue64_w512/windows.jsonl"
for line in open(path):
    s = line.strip()
    if s.startswith("{"):
        cnt[json.loads(s)["workload"]] += 1
print("total =", sum(cnt.values()))
for k in sorted(cnt):
    print(k, cnt[k])
PY
```

### 5.4 Resume v2：baseline quota + ads/rank/ladder 新负载

推荐用于从 `quota_32k_balanced_v1` 继续训练。口径与 baseline 对齐，都是 `quota-max-len=32768`。

先构建 3 个新负载窗口：

```bash
cd /data00/yinhaolang/LLMSim && \
$PY data/build_windows.py \
  --raw data/raw_train12_ads_rank_ladder_8c_probe \
  --out data/windows_train12_ads_rank_ladder_quota_maxlen32768 \
  --quota-max-len 32768 \
  --quota-seed 20260618 \
  --cache-max-len 32768 \
  --jobs 3 \
  --workloads W_ads_lookup_mix W_rank_score_filter W_lookup_latency_ladder \
  2>&1 | tee logs/build_windows_train12_ads_rank_ladder_quota.log
```

当前新负载窗口数：

```text
W_ads_lookup_mix          1423
W_rank_score_filter       1424
W_lookup_latency_ladder   1390
total                     4237
```

再合并 baseline balanced quota 集：

```bash
cd /data00/yinhaolang/LLMSim && \
OUT=data/windows_continue_v2_ads_rank_ladder_quota_maxlen32768 && \
rm -rf "$OUT" && \
mkdir -p "$OUT" && \
cat \
  data/windows_quota_maxlen32768_balanced/windows.jsonl \
  data/windows_train12_ads_rank_ladder_quota_maxlen32768/windows.jsonl \
  > "$OUT/windows.jsonl" && \
$PY scripts/prepare_dataset_cache.py \
  --data "$OUT/windows.jsonl" \
  --max-len 32768 \
  --jobs 8 \
  2>&1 | tee logs/prepare_continue_v2_ads_rank_ladder_quota.log
```

当前合并集窗口数：

```text
W_branch_storm            1164
W_chase_dram               920
W_compute_int              840
W_false_sharing            856
W_indirect                 876
W_int_div                 1285
W_phased_mix              1500
W_stream                   878
W_ads_lookup_mix          1423
W_rank_score_filter       1424
W_lookup_latency_ladder   1390
total                    12556
```

## 6. 训练

### 6.1 基线训练

当前最优 baseline 是 `quota_32k_balanced_v1`。当时命令口径：

```bash
cd /data00/yinhaolang/LLMSim && \
NPROC=8 \
STEPS=3000 \
BS=1 \
GRAD_ACCUM=2 \
MAXLEN=32768 \
DATA=data/windows_quota_maxlen32768_balanced/windows.jsonl \
OUT=ckpt/quota_32k_balanced_v1 \
bash scripts/launch_ddp8.sh | tee logs/train_quota_balanced_20260616_204803.log
```

### 6.2 新混合集从零训练

```bash
cd /data00/yinhaolang/LLMSim && \
NPROC=8 \
STEPS=2000 \
BS=1 \
GRAD_ACCUM=2 \
MAXLEN=32768 \
DATA=data/windows_train10fp_mix_continue64_w512/windows.jsonl \
OUT=ckpt/train10fp_mix_continue64_s2000 \
bash scripts/launch_ddp8.sh | tee logs/train10fp_mix_continue64_s2000.log
```

### 6.3 备份原 baseline ckpt

```bash
cd /data00/yinhaolang/LLMSim && \
ts=$(date +%Y%m%d_%H%M%S) && \
cp -a ckpt/quota_32k_balanced_v1 ckpt/quota_32k_balanced_v1_backup_$ts
```

### 6.4 从 `quota_32k_balanced_v1` 真正续训

`scripts/launch_ddp8.sh` 已支持环境变量 `INIT_CKPT`，非空时会透传成 `--init-ckpt`。

续训命令：

```bash
cd /data00/yinhaolang/LLMSim && \
NPROC=8 \
STEPS=2000 \
BS=1 \
GRAD_ACCUM=2 \
MAXLEN=32768 \
DATA=data/windows_train10fp_mix_continue64_w512/windows.jsonl \
OUT=ckpt/train10fp_mix_continue64_from_quota32k_resume_s2000 \
INIT_CKPT=ckpt/quota_32k_balanced_v1 \
bash scripts/launch_ddp8.sh | tee logs/train10fp_mix_continue64_from_quota32k_resume_s2000.log
```

看续训 rank0 实时日志：

```bash
tail -f /data00/yinhaolang/LLMSim/logs/rank_0.log
```

确认本次启动确实走了 resume：

```bash
rg -n "\\[launch\\] INIT_CKPT|\\[resume\\]" \
  /data00/yinhaolang/LLMSim/logs/train10fp_mix_continue64_from_quota32k_resume_s2000.log \
  /data00/yinhaolang/LLMSim/logs/rank_0.log
```

### 6.5 推荐 resume v2 续训命令

使用 `baseline quota balanced + ads/rank/lookup ladder` 数据集，从当前最优 ckpt 继续训练：

```bash
cd /data00/yinhaolang/LLMSim && \
NPROC=8 \
STEPS=1500 \
BS=1 \
GRAD_ACCUM=2 \
MAXLEN=32768 \
DATA=data/windows_continue_v2_ads_rank_ladder_quota_maxlen32768/windows.jsonl \
OUT=ckpt/continue_v2_ads_rank_ladder_from_quota32k_s1500 \
INIT_CKPT=ckpt/quota_32k_balanced_v1 \
bash scripts/launch_ddp8.sh \
  2>&1 | tee logs/train_continue_v2_ads_rank_ladder_from_quota32k_s1500.log
```

如果要更保守，先把 `STEPS` 改成 `1000` 跑短版，再用 11 负载 PMU 验证确认 `W_ads_ctr / W_feed_ranking` 改善且旧 8 负载不退化。

## 7. 验证

### 7.1 用 baseline 跑 infer3

```bash
cd /data00/yinhaolang/LLMSim && \
CUDA_VISIBLE_DEVICES=0 HF_HUB_OFFLINE=1 \
$PY eval/eval_quota_cycles.py \
  --raw-root data/raw_infer3_8c_500k_fullfit \
  --ckpt ckpt/quota_32k_balanced_v1 \
  --max-len 32768 \
  --uarch-config arch_A \
  --seed-n 160 \
  --dt-target 1000 \
  --dt-min 200 \
  --dt-max 8000 \
  --dt-alpha 0.3 \
  --dt-target-load 0.95 \
  --nmin 8 \
  --tpm-init 30 \
  --ewma-alpha 0.2 \
  --ucb-lambda 1.0 \
  > logs/eval_quotaC_infer3.log 2>&1
```

### 7.2 用新 ckpt 跑 infer3

```bash
cd /data00/yinhaolang/LLMSim && \
CUDA_VISIBLE_DEVICES=0 HF_HUB_OFFLINE=1 \
$PY eval/eval_quota_cycles.py \
  --raw-root data/raw_infer3_8c_500k_fullfit \
  --ckpt ckpt/train10fp_mix_continue64_s2000 \
  --max-len 32768 \
  --uarch-config arch_A \
  --seed-n 160 \
  --dt-target 1000 \
  --dt-min 200 \
  --dt-max 8000 \
  --dt-alpha 0.3 \
  --dt-target-load 0.95 \
  --nmin 8 \
  --tpm-init 30 \
  --ewma-alpha 0.2 \
  --ucb-lambda 1.0 \
  > logs/eval_train10fp_mix_continue64_s2000_infer3.log 2>&1
```

### 7.3 用 resume 后 ckpt 跑 infer3

```bash
cd /data00/yinhaolang/LLMSim && \
CUDA_VISIBLE_DEVICES=0 HF_HUB_OFFLINE=1 \
$PY eval/eval_quota_cycles.py \
  --raw-root data/raw_infer3_8c_500k_fullfit \
  --ckpt ckpt/train10fp_mix_continue64_from_quota32k_resume_s2000 \
  --max-len 32768 \
  --uarch-config arch_A \
  --seed-n 160 \
  --dt-target 1000 \
  --dt-min 200 \
  --dt-max 8000 \
  --dt-alpha 0.3 \
  --dt-target-load 0.95 \
  --nmin 8 \
  --tpm-init 30 \
  --ewma-alpha 0.2 \
  --ucb-lambda 1.0 \
  > logs/eval_resume_quota32k_infer3.log 2>&1
```

### 7.4 快速查看验证结果

```bash
rg -n "Summary|pred_vs_roi_stats|label_vs_roi_stats|gem5_full_vs_roi_stats|macro/s|running pred" \
  /data00/yinhaolang/LLMSim/logs/eval_quotaC_infer3.log \
  /data00/yinhaolang/LLMSim/logs/eval_train10fp_mix_continue64_s2000_infer3.log \
  /data00/yinhaolang/LLMSim/logs/eval_resume_quota32k_infer3.log
```

### 7.5 11 负载并行验证：当前 baseline 结果

当前已完成一次 `quota_32k_balanced_v1` 的 11 负载并行验证：

- 总控日志：[logs/eval_parallel_quota32kv1_console_rerun.log](file:///data00/yinhaolang/LLMSim/logs/eval_parallel_quota32kv1_console_rerun.log)
- 子日志目录：[logs/eval_parallel_quota_32k_balanced_v1_20260618_124659](file:///data00/yinhaolang/LLMSim/logs/eval_parallel_quota_32k_balanced_v1_20260618_124659)
- 结果沉淀：[docs/validation_results.md](file:///data00/yinhaolang/LLMSim/docs/validation_results.md)

查看最终汇总表：

```bash
sed -n '/FINAL SUMMARY/,$p' \
  /data00/yinhaolang/LLMSim/logs/eval_parallel_quota32kv1_console_rerun.log
```

当前 `scripts/eval_parallel.sh` 已升级：重跑后日志末尾会同时包含两张表：

- `FINAL SUMMARY`：全局 CPI / cycles 误差
- `FINAL PMU SUMMARY`：每个 workload × PMU 的 `pred / label / trace ROI / gem5` 与误差

说明：

- `trace ROI` 是从 trace rows 用 `aggregate_pmu()` 同口径聚合出的 ROI PMU baseline。
- 当前 `gem5` 列只有 `cpi` 能稳定从 `stats.txt` 解析；其它 PMU 没有可靠 gem5 full/ROI 字段时显示 `-`。
- 稀疏 PMU（如 `itlb_miss / inv_recv / mpki_br`）在真值接近 0 时，窗口级 MAPE 会非常大，优先看全局 `pred vs ROI`。

重跑带 PMU 结果的 11 负载验证：

```bash
cd /data00/yinhaolang/LLMSim && \
mkdir -p logs && \
CKPT=ckpt/quota_32k_balanced_v1 \
nohup bash scripts/eval_parallel.sh \
  </dev/null > logs/eval_parallel_quota32kv1_pmu_console.log 2>&1 &
echo "started pid=$!"
```

查看 PMU 汇总表：

```bash
sed -n '/FINAL PMU SUMMARY/,$p' \
  /data00/yinhaolang/LLMSim/logs/eval_parallel_quota32kv1_pmu_console.log
```

核心结果：

- 11 个 workload 平均 `pred vs ROI = 7.58%`
- 去掉最大异常点 `W_ads_ctr` 后，10 个 workload 平均 `pred vs ROI = 4.95%`
- `W_ads_ctr` 仍是主要问题：`pred vs ROI = 33.87%`
- `W_feed_ranking = 9.19%`
- `W_interest_graph_recall = 1.56%`

## 8. 训练过程检查

看当前进度：

```bash
tail -n 50 /data00/yinhaolang/LLMSim/logs/rank_0.log
```

看吞吐和最终结果：

```bash
rg -n "\\[DONE\\]|\\[THROUGHPUT|best_val_loss|effective global batch" \
  /data00/yinhaolang/LLMSim/logs/rank_0.log
```

看某次训练是否真的是 resume：

```bash
rg -n "\\[resume\\]|Loading weights|new embeddings will be initialized|init-ckpt" \
  /data00/yinhaolang/LLMSim/logs/rank_0.log \
  /data00/yinhaolang/LLMSim/logs/train10fp_mix_continue64_s2000.log
```

## 9. 当前结论口径

- 当前最优 ckpt：`ckpt/quota_32k_balanced_v1`
- `ckpt/train10fp_mix_continue64_s2000` 是从基座重新开始，不是 resume
- 后续正确路线：从 `ckpt/quota_32k_balanced_v1` 用 `data/windows_continue_v2_ads_rank_ladder_quota_maxlen32768/windows.jsonl` 继续训，再跑 11 负载 PMU 验证对比

## 10. 相关文件

- 训练入口：`train/train_lora.py`
- 8 卡启动脚本：`scripts/launch_ddp8.sh`
- 采集脚本：`scripts/collect_parallel_500k.sh`
- 窗口构建：`data/build_windows.py`
- cache 构建：`scripts/prepare_dataset_cache.py`
- 下采样：`scripts/downsample_workload.py`
- 验证入口：`eval/eval_quota_cycles.py`
