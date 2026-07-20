# LLMSim — Window-level Multi-core PMU Predictor via LLM Fine-tuning

## 0. 目标

用 LLM（LoRA 微调）把"多核 functional trace 窗口"直接映射成"该窗口内每核的 PMU 向量"。

下一代 Coding LLM 路线、真实汇编数据方案、LM head/回归头选择、
SFT/RL/RAG 取舍和严格评测协议见
[docs/llm_multicore_cpu_simulation_blueprint.md](docs/llm_multicore_cpu_simulation_blueprint.md)。

- 输入：纯 functional trace（架构态可见字段）
- 输出：窗口内 N_core × K_pmu 的标量回归
- 微架构态（cycle / cache miss / mshr / tlb / mesi / ...）只能作为 label，**禁止作为输入**

## 1. 目录结构

```
LLMSim/
├── README.md                # 本文档
├── docs/
│   └── design.md            # 设计与方案审核（含改进项）
├── config/
│   ├── tokenizer.json       # 自定义 vocab 规范
│   └── pmu_keys.yaml        # K_pmu 维标签字段定义（口径单一真值源）
├── data/
│   ├── build_windows.py     # gem5 raw -> 窗口样本 jsonl
│   ├── tokenize.py          # functional µop -> token id 序列
│   └── stats.py             # PMU 归一化统计（mean/std for log1p）
├── model/
│   ├── tokenizer.py         # 自定义 vocab 实现（避免数字被 BPE 切碎）
│   ├── regression_head.py   # <PMU> token hidden -> [N_core, K_pmu]
│   └── llm_wrapper.py       # 加载 base LLM + LoRA + 回归头
├── train/
│   ├── dataset.py           # jsonl -> torch Dataset / collate
│   ├── loss.py              # log1p Huber + ratio head + 多任务加权
│   └── train_lora.py        # 训练入口
├── eval/
│   ├── metrics.py           # MAPE / log-MAE / cycles ±10% 命中率
│   └── eval.py
└── scripts/
    ├── env.sh
    ├── 01_build_windows.sh
    ├── 02_train.sh
    └── 03_eval.sh
```

## 2. 输入边界（functional only）

每条 µop 编码为定长 8 槽：

```
<CORE i> <OPCLASS c> <SRC regs hash> <DST regs hash>
<MEMKIND k> <VLINE_BUCKET> <VPAGE_BUCKET>
<BR taken? target_delta_bucket?>
```

只允许：
- `core_id`, `opcode_class`, `n_src/n_dst`, 寄存器号 hash
- `mem_kind`(none/load/store/atomic/fence)
- `vaddr`、`paddr`（已经由架构页表决定，可视为可见）
- `cacheline_addr/paddr`（hash bucket）
- `is_branch / is_cond / is_indirect / taken`、target PC delta bucket

禁止：
- 任何 cycle / latency / issue_tick
- mesi / coh / path_class / mshr / tlb_hit / walker / lru_pos
- ref_sim oracle 输出

## 3. Label 边界（窗口聚合 PMU）

`config/pmu_keys.yaml` 定义 K 维：

```
- cycles
- instructions_retired
- branch_count
- branch_miss
- l1d_load_miss
- l1d_store_miss
- l1i_miss
- l2_miss
- llc_miss
- dtlb_miss
- itlb_miss
- mshr_occupancy_avg
- stall_frontend
- stall_backend
- inv_recv
- inv_send
```

`label.shape = [N_core, K_pmu]`，每维独立 log1p + z-score。

`cycles` 同时输出 `1/IPC` ratio 头，最终 `cycles_pred = ratio_pred × instr_count`。

## 4. 模型

- Base LLM：`Qwen3-0.6B-Base`（本地缓存 `~/.cache/huggingface/hub/models--Qwen--Qwen3-0.6B-Base`）
- LoRA：rank=32, target=q/k/v/o，主体冻结
- 回归头：序列尾部追加 per-core `<QUERY CORE_i>` token，取其 hidden 过 2 层 MLP -> 每核 K 维输出
- 配置 conditioning：序列首部插 `<CFG_*>` token（cache size、MSHR 等 log-bin）

## 4.1 运行环境（已核实）

- Python：`/data00/yinhaolang/infer/.venv/bin/python`（3.11, torch 2.12+cu130, pyarrow 24.0）
- GPU：4× NVIDIA H20 97GB
- 需补装：`transformers` / `peft` / `datasets`
- 数据管线：复用 taogen 已编译的 `gem5.opt` + `mesi_ref_sim` + `tools/pack_to_parquet.py`

## 5. 训练

- Loss = Σ_k w_k · Huber(log1p(y_k), log1p(ŷ_k)) + λ·MSE(1/IPC)
- 优化器：AdamW (lr=2e-4 for LoRA, 1e-3 for head)
- 上下文长度：8K~16K token（W=1024 µop × 8 token/µop ≈ 8K）

## 6. 评估

- per-PMU MAPE / log-MAE
- cycles ±5% / ±10% 命中率
- 跨配置 OOD（leave-one-config-out）
- 跨 workload OOD（leave-one-workload-out）

## 7. 数据来源

依赖 `taogen` 现有 gem5 detailed run 输出：
- functional 列：`infer/functional_trace/schema.py` 中的 `FUNCTIONAL_TRACE_COLS`
- PMU 真值：`labels.core<N>.parquet` + ruby/PMU log 聚合

参见 `docs/design.md` 第 4 节。
