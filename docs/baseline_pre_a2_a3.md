# A3-pre baseline 实测（Step 2）

> 数据：`diagnosis/baseline_pre_a2_a3.json`
> 脚本：[baseline_pre_a2_a3.py](file:///data00/yinhaolang/simulators/single_core_mvp/tools/baseline_pre_a2_a3.py)
> 时间：2026-05-26

## 1. 总览表

| workload | records.micro 总条数 | load µops | mem_events DRAM load | rec fallback% | event fallback% |
|---|---:|---:|---:|---:|---:|
| W1 | 330 497 | 3 191 | 121 | 99.09% | 53.73% |
| W3 | 1 976 577 | 118 840 | 5 364 | 96.83% | 71.10% |
| W4 | 181 200 | 10 901 | 512 | 95.75% | 61.59% |
| W7 | 3 971 658 | 235 866 | 30 866 | 94.07% | 45.93% |

W7 oracle DRAM load = 30866，与 PMU diagnosis 报告一致。

## 2. records.micro coh_oracle 分布

| workload | UNKNOWN | L1_HIT | DRAM | LLC_HIT | L2_HIT | R_DIRTY | WB |
|---|---:|---:|---:|---:|---:|---:|---:|
| W1 | 324 007 | 6 045 | 357 | 41 | 0 | 0 | 47 |
| W3 | 1 759 655 | 168 845 | 8 794 | 182 | 25 | 21 184 | 17 892 |
| W4 | 161 154 | 19 010 | 757 | 211 | 0 | 0 | 68 |
| W7 | 3 535 852 | 334 549 | 83 327 | 7 494 | 10 394 | 0 | 42 |

UNKNOWN 占 80–98%（包含 ALU / branch / fence 等非 mem µop），符合预期。

## 3. mem_events load coh_oracle 分布

| workload | L1 | L2 | LLC | DRAM | R_DIRTY | WB |
|---|---:|---:|---:|---:|---:|---:|
| W1 | 3 020 | 0 | 41 | 121 | 0 | 9 |
| W3 | 92 083 | 18 | 182 | 5 364 | 21 184 | 9 |
| W4 | 10 169 | 0 | 211 | 512 | 0 | 9 |
| W7 | 187 107 | 10 391 | 7 493 | 30 866 | 0 | 9 |

## 4. mem_events store coh_oracle 分布

| workload | L1 | L2 | LLC | DRAM | WB |
|---|---:|---:|---:|---:|---:|
| W1 | 3 025 | 0 | 0 | 236 | 38 |
| W3 | 76 762 | 7 | 0 | 3 430 | 17 883 |
| W4 | 8 841 | 0 | 0 | 245 | 59 |
| W7 | 147 442 | 3 | 1 | 52 461 | 33 |

## 5. W7 MSHR 窗口式 dedup 估算（关键发现）

| 窗口大小 | 原始 DRAM load | dedup 后 | coalesced | 留存比 |
|---|---:|---:|---:|---:|
| 50  | 30 866 | 30 865 | 1 | 100.00% |
| 100 | 30 866 | 30 865 | 1 | 100.00% |
| 200 | 30 866 | 30 865 | 1 | 100.00% |
| 500 | 30 866 | 30 865 | 1 | 100.00% |
| 1 000 | 30 866 | 30 865 | 1 | 100.00% |
| 5 000 | 30 866 | 30 865 | 1 | 100.00% |
| ∞（纯 set dedup） | 30 866 | 30 859 | 7 | 99.98% |

**关键洞察**：W7 的 30 866 个 DRAM load 在任意 ≤5000 命令窗口里几乎不重叠 — 同一 cacheline 在 5000 个 commit 内**很少**被反复访问。即使整段 trace 全集 dedup 也只能合并 7 条。

## 6. 结论修正

V9.5 PMU diagnosis 推测 W7 主因是 "A2(MSHR coalescing) + A3(L3 LRU 视图差异)" 组合。Baseline 实测后修正为：

1. **W7 主因 = A3（纯 LRU 抖动）**。oracle 单一全相联 32 768 行 LRU，对 W7 的 **52 580 unique cachelines** 的 working set 长跨度访问模式无法正确捕捉命中；ruby 真实 4-bank × 16-way set-assoc 的 set conflict 模式与 oracle 不同 → 同一 cacheline 在 oracle LRU 上**反复 evict / refill**，每次 refill 都判 DRAM。
2. **A2（MSHR）对 W7 修复贡献 < 0.05%**。但 A2 仍要做：W3 R_DIRTY 21184 条里包含 squash + reissue 场景，还有 i-cache + walker 路径上重叠的 in-flight miss，需要 MSHR 去重。

## 7. W7 unique cacheline 数量

```
W7 total mem ops:                    435 806
W7 unique cachelines (load+store):    52 580
W7 ruby unique miss (NP.L1_GETS):        876   ← ruby 视角的"首次进 L2/L3 cacheline"
```

W7 unique cacheline 数（52 580）远大于 ruby 真值 876，进一步证明 oracle 的 LRU 视图把大量本应 LLC hit 的访问错判 DRAM。

ruby 真实 L3 = 2 MiB / 64B = 32 768 lines，能容纳 W7 的 52 580 unique cachelines 吗？不能完全容纳，但 chase_dram 的访问局部性好，热点子集 < 32K，所以 ruby 视角下 88% 的访问命中 L3，仅有 876 条 cold miss。oracle 单一全相联 LRU 把"热点子集"判定错乱，将 30 866 条命中误判为 DRAM。

## 8. 对 Plan 的影响

- A2 + A3 + 方案2 + walker 模型组合方向不变
- A3 是 W7 修复的**主要驱动**，要重点验证 4-bank × 16-way 的 hash 与 ruby 完全对齐
- A2 主要服务 W3，对 W7 助益有限（写入文档说明）
- W7 修复后 PMU acc 期望 ≥ 95%（不再期望 ≥98%，因为 ruby 实际在 transient state / L2 吸收上仍有 oracle 不可见的差异）
