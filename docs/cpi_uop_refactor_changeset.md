# cpi_uop 重构 — 完整修改清单（不动手版）

> 配套主方案：[cpi_uop_refactor_plan.md](file:///data00/yinhaolang/LLMSim/docs/cpi_uop_refactor_plan.md)
>
> 本文档把"label / model / train / eval / 工具脚本"5 层每一处需要改的点列出来，
> 包含动机、改前/改后、依赖与回归检查。**先不动手**，等审完再实施。
>
> 用户已确认的 3 项决策：
> - **D1**：重新跑 build_windows 生成带 cpi_uop 的新数据集（不复用旧 jsonl）。
>   原因：后续所有负载要重采，趁此一起。
> - **D2**：eval planner 改 uop budget 后旧 tpm/var/water-filling/shrink-retry
>   **注释保留 + flag 切换**，**默认 mode=uop**。
> - **D3**：dt_target 仍按 cycle 计（多核终点对齐核心，不能换单位）。

## 0. 单位与口径定义（全局约束）

| 符号 | 单位 | 含义 |
|---|---|---|
| uops | 个 | 窗口内 µop 数 = `len(window)` |
| macros | 个 | 窗口内 macro 数 = `instr_retired` |
| cycles | cycle | 窗口内 cycles = `(t_end - t_start) / tpc` |
| tokens | token | 窗口 tokens = `6 × uops + 控制/cfg/summary 开销` |
| upm | 比 | uops / macros，workload 内通常 1.3-2.5 |
| **cpi_uop** | cycle/uop | `cycles / uops` — **新主目标** |
| **cpi_macro** | cycle/macro | `cycles / macros` — 仅 eval 报表用 |
| tpm | token/macro | tokens_per_macro，旧 planner 估算用，新方案下退化为 `6 × upm` |
| dt_target | cycle | planner 用于终点对齐的目标窗时长，**保持 cycle 单位** |

恒等式（实施时反复用到）：
```
cycles = cpi_uop × uops = cpi_macro × macros
upm    = uops / macros
cpi_macro = cpi_uop × upm
tokens(in window) ≈ 6 × uops   (控制/summary token 是常量)
```

---

## 1. data 侧：build_windows 加 cpi_uop / uops

文件：[data/build_windows.py](file:///data00/yinhaolang/LLMSim/data/build_windows.py)

### 1.1 PMU_KEYS 改名（line 66-75）

```python
# 现状
PMU_KEYS = ["cpi", "mpki_br", "branch_mispred_frac", "mr_l1d_ld",
            "mr_l1d_st", "mr_llc", "dtlb_miss", "mshr_avg"]

# 改造后
PMU_KEYS = ["cpi_uop", "mpki_br", "branch_mispred_frac", "mr_l1d_ld",
            "mr_l1d_st", "mr_llc", "dtlb_miss", "mshr_avg"]
```

**影响面**：dataset / regression_head / loss / eval 全部要跟着改字符串（详见后续节）。

### 1.2 aggregate_pmu 增加 uops + cpi_uop（line 425-518）

`aggregate_pmu()` 返回字典加：

```python
uops = len(window)            # 已有 implicit 概念
cpi_uop = safe_div(cycles, uops)
cpi_macro = safe_div(cycles, instr_retired)   # 仅用于 sample meta，不进 PMU_KEYS
```

返回 dict 新增字段：
- `"uops"`: int
- `"cpi_uop"`: float — 替换原 "cpi" 在 PMU_KEYS 的位置
- `"cpi_macro"`: float — 旁路 label，不进 PMU_KEYS

**取舍**：旧 key `"cpi"` 在返回字典里**保留**为 `cpi_macro` 别名，避免外部脚本
（eval/diag scripts）批量挂掉。但 PMU_KEYS 不再含 "cpi"。

### 1.3 sample meta 加 uops_per_core

build_samples_align / build_samples_timewin / build_samples_tq / build_samples_quota
四个 builder 在最终 sample dict 都要加：

```python
"uops_per_core": [len(per_core_windows[c][0]) for c in cores],
"cpi_macro_per_core": [per_core_windows[c][1]["cpi_macro"] for c in cores],
```

理由：训练 dataset 只读 PMU_KEYS 字段，但 eval 阶段做 macro/uop 双输出时需要
回算 cpi_macro，避免再 re-aggregate。

### 1.4 重跑数据集（D1）

```
python data/build_windows.py \
    --raw <raw> --out data/windows_v7_cpi_uop \
    --scheme tq --tq-max-len 16384 --tq-target-windows 1200 \
    --workers 32
```

**新数据集名建议**：`windows_v7_cpi_uop_tq32k`（与旧 `v6.2_tq32k` 并列），
不覆盖旧数据。注意 .gitignore 已把 `data/` 整个屏蔽，不影响 git。

回归检查：
- 头一个 workload 的 `label[c][0]`（cpi_uop）应在 `[0.2, 30]` 区间，
  对照 docs/diag/diag_cpi_uop_vs_macro.json 的 cpi_uop 列 mean/p90。
- 单 sample 的 `cpi_macro_per_core[c] / cpi_uop_per_core[c]` 应在 `[1.0, 5.0]`
  范围（upm 物理量级）。

---

## 2. model 侧：regression_head 重命名

文件：[model/regression_head.py](file:///data00/yinhaolang/LLMSim/model/regression_head.py)

### 2.1 PMU_KEYS / KEY_SPACE 改名（line 14-41）

```python
PMU_KEYS = ["cpi_uop", "mpki_br", ...]    # 第 0 维改名
KEY_SPACE = {
    "cpi_uop": "logratio",                 # 同样是 log 空间
    ...
}
```

**架构、维度、forward、sigmoid mask 全部不变**。只是字符串改名。

### 2.2 K 不变

K = len(PMU_KEYS) 仍是 8。模型 ckpt 的 head 形状不变 → **理论上旧 ckpt 可继续
load**，只是第 0 维语义从 cpi_macro 变成 cpi_uop。但因为 D1 重跑数据集，会
重新训练 head，所以这点不重要。

### 2.3 兼容 hook（可选，避免误用旧 ckpt）

ckpt 保存时新增 meta `head_label_version = "v7_cpi_uop"`，load 时检查不匹配
就 warn。改 train/train_ddp_main.py 的 ckpt save / load 一处即可。

---

## 3. train 侧：loss / dataset / 训练入口

### 3.1 train/loss.py 改 key 字符串（line 80-156）

[train/loss.py](file:///data00/yinhaolang/LLMSim/train/loss.py) 直接搜
`"cpi"` 替换为 `"cpi_uop"`：
- line 83-84 `init_lv[idx["cpi"]] = -1.0`
- line 126 `cpi_idx = self.idx["cpi"]`
- line 130-131 `cpi_label = label[..., cpi_idx]`
- line 153-155 `cpi_log = pred[..., self.idx["cpi"]]`

**关键语义改动**：L_cycles 项原来是
```
log_cycles_pred = pred_cpi + log(macro)
log_cycles_tgt  = log(label_cpi) + log(macro)
```
现在要改成：
```
log_cycles_pred = pred_cpi_uop + log(uops)
log_cycles_tgt  = log(label_cpi_uop) + log(uops)
```

→ Loss 函数的 `instr_retired` 入参需要替换成 `uops`。dataset collate 需要
把 `uops` 也丢进 batch（见 3.2）。

**dataset collate 替换原则**：保留 `instr_retired` 字段名（向后兼容），
但语义改成 uops。或者新增 `uops` 字段、彻底废弃 `instr_retired`。我倾向**后者**
（语义清晰），但要回看所有用 batch["instr_retired"] 的代码点。

#### 3.1.x 影响面排查（用 grep 全仓 instr_retired）

- [eval/eval_quota_cycles.py](file:///data00/yinhaolang/LLMSim/eval/eval_quota_cycles.py) line 1188 `macro = float(step["instr_retired"][ci])` → 替换思路见 §5
- [train/dataset.py](file:///data00/yinhaolang/LLMSim/train/dataset.py) line 79、182、228、269 都要看

→ 决策：**保留 `instr_retired` 字段（macro 数）** + **新增 `uops` 字段（µop 数）**，
不用一个字段两个语义，避免半年后看代码人脑撞墙。

### 3.2 train/dataset.py — 加 uops 字段（line 60-275）

[train/dataset.py](file:///data00/yinhaolang/LLMSim/train/dataset.py):

- line 79 `"instr_retired": rec["instr_retired"]` → 旁边加
  `"uops": rec.get("uops_per_core", rec["instr_retired"])`（兼容旧 jsonl）。
- 类似地 line 182、229 都加 `"uops"` 字段。
- collate（line 247-275）：在 `instr` tensor 同位置加 `uops` tensor：
  ```python
  uops = torch.ones((B, max_nc), dtype=torch.float32)
  for ci in range(b["n_core"]):
      uops[bi, ci] = float(b["uops"][ci])
  out["uops"] = uops
  ```
- batch 返回字典加 `"uops"`。

### 3.3 train 入口传 uops 给 loss

[train/train_ddp_main.py](file:///data00/yinhaolang/LLMSim/train/train_ddp_main.py)
（未读，待查找）：调用 `loss_fn(pred, label, instr_retired=batch["instr_retired"])`
改成 `uops=batch["uops"]`。loss 内部参数名同步改。

### 3.4 归一化 stats 重统计

- `train/loss.py` 没有 running mean 之类的，是直接 log 后 Huber，纯函数。
- 如果有 dataset 侧的 label normalize（再去 grep），需要按新 cpi_uop 的分布
  统计 mean/std。

→ 待 grep 验证：
```
grep -rn "label_mean\|label_std\|stats_path" train/ model/ data/
```

---

## 4. eval 侧：planner / time advance / 双输出

文件：[eval/eval_quota_cycles.py](file:///data00/yinhaolang/LLMSim/eval/eval_quota_cycles.py)

### 4.1 加 --budget-mode flag（D2）

```python
ap.add_argument("--budget-mode", choices=["uop", "macro"], default="uop",
                help="窗预算单位：uop=新方案(默认)，macro=旧 tpm/var 路径")
```

### 4.2 改 CPI_IDX 含义（line 55）

```python
# 现状
CPI_IDX = PMU_KEYS.index("cpi")
# 改造后
CPI_UOP_IDX = PMU_KEYS.index("cpi_uop")
```

### 4.3 OnlineQuotaPlanner 双路径（line 648-782）

**uop mode（新默认，简洁路径）**：

```python
class OnlineQuotaPlanner:
    def __init__(self, n_core, max_len, mode="uop", ...):
        self.mode = mode
        if mode == "uop":
            # 控制 token 开销 ≈ cfg(4) + per-core BEGIN/END/QUERY/summary(~38)
            overhead = 4 + n_core * 40 + 10  # 经验值，跟 build_core_summary 对齐
            self.uop_budget = (max_len - overhead) // 6
            # dt_target 仍按 cycle 维护
            self.dt_target = dt_init
            self.dt_min = dt_min
            self.dt_max = dt_max
            # tpm/var/carry/load_ema 在 uop mode 下不维护
        else:
            # macro mode：原有路径完整保留（注释成 "legacy"）
            ...

    def plan_uop(self, pred_cpi_uop, pred_start_cycle, dt_target):
        """
        终点对齐：t_end = max(pred_start_cycle) + dt_target
        每核 ideal uops = (t_end - pred_start[c]) / cpi_uop[c]
        总 uops 上限 = uop_budget
        若 sum(ideal) > budget：按超前比例 water-filling 削减（保留这一步，
                                  否则慢核会一直被快核拖死）。
        若 sum(ideal) < budget：保持 ideal（dt_target 后面会被装载率
                                  自适应升高）。
        """
        n_ideal = [
            max(self.n_min,
                int(round((t_end - pred_start_cycle[c]) / max(cpi[c], 1e-4))))
            for c in range(N)
        ]
        total = sum(n_ideal)
        if total > self.uop_budget:
            # water-filling 在 uop 空间，原理同 macro mode 但更简洁：
            # 每核 floor = n_min；按超前程度分配削减量
            shortfall = total - self.uop_budget
            min_start = min(pred_start_cycle)
            p = [max(0.0, pred_start_cycle[c] - min_start) for c in range(N)]
            if sum(p) == 0:
                p = [1.0 / cpi[c] for c in range(N)]
            sp = sum(p)
            for c in range(N):
                room = max(0.0, n_ideal[c] - self.n_min)
                cut = min(room, shortfall * p[c] / sp)
                n_ideal[c] -= int(cut)
                shortfall -= cut
        return n_ideal  # 直接是 uops，无需再除 tpm
```

**dt_target 自适应（保留，§3 D3 决策）**：

```python
def update_dt_target_uop(self, uops_used_total):
    """同 macro mode 的 update_dt_target，但 load = uops_used / uop_budget。"""
    self.step_count += 1
    if self.step_count <= self.dt_warmup:
        return self.dt_target
    load = uops_used_total / max(self.uop_budget, 1)
    self.load_ema = self.dt_alpha * load + (1 - self.dt_alpha) * self.load_ema
    ratio = self.dt_target_load / max(self.load_ema, 0.1)
    ratio = max(1 - self.dt_step_clip, min(1 + self.dt_step_clip, ratio))
    self.dt_target = max(self.dt_min, min(self.dt_max, self.dt_target * ratio))
    return self.dt_target
```

shrink-retry 循环（line 1027-1062）在 uop mode 下**完全跳过**：精确预算
不需要 retry。代码上保留 if macro mode 走 retry 分支。

### 4.4 take_uop_window 新增（line 785-803 旁）

```python
def take_uop_window(seq, start, n_uop):
    """取 [start, start+n_uop) 这一段 µop，再向后对齐到 macro 边界。"""
    end_raw = min(start + n_uop, len(seq))
    end = end_raw
    # 推到下一条 macro head（不切半条 macro）
    while end < len(seq):
        prev = seq[end - 1] if end > 0 else None
        if is_macro_head(seq[end], prev):
            break
        end += 1
    # 算实际 macros
    got_macros = sum(
        1 for i in range(start, end)
        if is_macro_head(seq[i], seq[i-1] if i > 0 else None)
    )
    return end, got_macros
```

### 4.5 推理主循环切窗（line 1015-1064）

```python
if args.budget_mode == "uop":
    for c in cores:
        n_u = next_uops[c]
        end, got_macros = take_uop_window(merged[c], cursor[c], n_u)
        per_core_wins[c] = merged[c][cursor[c]:end]
        win_end[c] = end
        tok_per_core[c] = 6 * (end - cursor[c])  # 精确，无估计
else:
    # 旧 macro mode 路径：take_macro_window + shrink-retry，注释保留
    ...
```

### 4.6 时间推进（line 1226-1230）

```python
if args.budget_mode == "uop":
    pred_cpi_uop = float(pred_pmu[ci, CPI_UOP_IDX].item())
    uops_in_win = float(len(per_core_wins[c]))
    pred_start_cycle[c] += pred_cpi_uop * uops_in_win
else:
    # legacy: pred_cpi_macro × macros
    ...
```

### 4.7 双输出报表（line 1180-1342）

每窗 dump 都计算：

```python
win_uops = sum(len(per_core_wins[c]) for c in cores)
win_macros = sum(step["instr_retired"][ci] for ci in range(n_core))
win_cycles_pred = sum(pred_cpi_uop[c] × uops_in_win[c])
win_cycles_label = sum(label_cpi_uop[c] × uops_in_win[c])

win_pred_cpi_uop   = win_cycles_pred / win_uops
win_label_cpi_uop  = win_cycles_label / win_uops
win_pred_cpi_macro = win_cycles_pred / win_macros
win_label_cpi_macro = win_cycles_label / win_macros
```

headline cycles 仍按 `sum_cyc_pred` 报，**全局 CPI 同时输出两条**：

```python
return {
    ...,
    "pred_cpi_uop": sum_cyc_pred / sum_uops,
    "label_cpi_uop": sum_cyc_label / sum_uops,
    "pred_cpi_macro": sum_cyc_pred / sum_macro,
    "label_cpi_macro": sum_cyc_label / sum_macro,
    "roi_stats_cpi_macro": roi_stats["cpi"],
    # ROI 的 cpi_uop = roi_stats["cycles"] / roi_uops，需要在 roi_stats 加 uops
    ...
}
```

### 4.8 roi_stats 加 uops 字段

[data/roi_stats.py](file:///data00/yinhaolang/LLMSim/data/roi_stats.py)
（已 import 进 eval，line 43-48）：`compute_trace_roi_stats` 返回 dict 加
`"uops"`（=trace 内 µop 总数，已遍历过）。同步 `roi_stats["cpi_uop"]`。

---

## 5. 其他脚手架同步修改

### 5.1 scripts/_diag_cpi_uop_vs_macro.py 与 scripts/_diag_b0_cpi_unit_oracle.py

这两个诊断脚本现在读旧 jsonl 的 `label[c][cpi]` + `core_split[c]` 反算 cpi_uop。
新数据集生成后，可以直接读 `label[c][cpi_uop]` 简化路径。不强求改。

### 5.2 scripts/oracle_warmup_ab.py / scripts/diagnose_cpi_outliers.py 等

这些脚本里硬编 `"cpi"` key 的地方在新数据集上会报 KeyError。需要 grep 后批量改：
```
grep -rln '"cpi"' scripts/
```
逐个看是否要改 `"cpi_uop"`。原则：诊断脚本若用旧数据集读 `label["cpi"]`，
保留；若读新数据集，改为 `label["cpi_uop"]`。

### 5.3 ckpt 兼容

新训练得到的 ckpt 与旧 ckpt 在 head 形状上等同（K=8），但 label 语义不同。
**不应该混用**。约定：
- 旧 ckpt 路径 `ckpt/phase0_ddp8_v6.2_continue_to4000` → 配旧数据集（cpi_macro）
- 新 ckpt 路径 `ckpt/phase0_ddp8_v7_cpi_uop_*` → 配新数据集（cpi_uop）

ckpt save 时写 `meta.json` 加 `label_version` 字段，load 时 warn 不匹配。

---

## 6. 任务 4：PMU 其他头 rate vs count 实验（与 cpi_uop 解耦）

文件（待创建）：`scripts/_diag_pmu_rate_vs_count.py`

目标：对每个比率头跑 rolling-mean(K) baseline，对比 rate 空间 vs count 空间
的 WAPE。这与 cpi_uop 重构**独立**，可并行做。

步骤：
1. 读现有 windows.jsonl 的 label 与 denoms。
2. 对每个 key ∈ {mpki_br, branch_mispred_frac, mr_l1d_ld, mr_l1d_st, mr_llc}：
   - y_rate[w] = label[w][key]
   - y_count[w] = y_rate[w] × denom[w][denom_of(key)]
   - rolling_pred_rate(K)、rolling_pred_count(K)
   - WAPE 在两个空间各算一次（注意：rate 空间用 abs(p-y)/max(y, ε)，
     count 空间用 abs(p-y)/max(y, max(1,p99/100))，避免 0-count 窗污染）
3. 报每 workload × 每 key 的对比表。

H1/H2/H3 假设见主文档 §5.1。预期 mr_l1d_st 因零分母最先翻转到 count 胜，
branch_mispred_frac 因分母稳定可能保持 rate 胜，mr_l1d_ld / mr_llc 处于中间
状态。

执行命令（待实现后跑）：
```
python scripts/_diag_pmu_rate_vs_count.py \
    --jsonl data/windows_v6.2_tq32k/windows.jsonl \
    --K 20 --out out/pmu_rate_vs_count.json
```

**先不改 regression_head**：等实验跑完拿到证据再做切换。

---

## 7. 实施顺序（建议）

1. **第 2-3 步合并**（label 改 + head 改）
   - data/build_windows.py、model/regression_head.py、train/loss.py、train/dataset.py、train/train_ddp_main.py 一次性改完。
   - 单元测试：build 一个小数据集（~100 sample），过 dataset → loss → backward，
     确认能跑通且 loss 数值合理（cpi_uop log 空间初始 loss 约 1.5-3.0）。
2. **重训 ckpt**
   - 新数据集 `windows_v7_cpi_uop_tq32k`。
   - 从 random init 训（旧 ckpt 不适配新语义），DDP8 跑 ~4000 step 验收。
3. **eval 改造**
   - 加 --budget-mode flag，默认 uop；旧 macro 路径注释保留。
   - 双输出 cpi_macro / cpi_uop。
4. **A/B 对照**
   - 跑 v6.2 ckpt + macro mode（旧路径回归测试）
   - 跑 v7 ckpt + uop mode（新路径主结果）
   - 报表对比：每 workload cpi_label / cpi_pred / pred_vs_label / final_drift
5. **任务 4 PMU 实验**（独立做）
6. （根据 4-5 结果决定要不要做 PMU 头空间切换）

---

## 8. 风险与回退点

- **R1**: 重训 ckpt 后 cpi_uop 在 W_compute_int 这种 cv 极小的 workload 上
  反而比 cpi_macro 准度差 → 检查是否归一化 stats / log_var 初值要重调。
- **R2**: uop budget 在 high-upm workload 上每窗实际 macros 太少 → 窗内
  噪声大 → label 标签 cv 增大 → 训练不稳。回退：把每窗最小 uops 提到
  `max(n_min × upm_global_mean, hard_floor)`。
- **R3**: dt_target 在 uop mode 装载率反馈失稳 → 改成 hardcode（去掉自适应），
  跑一组对照。
- **R4**: 旧 macro mode 代码注释保留 6 个月后没人维护，慢慢腐烂。约定 6 个月
  后若新方案稳定 → 把 macro mode 全部删除（不是注释，是 git rm）。

## 9. 验收指标

新方案对比 v6.2 baseline，至少满足：
- mem-stall workload（chase_dram / interest_graph_recall / phased_mix /
  feed_ranking / fp_lite）的 `pred_vs_label` 相对误差降低 ≥ 10%。
- upm 稳定 workload（compute_int / false_sharing / branch_storm / indirect /
  mlp_light）的 `pred_vs_label` 变化在 ±5% 内（不退化）。
- final_drift（端点对齐能力）所有 workload 都 ≤ 5%。
- eval headline cycles `sum_cyc_pred / sum_cyc_label` 在所有 workload 上 ∈ [0.9, 1.1]。

---

## 10. 变核数支持：单核放开 + MAX_CORES 8 → 32

> 本节与 cpi_uop 重构**正交**（不依赖 label 单位变化），但因都涉及 tokenizer
> 词表 / ckpt 兼容，**合并到本轮重训**一并落地，避免词表二次膨胀引发的 ckpt
> 迁移痛点。
>
> 目标：
> 1. 允许 **n_core = 1**（当前 `<2 cores` 被直接 skip）。
> 2. **MAX_CORES = 8 → 32**，词表预留 32 套 `<C{i}_BEGIN/END>` 与
>    `<QUERY_C{i}>`，使模型未来可吃 9-32 核负载而不再扩词表。

### 10.1 现状审计

| 层 | 当前状态 | 评级 |
|---|---|---|
| model/regression_head | 头权重 per-core 共享（line 6 注释明确） | 完全核数无关 |
| model/llm_wrapper | `B, n_core = query_pos.shape` 动态推 | 完全核数无关 |
| train/dataset (collate) | `max_nc = max(b["n_core"] for b in batch)` 动态 pad | 完全核数无关 |
| train/loss | 所有归约用 `core_mask.sum().clamp(min=1)` | 完全核数无关 |
| eval planner | `OnlineQuotaPlanner(n_core=len(cores), ...)` | 完全核数无关 |
| **model/tokenizer** | `MAX_CORES = 8`，词表只预生成 8 套 core token | **阻断 >8 核** |
| **data/build_windows** | 入口 `if len(files) < 2`、tq/quota 内 `if n_core < 2: return []` | **阻断单核** |
| train/dataset 默认值 | `max_cores: int = 8` 硬编 3 处 | 不阻断，但 magic number |
| scripts/prepare_dataset_cache | `max_cores=8` 硬编 1 处 | 不阻断，但 magic number |

### 10.2 需要修改的位置（按文件分组）

#### 10.2.1 单核放开（3 处硬阻断 + 退化路径 audit）

[data/build_windows.py](file:///data00/yinhaolang/LLMSim/data/build_windows.py):

1. **入口阻断** [line 1208-1209](file:///data00/yinhaolang/LLMSim/data/build_windows.py#L1208-L1209)
   ```python
   # 改前
   if len(files) < 2:
       return wd, False, 0, "", f"[skip] {wd}: <2 cores"
   # 改后
   if len(files) < 1:
       return wd, False, 0, "", f"[skip] {wd}: no cores"
   ```

2. **tq builder** [line 892](file:///data00/yinhaolang/LLMSim/data/build_windows.py#L892)
   ```python
   if n_core < 1: return []   # was < 2
   ```

3. **quota builder** [line 1038](file:///data00/yinhaolang/LLMSim/data/build_windows.py#L1038)
   ```python
   if n_core < 1: return []   # was < 2
   ```

4. **退化路径 audit（不改代码，验证逻辑正确）**：
   - align builder [line 540-625](file:///data00/yinhaolang/LLMSim/data/build_windows.py#L540-L625)：
     `min(ticks[c] for c in cores)` 单核退化为单值，`min_ts`/`t_start_rel`
     单核时 = [0.0]，**OK**。
   - timewin builder [line 643-725](file:///data00/yinhaolang/LLMSim/data/build_windows.py#L643-L725)：
     时间窗扫描不依赖 n_core ≥ 2，**OK**。
   - tq builder [line 904-905](file:///data00/yinhaolang/LLMSim/data/build_windows.py#L904-L905)：
     `t_lo = max(ticks[c][0])` / `t_hi = min(ticks[c][-1])`
     单核退化为该核 first/last tick，仍满足 `t_hi > t_lo`（除非 trace
     只有 1 条记录，但此时 `len(seq) < 2` 已被 [line 899](file:///data00/yinhaolang/LLMSim/data/build_windows.py#L899) 挡掉），**OK**。
   - quota builder [line 1049-1067](file:///data00/yinhaolang/LLMSim/data/build_windows.py#L1049-L1067)：
     `ratios = [rng.uniform(...)] * 1`、`ratio_sum = ratios[0]`、
     `budget_c = total_budget`，单核情况下 ratio 抖动失效但不报错，**OK**。
   - `encode_multicore_sample` [line 835-867](file:///data00/yinhaolang/LLMSim/data/build_windows.py#L835-L867)：
     单核只写 `<C0_BEGIN>...<C0_END>` + 单 `<QUERY_C0>`，**OK**。

#### 10.2.2 MAX_CORES 8 → 32

[model/tokenizer.py](file:///data00/yinhaolang/LLMSim/model/tokenizer.py):

1. **[line 35](file:///data00/yinhaolang/LLMSim/model/tokenizer.py#L35)**:
   ```python
   MAX_CORES = 32          # was 8
   ```
   - [line 242-243](file:///data00/yinhaolang/LLMSim/model/tokenizer.py#L242-L243) 的
     `for c in range(MAX_CORES)` 自动产生 32 套 `<C{i}_BEGIN/END/QUERY>`，
     总新增 token 数：`8 × 3 = 24` → `32 × 3 = 96`，词表净增 **72** 行。
   - 全局 token 数 1470 → 1542（约 +5%），相对 backbone 词表 ~150k 影响极小。

[train/dataset.py](file:///data00/yinhaolang/LLMSim/train/dataset.py):

2. 三处默认值替换为 `tk.MAX_CORES`，避免 magic number 漂移：
   - [line 52](file:///data00/yinhaolang/LLMSim/train/dataset.py#L52) `def build_cache_samples_from_jsonl(... max_cores: int = 8)`
   - [line 88](file:///data00/yinhaolang/LLMSim/train/dataset.py#L88) `def __init__(self, ... max_cores: int = 8, ...)`
   - [line 234](file:///data00/yinhaolang/LLMSim/train/dataset.py#L234) `def prepare_dataset_cache(... max_cores: int = 8, ...)`
   ```python
   from model import tokenizer as tk
   # ...
   max_cores: int = tk.MAX_CORES
   ```

[scripts/prepare_dataset_cache.py](file:///data00/yinhaolang/LLMSim/scripts/prepare_dataset_cache.py):

3. [line 53](file:///data00/yinhaolang/LLMSim/scripts/prepare_dataset_cache.py#L53)
   `build_cache_meta(str(data), tok, args.max_len, max_cores=8)` → 改 `tk.MAX_CORES`。

#### 10.2.3 不动的地方（已审过）

- `config/uarch_profile_arch_A.json` 的 `num_cores: 8` 是 shared_system MESI 仿真器
  的核数，与训练侧无关，按实际负载 trace 决定。
- `model/regression_head.py` / `model/llm_wrapper.py` / `train/loss.py` 全部
  动态从 input 推 `n_core`，**0 处修改**。
- `eval/eval_quota_cycles.py` planner 已经 `n_core=len(cores)` 动态，**0 处修改**。
- 各种 `_diag_*.py` / `_inspect_tstart.py` 读 `r["n_core"]` 都是动态，**0 处修改**。

### 10.3 ckpt 兼容（关键风险点）

**词表 1470 → 1542 → embedding shape mismatch**：

- 旧 ckpt（v6.2 训练得到）的 embedding 形状是 `[base_vocab + 1470, d_model]`。
- 新 tokenizer 注入后 `resize_token_embeddings` 会拿到 `[base_vocab + 1542, d_model]`。
- 直接 `model.load_state_dict(old_ckpt)` 会因 embedding 形状不匹配 fail。
- `_unfreeze_new_embeddings` 中 `new_token_start = vocab_size - n_new`
  也会因为 `n_new` 从 1470 变 1542 错位。

**处理方案（与 cpi_uop 重训合并）**：
- 本轮 cpi_uop 重构本就要求从 random init 重训（label 语义变了）。
- 把 MAX_CORES=32 一起进去，**一次重训覆盖两个变更**。
- 新 ckpt 路径建议：`ckpt/phase0_ddp8_v7_cpi_uop_mc32_*`
  （mc32 后缀标识 MAX_CORES=32）。

**ckpt meta 落地**：
- ckpt save 时新增字段 `meta["max_cores"] = tk.MAX_CORES`、
  `meta["vocab_size"] = len(hf_tokenizer)`、
  `meta["label_version"] = "v7_cpi_uop"`。
- ckpt load 时 assert 三者匹配，不匹配则 warn + 拒绝 load。
  改 train/train_ddp_main.py 一处。

### 10.4 验证点（实施后跑）

1. **单核冒烟测试**：找 / 造一个 1 核 trace，跑 build_windows，确认：
   - sample `n_core == 1`
   - token 序列只含 `<C0_BEGIN>` / `<C0_END>` / `<QUERY_C0>`
   - dataset 加载 → loss 反传不报错
2. **多核（>8）冒烟测试**：找 / 造一个 16 核 trace（或 mock 16 个伪 core 重复
   现有 8 核数据），确认：
   - tokenizer 能 inject `<C8_*>` ~ `<C15_*>` 而不报 unknown token
   - 单 sample tokens 数不超 max_len（核数翻倍则每核 budget 减半，
     需注意 16 核时 per-core token < 1000，模型可能学不动；
     **若 max_len=16384 不变，16 核可能要降到 32 核 max_len=32k 才合理**）。
3. **tokenizer round-trip**：`MAX_CORES=32` 下 build → save → load HF tokenizer，
   确认 1542 个新 token 全部 inject 成功，且 `tk.all_special_tokens()` 与
   `_unfreeze_new_embeddings` 中 `n_new` 一致。
4. **历史 8 核 ckpt 不能继续 load**：跑一次 eval 用旧 ckpt，应该看到
   shape mismatch 报错 / 拒绝 load 警告（验证 §10.3 的 meta 检查生效）。

### 10.5 实施顺序（合并到主实施计划 §7）

把 §7 的"第 2-3 步合并"扩展为：

1. **label/head/loss 改名 + MAX_CORES + 单核放开**
   - data/build_windows.py：cpi_uop + uops 字段 + 3 处单核阻断放开。
   - model/tokenizer.py：MAX_CORES = 32。
   - model/regression_head.py、train/loss.py、train/dataset.py：改 key 字符串 +
     替换硬编 max_cores。
   - scripts/prepare_dataset_cache.py：换 tk.MAX_CORES。
   - train/train_ddp_main.py：ckpt save/load 加 meta（max_cores / vocab_size /
     label_version）。
2. **重跑数据集**：新数据集名建议 `windows_v7_cpi_uop_mc32_tq32k`。
3. **重训 ckpt**：DDP8，从 random init 跑 ~4000 step。
4. **eval 改造**：同 §4。
5. **A/B 对照**：同 §7 第 4 步。

### 10.6 风险

- **R5**：32 套 core token 的 embedding 只在多核样本里被训练；如果训练集
  全部 ≤ 8 核，则 `<C8_*>` ~ `<C31_*>` 的 embedding 永远是 random init，
  未来加入 16 核 workload 时这些行是冷启动。
  → 缓解：训练集要包含至少几个 16/32 核 workload；或先训 8 核 baseline，
    再加多核 workload fine-tune（warm start）。
- **R6**：单核 workload `core_mask` 永远是 `[1.0]`，invariance loss 项
  ([train/loss.py L150-156](file:///data00/yinhaolang/LLMSim/train/loss.py#L150-L156))
  退化为 hinge(0.25 - cpi) 只对单核生效，不会引入额外问题。
- **R7**：单核 trace 没有 `t_start_rel` 跨核对齐信号，tstart_proj 输入恒为 0,
  对模型无害（zeros_init 已保证起点等价于不注入）。

## 11. seed 化 14 个训练 workload（train/infer 分布相似但不同）

### 11.0 目的与边界（必读）

**这一节只解决一件事**：让 **train trace（seed=A） 和 infer trace（seed=B）**
在 PMU/cpi/uops 整体分布上**相似但不完全相同**，给模型一个"OOD-but-same-class"
的泛化检验信号。

**这一节不解决**：
- 单 trace 内**窗口高度重复**（W_compute_int eff_n=2 / W_false_sharing eff_n=2）。
  这是单 trace 时间稳态问题，和 seed 完全无关 → 单独放到 §12。
- 不引入跨窗 phase 切换。同一 workload 在不同窗有相同稳态结构是**正确**的；
  让 phase 跟着 seed 走会让 "train vs infer" 的分布偏移过大、本质上变成
  "两个 workload"，违反"相似但不同"的目的。

**约束（硬性）**：
1. seed 影响范围**只在 init 阶段的常量派生**（rng 起点、查表初值、hash 表
   key 池等）。**hot loop 的结构 / 控制流 / 循环次数 / scale / 输入规模 完全
   不变**。
2. 14 个训练 workload **全部加 seed** 入口；当 g_tao_seed=0（默认）时行为
   **退化为现有 trace bit-equal**，保证旧采集结果可复现。
3. **不**新增 phase 切换变量、**不**让循环次数随 seed 变化、**不**改
   working-set 大小、**不**改采集脚本里 scale 的选择。

### 11.1 14 训练 workload 清单与现状

来源：[logs/window_duplication_v6.2.json](file:///data00/yinhaolang/LLMSim/logs/window_duplication_v6.2.json)
（14 行，覆盖当前训练集全部）。

| # | workload | 已读 seed？ | init 端 rng 入口（待 seed 化） |
|---|----------|-------------|-------------------------------|
| 1 | ads_ctr | 否 | `r = 0xd13...95ULL ^ (tid+11) * 0x94d049bb` ([L35](file:///data00/yinhaolang/LLMSim/workloads/src/bench_ads_ctr.c#L35)) |
| 2 | branch_storm | 否 | `r = tid * 0x9e37...c15ULL + 12345` ([L14](file:///data00/yinhaolang/LLMSim/workloads/src/bench_branch_storm.c#L14)) |
| 3 | chase_dram | 否 | `r = tid * 0x9e37...c15ULL + 1` ([L15](file:///data00/yinhaolang/LLMSim/workloads/src/bench_chase_dram.c#L15)) |
| 4 | compute_int | 否 | `x/y/z/w = tid * 0x12345/0x67890/0xabcde/0xf0f0f + odd` ([L12-13](file:///data00/yinhaolang/LLMSim/workloads/src/bench_compute_int.c#L12-L13)) |
| 5 | false_sharing | 否 | `slot = tid % 8`（**没有 rng**，是结构常量） ([L13](file:///data00/yinhaolang/LLMSim/workloads/src/bench_false_sharing.c#L13)) |
| 6 | feed_ranking | 否 | `r = 0x9e37...c15ULL ^ (tid+1) * 0x94d049bb` ([L27](file:///data00/yinhaolang/LLMSim/workloads/src/bench_feed_ranking.c#L27)) |
| 7 | fp_compute_dense | 否 | `acc/a/b/c/d/idx = tid * 常量 + odd`（无 rng） ([L39-42](file:///data00/yinhaolang/LLMSim/workloads/src/bench_fp_compute_dense.c#L39-L42)) |
| 8 | fp_lite | 否 | `x/y/f0/f1/f2/ii/fi = tid * 常量 + odd`（无 rng） ([L41-48](file:///data00/yinhaolang/LLMSim/workloads/src/bench_fp_lite.c#L41-L48)) |
| 9 | indirect | 否 | `r = tid * 0x9e37...c15ULL + 7` + `tab[6]={op_add,...}` 排列固定 ([L20-22](file:///data00/yinhaolang/LLMSim/workloads/src/bench_indirect.c#L20-L22)) |
| 10 | int_div | 否 | `x = tid * 0x9e37...c15ULL + 1`，d0..d3 是 `i` 的函数（不可改） ([L14-19](file:///data00/yinhaolang/LLMSim/workloads/src/bench_int_div.c#L14-L19)) |
| 11 | interest_graph_recall | 否 | `r = 0x517c...95ULL ^ (tid+1) * 0x9e3779b1` ([L25](file:///data00/yinhaolang/LLMSim/workloads/src/bench_interest_graph_recall.c#L25)) |
| 12 | mlp_light | 否 | `seed = 0x243f...d3ULL ^ (tid << 33)` ([L51](file:///data00/yinhaolang/LLMSim/workloads/src/bench_mlp_light.c#L51)) |
| 13 | phased_mix | 否 | `r = tid * 0x9e37...c15ULL + 1` ([L24](file:///data00/yinhaolang/LLMSim/workloads/src/bench_phased_mix.c#L24)) |
| 14 | stream | 否 | `b[i]=1.0+tid; c[i]=2.0`（无 rng，常量数组） ([L13](file:///data00/yinhaolang/LLMSim/workloads/src/bench_stream.c#L13)) |

注：[tao_bench.h](file:///data00/yinhaolang/LLMSim/workloads/common/tao_bench.h#L51) 已经
预留 `g_tao_seed`（从 `argv[4]` 解析，默认 0），**14 个训练 workload 都没读**。

### 11.2 改造模板：splitmix64 派生 init 常量

在 `tao_bench.h` 加一个 inline helper（不污染单文件，复用率高）：

```c
/* Per-(seed, tid, slot) splitmix64 派生。slot 让同一线程能拿到多路独立常量。
 * 当 g_tao_seed == 0 且 callsite 用 slot=0 时，要求返回值等价于旧硬编常量；
 * 各 callsite 通过额外异或现有硬编常量来锚定向后兼容。 */
static inline uint64_t tao_seed_mix(int tid, uint32_t slot)
{
    uint64_t z = g_tao_seed + (uint64_t)(tid + 1) * 0x9E3779B97F4A7C15ULL
               + (uint64_t)slot * 0xBF58476D1CE4E5B9ULL;
    z = (z ^ (z >> 30)) * 0xBF58476D1CE4E5B9ULL;
    z = (z ^ (z >> 27)) * 0x94D049BB133111EBULL;
    return z ^ (z >> 31);
}
```

**通用改造模板**：

```c
/* 改造前 */
uint64_t r = (uint64_t)tid * 0x9e3779b97f4a7c15ULL + 1;

/* 改造后（seed=0 时 hot loop trace bit-equal） */
uint64_t r = tao_seed_mix(tid, /*slot=*/0)
           ^ ((uint64_t)tid * 0x9e3779b97f4a7c15ULL + 1);
```

兼容性原理：`tao_seed_mix(tid, 0)` 在 `g_tao_seed=0` 时是
`splitmix64((tid+1)*0x9E37...C15)`，对**固定 tid** 是一个 **fixed** 常量，把它
与旧 init 异或会改 trace bit-pattern → 不兼容 seed=0 退化。

**解决**：g_tao_seed=0 时直接走旧路径：

```c
/* 真正使用的模板，保证 seed=0 时 trace bit-equal */
uint64_t r = (g_tao_seed == 0)
           ? ((uint64_t)tid * 0x9e3779b97f4a7c15ULL + 1)
           : tao_seed_mix(tid, /*slot=*/0);
```

或更紧凑：

```c
/* 把所有 r/x/y 等 init 常量集中到一个 init helper */
static inline uint64_t tao_seed_or(int tid, uint32_t slot, uint64_t fallback)
{
    return (g_tao_seed == 0) ? fallback : tao_seed_mix(tid, slot);
}
```

callsite 写成：

```c
uint64_t r = tao_seed_or(tid, 0, (uint64_t)tid * 0x9e3779b97f4a7c15ULL + 1);
```

**约定**：每个 workload 内 slot 编号从 0 递增。比如 compute_int 里 x=slot0,
y=slot1, z=slot2, w=slot3。

### 11.3 14 workload 改造点表（精确到行）

> 改动一律遵守"seed=0 → bit-equal 旧行为"原则。所有改动**只在 init 段**，
> hot loop **不动**。

| # | workload | 改动 | seed=0 兼容 |
|---|----------|------|-------------|
| 1 | ads_ctr | [L35](file:///data00/yinhaolang/LLMSim/workloads/src/bench_ads_ctr.c#L35) 把 `r = 0xd1...^(tid+11)*0x94d049bb` 改成 `tao_seed_or(tid, 0, 旧)` | ✓ |
| 2 | branch_storm | [L14](file:///data00/yinhaolang/LLMSim/workloads/src/bench_branch_storm.c#L14) `r = tid*0x9e37c15+12345` → `tao_seed_or(tid, 0, 旧)` | ✓ |
| 3 | chase_dram | [L15](file:///data00/yinhaolang/LLMSim/workloads/src/bench_chase_dram.c#L15) `r = tid*0x9e37c15+1` → `tao_seed_or(tid, 0, 旧)` | ✓（hot loop 只 follow next，seed 改 permutation） |
| 4 | compute_int | [L12-13](file:///data00/yinhaolang/LLMSim/workloads/src/bench_compute_int.c#L12-L13) x/y/z/w 4 个起点用 slot 0..3 派生 | ✓ |
| 5 | false_sharing | [L13](file:///data00/yinhaolang/LLMSim/workloads/src/bench_false_sharing.c#L13) `slot = tid % 8` **不动**（结构性常量，改了就改 hot loop 行为）；改：在 line[] init 时（位于 tao_bench.h 的 shbytes 之外，由 worker0 来写）按 seed 写 4 个起始值（每 `long` 8B × 8 slots = TAO_LINE×4 字节）| **基本等价**（seed=0 时 line 全 0，与旧 calloc 一致） |
| 6 | feed_ranking | [L27](file:///data00/yinhaolang/LLMSim/workloads/src/bench_feed_ranking.c#L27) `r = 0x9e37c15 ^ (tid+1)*0x94d049bb` → `tao_seed_or(tid, 0, 旧)` | ✓ |
| 7 | fp_compute_dense | [L39](file:///data00/yinhaolang/LLMSim/workloads/src/bench_fp_compute_dense.c#L39) 把 `acc = tid*0.1+1.0` / `a/b/c/d` / `idx = tid*17u` 这 6 个 init 标量按 slot 0..5 派生（`a/b/c/d` 取派生值映到 `[0.9990, 1.0010]` 窄区间，保证 IPC 不漂移） | seed=0 时 `acc = tid*0.1+1.0`、`a=1.0001`... 完全一致 |
| 8 | fp_lite | [L41-48](file:///data00/yinhaolang/LLMSim/workloads/src/bench_fp_lite.c#L41-L48) x/y/f0/f1/f2/ii/fi 7 个 init 用 slot 0..6 派生（系数 a/b/c/d 不改） | ✓ |
| 9 | indirect | [L20](file:///data00/yinhaolang/LLMSim/workloads/src/bench_indirect.c#L20) `tab[6]` **保持顺序不动**（如果 seed 重排 tab 会改变间接分支统计分布，但**结构**不变；分布层 OK），改 [L21](file:///data00/yinhaolang/LLMSim/workloads/src/bench_indirect.c#L21) `r = tid*0x9e37c15+7` → `tao_seed_or(tid, 0, 旧)` + acc 初值用 slot1 派生 | ✓ |
| 10 | int_div | [L14](file:///data00/yinhaolang/LLMSim/workloads/src/bench_int_div.c#L14) `x = tid*0x9e37c15+1` → `tao_seed_or(tid, 0, 旧)`；d0..d3 是 `i` 的函数**不能改**（改了会改 divide 周期分布的"难度"）；改 acc 初值用 slot1 派生 | ✓ |
| 11 | interest_graph_recall | [L25](file:///data00/yinhaolang/LLMSim/workloads/src/bench_interest_graph_recall.c#L25) `r = 0x517c95 ^ (tid+1)*0x9e3779b1` → `tao_seed_or(tid, 0, 旧)` | ✓ |
| 12 | mlp_light | [L51](file:///data00/yinhaolang/LLMSim/workloads/src/bench_mlp_light.c#L51) `seed = 0x243f...d3 ^ (tid<<33)` → `tao_seed_or(tid, 0, 旧)` | ✓ |
| 13 | phased_mix | [L24](file:///data00/yinhaolang/LLMSim/workloads/src/bench_phased_mix.c#L24) `r = tid*0x9e37c15+1` → `tao_seed_or(tid, 0, 旧)`；acc 初值 [L39](file:///data00/yinhaolang/LLMSim/workloads/src/bench_phased_mix.c#L39) `acc = tid+1` 用 slot1 派生 | ✓ |
| 14 | stream | [L13](file:///data00/yinhaolang/LLMSim/workloads/src/bench_stream.c#L13) `b[i] = 1.0 + tid; c[i] = 2.0` → `b[i] = base_b + tid; c[i] = base_c`，base_b/base_c 在 `[1.0, 1.001]` / `[2.0, 2.001]` 用 slot0/slot1 派生 1 次（不进 i loop）；保证仍是顺序 stride-1 流式访存 | ✓ |

**改造范围统计**：
- 14 workload，每个 4-10 行 init 段改动，hot loop 完全不动。
- 新增 helper：`tao_bench.h` 加 `tao_seed_mix` + `tao_seed_or` 两个 inline（~12 行）。

### 11.4 false_sharing 单独说明

false_sharing 的特殊性在于：它没有 rng，只有 `slot = tid % 8` 这个结构常量
决定每个线程在 cacheline 上的位置。**不能改 slot 公式**，否则改变冲突结构。

**可改的 seed 入口**：
- `line[slot]` 的**初值**（worker0 在 alloc 后写入 4 个 long，每 long 不同），
  使得 hot loop `line[slot] += i; line[slot] ^= line[(slot+1) % ...]` 起点不同
  → 累加序列的 mod-2 模式不同 → 微改 PMU（uops 总数完全一致，CPI 微差）。
- 注意 `shbytes = TAO_LINE * 4`，alloc 的 buffer 由 `tao_xaligned` calloc 清零。
  seed=0 时不写初值 → 行为完全等同旧版。seed!=0 时只有 tid==0 写初值。

```c
/* 改造后 */
static void kernel(int tid, int nthreads, long scale, void *shared)
{
    long iters = scale * 1000;
    volatile long *line = (volatile long *)shared;
    int slot = tid % 8;
    /* seed 入口：tid 0 在 hot loop 前写 4 路不同初值（不动结构）。
     * seed=0 时跳过，line 全 0，与旧版完全一致。 */
    if (tid == 0 && g_tao_seed != 0) {
        for (int s = 0; s < 8; s++) {
            line[s] = (long)tao_seed_mix(s, /*slot=*/0);
        }
        /* 防止编译器把这段移到 ROI 内（结构上 ROI 在 worker 自动包，没问题） */
        __sync_synchronize();
    }
    for (long i = 0; i < iters; i++) {
        line[slot] += i;
        line[slot] ^= line[(slot + 1) % (nthreads > 8 ? 8 : nthreads)];
    }
}
```

**风险**：tid==0 在 ROI 内做初始化，会额外多 8 条 store + 1 fence。在 1199 窗
里只影响前 1-2 个窗的 PMU。可接受。如果不接受，把 init 挪到 `shared` 申请时
（需要改 `tao_bench.h` 的 alloc 路径，更侵入）。**推荐**接受这点污染。

### 11.5 stream 单独说明

stream 同样没有 rng，靠 b/c 数组初值驱动。改造范围：

```c
/* 改造后 */
size_t n = (size_t)scale * 1024;
double *a = (double *)tao_xaligned(n * sizeof(double));
double *b = (double *)tao_xaligned(n * sizeof(double));
double *c = (double *)tao_xaligned(n * sizeof(double));
/* seed 入口：base_b / base_c 在 [1.0, 1.001) / [2.0, 2.001) 微抖。
 * seed=0 时 base_b=1.0, base_c=2.0，与旧版完全一致。 */
double base_b = 1.0;
double base_c = 2.0;
if (g_tao_seed != 0) {
    base_b = 1.0 + (double)(tao_seed_mix(tid, 0) & 0xfff) / 4.096e6;  /* [1.0,1.001) */
    base_c = 2.0 + (double)(tao_seed_mix(tid, 1) & 0xfff) / 4.096e6;  /* [2.0,2.001) */
}
for (size_t i = 0; i < n; i++) { b[i] = base_b + tid; c[i] = base_c; }
const double q = 3.0;
for (int rep = 0; rep < 4; rep++)
    for (size_t i = 0; i < n; i++)
        a[i] = b[i] + q * c[i];
```

效果：FP 操作数值微变，IPC/uops/cache 行为基本相同（值改变不改控制流和
访问模式）。

### 11.6 采集脚本：seed 传递（不在本节实施）

> 用户已确认 "采样策略先别管，我后面自己采"，本节只**预留**接口，不动采集
> 脚本。下面是预期的采集端接入点描述（实施时自取）。

- `argv[4] = seed`（已存在，[tao_bench.h L142](file:///data00/yinhaolang/LLMSim/workloads/common/tao_bench.h#L142)）。
- 采集脚本（推测在 `scripts/sample_workloads_gem5.py` 或类似位置）的命令拼装
  需要在 `--workload-args "<nthreads> <scale> <roi>"` 后加 `<seed>`。
- 数据 manifest 里加 `seed` 字段，build_windows 端读 manifest 时不需要处理
  seed（PMU 都是黑盒）。

### 11.7 ckpt / 数据集兼容

- 训练数据集和推理数据集**不是同一份**。建议命名：
  - `windows_v7_cpi_uop_mc32_train_seedA`（采集时 seed=A）
  - `windows_v7_cpi_uop_mc32_infer_seedB`（采集时 seed=B）
  - 同 workload 同 scale 同 nthreads，只差 seed。
- seed=0 数据集（旧采集）仍然可用，等价于 `seed=0`。
- ckpt 不受 seed 影响（模型完全黑盒看 PMU）。

### 11.8 验收指标

完成 §11 后跑：
- **D1**（分布相似性）：对每个 workload，比较 `(seed=A trace)` 和
  `(seed=B trace)` 的窗级 PMU 分布：
  - 关键标签：`cpi_uop_mean`, `uops_per_core`, `branch_miss_rate`, `mshr_mean`,
    `llc_miss_rate`, `mem_acc_per_uop`。
  - 每个标签的 trace-mean 偏差 `|mean(A) - mean(B)| / max(mean(A), 1e-9) < 5%`。
  - 每个标签的 trace-CV 偏差 `|cv(A) - cv(B)| < 0.05`。
  - **必须落在 [3%, 8%] 之间**：太小（<3%）说明 seed 没起作用；太大（>10%）
    说明 init 改动泄漏到 hot loop 了，需要回查。
- **D2**（trace 间 NN 距离）：训练集每条 trace 取 100 窗，infer trace 取 100 窗,
  跨集 NN 距离（35-d feature z-score 后 L2）应在
  `[0.05, 0.30]` 区间（同分布但不重合）。
- **D3**（seed=0 退化）：用旧采集脚本（不传 argv[4]）跑 14 workload，
  PMU trace **bit-equal** 当前 main 分支结果。

### 11.9 实施顺序（合并到 §7 / §10.5）

1. tao_bench.h 加 `tao_seed_mix` + `tao_seed_or` 两个 inline helper（~12 行）。
2. 14 个 bench_*.c 按 §11.3 改 init 段（每个 4-10 行）。
3. **本地 -O0 编译 + 不传 argv[4]** 跑一遍 → diff 物理机 perf 与旧版应一致。
4. **本地编译 + 传 argv[4]=42** 跑一遍 → diff 物理机 perf 与旧版应在 ±5%。
5. 采集端联调（用户自行处理）。
6. 验收 §11.8 D1/D2/D3。

### 11.10 R8-R10 风险

- **R8**：fp_compute_dense / fp_lite 的 `a/b/c/d` 系数派生范围必须紧
  （`[0.9990, 1.0010]`），过宽会让 FP 数值发散，IPC 偏移会突破 5%。
- **R9**：indirect 的 `tab[6]` 顺序如果未来想加 seed 重排，需要重测 branch
  miss 分布。**当前 plan 不改 tab**，保守。
- **R10**：false_sharing 的 tid==0 写 8 个 long 会落到第 1-2 个窗里，造成
  早期窗 PMU 微污染。如果训练时切窗 stride 较小（如 100 个 token），第 1 窗
  CPI 可能偏低 5-10%。验收时关注 D1 是否在前 5 个窗也满足。

## 12. 窗口高度重复修复（独立于 seed 体系）

### 12.0 背景与目的

来自实测 [logs/window_duplication_v6.2.json](file:///data00/yinhaolang/LLMSim/logs/window_duplication_v6.2.json)：

| workload | n_win | effective_n | NN<0.05 占比 |
|----------|-------|-------------|--------------|
| W_compute_int | 1198 | **2** | 99.5% |
| W_false_sharing | 1199 | **2** | 99.7% |
| W_int_div | 1199 | 9 | 95.6% |
| W_branch_storm | ~1200 | 10 | 91.9% |
| W_phased_mix | ~1200 | 720 | 6% |
| W_indirect | ~1200 | 80 | 23.3% |
| W_stream | ~1200 | 162 | 56.8% |
| ...（其余 7 个见 v6.2.json） | | | |

含义：W_compute_int 的 1198 个窗在 35-d 特征上**几乎只有 2 个不同状态**。
模型上等价于"1 个样本被复制 1198 次"，loss 收敛后无法分辨 OOD/同分布,
**WAPE 的可信度极低**。

**§12 的目的**：让单 trace 内**窗与窗之间**的 PMU 有自然漂移，提高 effective_n。

**§12 的边界**：
- 不和 seed 挂钩（按用户指示）。seed 决定 trace 间差异，phase 决定 trace 内差异，
  两者**正交解耦**。
- 用户授权方向：**多 phase**（不挂 seed）。多 phase 在同一 trace 内是
  **确定性**的，每次采集相同。

### 12.1 多 phase 设计原则

**正交于 seed**：phase 切换由 `i / iters_per_phase`（外层 loop index）决定，
完全确定性，不读 g_tao_seed。换言之：

```
trace(seed=A) 的窗序列 = [phase0_seedA, phase1_seedA, ..., phase7_seedA]
trace(seed=B) 的窗序列 = [phase0_seedB, phase1_seedB, ..., phase7_seedB]
```

`phase_k_seedA` 和 `phase_k_seedB` **相似但不同**（seed 差异），
`phase_j_seedA` 和 `phase_k_seedA` **明显不同**（trace 内 phase 差异）。
这样 §11 的 seed 目标和 §12 的 phase 目标互不破坏。

**保留稳态特征**：每个 phase 内仍然是稳态结构（一段连续窗内 PMU 相似），
不要让每个迭代都换 phase（否则 cache/branch predictor 跟不上，PMU 退化为
噪声）。

**只动 effective_n < 50 的 4 个 workload**：compute_int, false_sharing,
int_div, branch_storm。其余 10 个 workload effective_n ≥ 80，等下个版本再说。

### 12.2 phase 切换模板

```c
/* 通用模板：N_PHASE 段稳态拼接，每段 iters_per_phase 个迭代。
 * - phase 边界对齐 build_windows 的切窗 stride（让 phase 转换不被同一窗吞掉）。
 * - phase 内行为是同一稳态，跨 phase 是不同稳态。
 * - 完全不读 g_tao_seed。 */
#define N_PHASE 8

static void kernel(int tid, int nthreads, long scale, void *shared)
{
    long iters = scale * 1000;
    long iters_per_phase = iters / N_PHASE;
    if (iters_per_phase < 1) iters_per_phase = 1;
    /* ... seed-driven init（见 §11） ... */
    for (int ph = 0; ph < N_PHASE; ph++) {
        /* phase ph 的"风味"参数：从 ph 派生（不读 seed） */
        int knob = phase_knob(ph);  /* 每个 workload 自定义 */
        for (long i = 0; i < iters_per_phase; i++) {
            /* hot loop 用 knob 调一个**结构常量** */
            ...
        }
    }
}
```

### 12.3 四个 workload 的 phase knob 设计

#### 12.3.1 compute_int

5 行 ALU 长依赖链：`x = x*K1 + K2; y = (y^(y>>S)) + i; z = z + (z<<L) - x;
w = (w*M + y) ^ (z>>R); x = x + w;`。

每 phase 切换 `shift S`（13 → 17/11/19/...）和 `(K1, K2)`。改 shift 会改变
`y` 的混合速度 → 影响 IPC 微观结构；改 `K1, K2` 改变乘加链的稳态行为。
**不改循环结构、不改 5 行的代数公式形状**。

```c
static const long PH_K1[N_PHASE] = {
    0x9e3779b1L, 0x517cc1b7L, 0xc2b2ae3dL, 0xbf58476dL,
    0x94d049bbL, 0x85ebca6bL, 0xa3b195f3L, 0xd1b54a32L,
};
static const long PH_K2[N_PHASE] = {
    0x12345, 0x67890, 0xabcdef, 0x13579b,
    0x2468ac, 0x369cf0, 0x5a5a5a, 0xa5a5a5,
};
static const int  PH_SHIFT[N_PHASE] = {13, 17, 11, 19, 7, 23, 13, 17};

for (int ph = 0; ph < N_PHASE; ph++) {
    long k1 = PH_K1[ph], k2 = PH_K2[ph];
    int s = PH_SHIFT[ph];
    for (long i = 0; i < iters_per_phase; i++) {
        x = x * k1 + k2;
        y = (y ^ (y >> s)) + i;
        z = z + (z << 5) - x;
        w = (w * 5 + y) ^ (z >> 7);
        x = x + w;
    }
}
```

预期：effective_n 从 2 → ≥ N_PHASE = 8（粗略下限），实测目标 ≥ 16。

#### 12.3.2 false_sharing

hot loop 是 2 行：`line[slot] += i; line[slot] ^= line[(slot+1) % ...]`。

phase knob：**交替** owner-set vs sharer-set 的字段位置（`slot` 公式分段）：

```c
for (int ph = 0; ph < N_PHASE; ph++) {
    /* 偶数 phase：所有线程紧密争抢 8 个字段（最大 bouncing）；
     * 奇数 phase：每线程跳到自己的远端字段（同行内 stride 4，bouncing 减半）。
     * 4 字段对一行（TAO_LINE=64B, 4 个 16B group）。 */
    int slot;
    if ((ph & 1) == 0) slot = tid % 8;
    else               slot = (tid + 4) % 8;
    int neigh_mod = (ph & 2) ? 4 : (nthreads > 8 ? 8 : nthreads);
    for (long i = 0; i < iters_per_phase; i++) {
        line[slot] += i;
        line[slot] ^= line[(slot + 1) % neigh_mod];
    }
}
```

预期：bouncing pattern 在 phase 间显著差异（M->I 速率不同）→
inv_recv / coherence stall 分布拉开 → effective_n ≥ 8。

**注意**：`slot` 跨 phase 切换会让某些 phase 内 cacheline 状态不一样。
**这是设计意图**，不是 bug。

#### 12.3.3 int_div

5 行核心：`acc += x/d0; x = x*K + acc + C; acc ^= x%d1; x ^= acc/d2; acc += x%d3;`。
d0..d3 从 `i` 派生**不能动**。

phase knob：改 `K, C`（让 `x` 的演化速度不同 → divide 的"被除数分布"不同 →
divide 周期分布不同）。

```c
static const uint64_t PH_KX[N_PHASE] = {
    6364136223846793005ULL, 2862933555777941757ULL, 6906969069LL, 1103515245ULL,
    134775813ULL,            3935559000370003845ULL, 2685821657736338717ULL, 0xb5297a4d,
};
static const uint64_t PH_CX[N_PHASE] = {
    1442695040888963407ULL, 3037000493ULL, 1ULL, 12345ULL,
    1ULL,                    2891336453ULL, 1ULL, 12820163ULL,
};

for (int ph = 0; ph < N_PHASE; ph++) {
    uint64_t kx = PH_KX[ph], cx = PH_CX[ph];
    for (long i = 0; i < iters_per_phase; i++) {
        uint64_t d0 = ((uint64_t)(i * 4 + 1) * 2654435761ULL) | 1ULL;
        uint64_t d1 = ((uint64_t)(i * 4 + 3) * 2246822519ULL) | 1ULL;
        uint64_t d2 = ((uint64_t)(i * 4 + 5) * 3266489917ULL) | 1ULL;
        uint64_t d3 = ((uint64_t)(i * 4 + 7) * 668265263ULL)  | 1ULL;
        acc += x / d0;
        x = x * kx + acc + cx;
        acc ^= x % d1;
        x ^= acc / d2;
        acc += x % d3;
        asm volatile("" : "+r"(x), "+r"(acc) :: "memory");
    }
}
```

预期：x 的稳态值域跨 phase 差异 → divide 平均周期跨 phase 差异 →
effective_n 从 9 → ≥ 24。

#### 12.3.4 branch_storm

8 行混合分支。phase knob：改 4 个偏斜阈值。

```c
static const unsigned PH_THR_LOW[N_PHASE]  = {1, 3, 5, 1, 3, 5, 7, 1};
static const unsigned PH_THR_HIGH[N_PHASE] = {4, 2, 6, 4, 2, 6, 0, 5};
static const unsigned PH_MASK1[N_PHASE]    = {12, 6, 14, 12, 6, 14, 10, 12};
static const unsigned PH_MASK2[N_PHASE]    = {7, 3, 15, 7, 3, 15, 11, 7};

for (int ph = 0; ph < N_PHASE; ph++) {
    unsigned thr_lo = PH_THR_LOW[ph], thr_hi = PH_THR_HIGH[ph];
    unsigned m1 = PH_MASK1[ph], m2 = PH_MASK2[ph];
    for (long i = 0; i < iters_per_phase; i++) {
        r = r * 6364136223846793005ULL + 1442695040888963407ULL;
        unsigned bits = (unsigned)(r >> 33);
        if (bits & 1) a += i; else b -= i;
        if (bits & 2) c ^= a; else c += b;
        if ((bits & m1) == 0) a = (a << 1) | 1;
        if (((bits >> 4) & m2) > thr_hi) b ^= c; else a += c;
        r ^= a + (b << 1) + (c << 3);
        bits = (unsigned)(r >> 29);
        if (bits & 1) c += r; else c ^= (a + b);
        if ((bits & 6) == thr_lo) a ^= c; else b += a;
        asm volatile("" : "+r"(r), "+r"(a), "+r"(b), "+r"(c) :: "memory");
    }
}
```

预期：分支偏斜跨 phase 切换 → branch predictor 在每个 phase 内有不同稳态命中
率 → effective_n 从 10 → ≥ 24。

### 12.4 phase 边界与 build_windows 切窗对齐

[scripts/analyze_window_duplication.py](file:///data00/yinhaolang/LLMSim/scripts/analyze_window_duplication.py)
说明窗是按 token budget 切的。每个 trace 当前 ≈ 1200 窗。

- N_PHASE=8 → 每 phase ≈ 150 窗，phase 内稳态足够样本。
- phase 转换瞬间会出现 1-2 个"过渡窗"（cache/predictor warm-up）。
  这些过渡窗在 NN 距离上是**新**样本，反而抬高 effective_n，是正收益。

**不需要改 build_windows 切窗策略**。

### 12.5 验收指标

- **D4**：跑完 §12 改造后，4 个 workload 的 effective_n：
  - W_compute_int ≥ 16（原 2）
  - W_false_sharing ≥ 12（原 2）
  - W_int_div ≥ 24（原 9）
  - W_branch_storm ≥ 24（原 10）
- **D5**：同一 workload 在 seed=A vs seed=B 下，effective_n 偏差 < 20%
  （phase 切换是确定性的，seed 只改 init → phase pattern 在 seed 维度稳定）。
- **D6**：4 个改过 phase 的 workload，整体 PMU 均值 vs 旧版（无 phase 的 main）
  应在 ±15% 内（结构未变；如果飞太多说明 phase knob 选过激）。

### 12.6 实施顺序

1. §11 全部落地并通过 D1/D2/D3。
2. §12 4 个 workload 加 phase loop（hot loop 外多一层 `for (ph)`）。
3. **本地编译 -O0 + 跑物理机 perf**：4 个 workload 各跑 seed=0 + seed=42，
   各采 1 条 trace（~1200 窗）。
4. 跑 [scripts/analyze_window_duplication.py](file:///data00/yinhaolang/LLMSim/scripts/analyze_window_duplication.py)
   出 effective_n，对照 D4/D5。
5. 如果 D4 没达标：N_PHASE 8 → 12，或扩大 knob 表的差异范围。
6. 重新跑 5 个 workload 的训练集采集 → 重训 ckpt。

### 12.7 R11-R13 风险

- **R11**：phase 切换在 trace 头部（第 1 phase）有一段无前置 cache 状态，
  PMU 与中间 phase 略不同。对 effective_n 是正贡献，对模型可能引入轻微
  warmup-bias。可在 build_windows 端选择丢弃前 50 窗（已有 warmup_skip
  机制）。
- **R12**：N_PHASE=8 × iters_per_phase = iters，但 `iters / 8` 取整后会少
  最多 7 次迭代。对 ROI 长度影响 < 0.1%，忽略。
- **R13**：false_sharing 的 phase knob `slot` 切换会导致 cacheline 在 phase
  转换瞬间出现一次 invalidation 风暴尖峰。预期 1-2 窗 PMU 跳变，与设计意图
  一致；但要确认尖峰窗不会因为 PMU 异常被 build_windows 当 outlier 丢弃。
  检查 [data/build_windows.py](file:///data00/yinhaolang/LLMSim/data/build_windows.py)
  的 outlier 过滤逻辑（若有 z-score 阈值，对 phase 转换窗放宽）。

## 13. §11 + §12 与主任务的关系

- §11 + §12 的实施**不阻塞** §1-§4（cpi_uop 重构）。两条独立路径。
- 但**采集**阶段要合并：因为 §10 重训 ckpt + §11 加 seed + §12 改 4 workload
  都要求**重新采集训练集**。建议一次性整理后采集：
  1. 实施 §1-§4（label/head/loss 改 cpi_uop） + §10（MAX_CORES=32 + 单核）
     + §11（seed 化 14 workload） + §12（4 workload 加 phase）。
  2. 一次重采：`windows_v7_cpi_uop_mc32_train_seedA_phase8`。
  3. 一次重训：`ckpt/phase0_ddp8_v7_cpi_uop_mc32_seed_phase_*`。
  4. 推理评估：另采 `windows_v7_cpi_uop_mc32_infer_seedB_phase8`。
- 单测顺序仍按 §7 / §10.5：先 label/dataset 通；再 ckpt 起训；再加 seed/phase
  做 OOD 验证。
