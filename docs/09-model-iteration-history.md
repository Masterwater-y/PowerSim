# 模型迭代记录

本文档记录 fetch/execution latency 模型的主要迭代、统一验证口径、关键实验结果和阶段性结论。文档中的实验结果均需注明验证规模，避免将不同核数、不同窗口或不同数据版本下的结果混用。

## 验证口径

- 数据范围：W11-W15 训练类负载与 H01-H03 holdout 负载。
- W11 数据：采用 ROI 对齐后的验证数据，非 ROI 数据仅作为历史问题排查记录，不纳入正式对比。
- 评估窗口：默认采用每核 warmup 20K rows、measured 80K rows 的窗口设置；若某一组结果使用不同口径，需在对应实验段落单独标注。
- 主指标：端到端 CPI 相对误差与平均绝对 CPI 误差。
- 推理后端：timing-functional。

## 版本归档

正式对比只使用归档后的模型版本。探索性训练、临时验证产物和 smoke 结果不作为正式实验结论。

| 版本 | 说明 | 状态 |
|---|---|---|
| v10_2_base | 原始 multi-task latency 基线模型 | 历史基线 |
| v10_3_fetchdecomp | 引入 fetch decomposition 监督信号 | 正式对比版本 |
| v10_3_fetchdecomp_soft15 | 在 v10_3_fetchdecomp 基础上采用温度为 1.5 的 soft fetch gate 推理校准 | 当前主要基线 |
| v10_4_softgate_mlp | 提升 fetch head 表达能力并弱化分解约束 | 对照版本 |
| v10_5_fetch_group | 探索性取指分组方案 | 不纳入正式 best 对比 |

## 迭代结果

### v10_2_base

方法设计：

- 使用原始 multi-task latency 模型。
- fetch latency 以 row-level latency 形式直接回归。
- fetch group head 作为辅助监督信号。

验证口径：4 核，每核 100K rows，W11-W15 训练类负载。

| workload | CPI err |
|---|---:|
| W11 | -76.42% |
| W12 | -11.70% |
| W13 | -43.35% |
| W14 | -48.40% |
| W15 | +3.14% |

阶段性结论：

- execution latency 训练相对稳定，但端到端 CPI 误差主要受 fetch gap 累积误差影响。
- W11、W13 和 W14 存在显著 CPI 低估，说明直接回归 fetch latency 对长尾取指延迟刻画不足。

### v10_3_fetchdecomp_hard

方法设计：

- 增加 fetch decomposition 辅助监督，将 fetch latency 分解为基础取指延迟、分支恢复后取指延迟和残余长尾取指延迟。
- 推理阶段仍采用二值取指门控。

验证口径：4 核，每核 100K rows；W11-W15 为训练类负载，H01-H03 为 holdout 负载。

| workload | CPI err |
|---|---:|
| W11 | -81.23% |
| W12 | -0.57% |
| W13 | -54.98% |
| W14 | -34.20% |
| W15 | +12.85% |
| H01 | -10.16% |
| H02 | -23.67% |
| H03 | -31.62% |

汇总指标：

| 负载集合 | mean abs CPI err |
|---|---:|
| W11-W15 | 36.77% |
| H01-H03 | 21.82% |

阶段性结论：

- W12 和 W14 相比 v10_2_base 有明显改善。
- 二值取指门控对漏判样本过于敏感，会将部分真实 fetch gap 排除在时钟推进之外。
- H01-H03 仍存在系统性 fetch 低估，说明仅增加分解监督不足以解决部署侧门控误差。

### v10_3_fetchdecomp_soft15

方法设计：

- 保持 v10_3_fetchdecomp 的训练数据、模型结构和监督信号不变。
- 推理阶段将二值取指门控替换为温度为 1.5 的 soft gate 校准，使低置信度但非零的 fetch gap 能够部分进入时钟推进。

验证口径：4 核，每核 100K rows；W11 使用 ROI 切片数据，W12-W15 为训练类负载，H01-H03 为 holdout 负载。本节表格为 4 核 gate ablation 结果，不能与 16 核全量验证结果直接混用。

| workload | CPI err |
|---|---:|
| W11 | +5.60% |
| W12 | +4.65% |
| W13 | -49.55% |
| W14 | +3.78% |
| W15 | +8.91% |
| H01 | +1.69% |
| H02 | +1.63% |
| H03 | -9.35% |

汇总指标：

| 负载集合 | mean abs CPI err |
|---|---:|
| W11-W15 | 14.50% |
| H01-H03 | 4.22% |

阶段性结论：

- soft gate 是有效的推理校准方式，尤其显著改善 H01、H02 和 H03 的 holdout 结果。
- W11、W12、W14 和 W15 在该口径下进入较小误差区间，说明 fetch decomposition 与 soft gate 的组合能缓解部分取指门控误差；其中 W11 已更正为 ROI 切片数据结果。
- W13 仍存在显著低估，主要问题集中在分支恢复后的 fetch recovery 建模不足。
- W11 结果受数据窗口和 ROI 对齐影响较大，应结合 ROI 对齐后的验证口径单独解释。

### W11 ROI 数据对齐

背景：W11 的早期验证数据存在 ROI 起点不一致问题，会放大前缀窗口差异并干扰模型结论。为保证不同 core 的验证窗口可比，后续正式实验采用 ROI 对齐后的 W11 数据。

| core | ROI first fetch tick |
|---:|---:|
| 0 | 1,560,438,000 |
| 1 | 1,560,479,292 |
| 2 | 1,560,482,955 |
| 3 | 1,560,476,295 |

阶段性结论：

- ROI 对齐后，各 core 的起始 fetch tick 差异约为 45K cycles，处于可接受范围。
- 非 ROI 数据不应继续作为模型优劣判断依据。
- 涉及 W11 的跨版本对比必须注明是否使用 ROI 对齐数据。

### v10_4_softgate_mlp

方法设计：

- 将 fetch 相关预测头从线性头调整为小型 MLP，以提升 fetch tail 和 gate 的表达能力。
- 弱化 fetch decomposition consistency 约束，避免辅助目标过强干扰共享表示。
- 推理阶段继续使用温度为 1.5 的 soft gate。

验证口径：统一全量验证集合，包括 W11-W15 与 H01-H03。

| workload | CPI err |
|---|---:|
| W11 | -26.64% |
| W12 | -0.50% |
| W13 | -55.20% |
| W14 | -3.07% |
| W15 | +1.75% |
| H01 | -13.58% |
| H02 | -23.46% |
| H03 | +16.93% |

汇总指标：

| 统计范围 | mean abs CPI err |
|---|---:|
| 全部负载 | 17.64% |
| 不含 W11 | 16.36% |

阶段性结论：

- W12、W14 和 W15 保持在较小误差区间。
- W13 仍然严重低估，说明单纯增加 fetch head 容量不能解决 fetch recovery 缺口。
- H01、H02 相比 v10_3_fetchdecomp_soft15 回退，H03 转为过估。
- 该版本不作为主基线，后续优化应回到 fetch 累计误差闭环，而不是单纯扩大预测头容量。

### v10_5_fetch_group

方法设计：

- 引入确定性的取指候选分组规则，将取指预测限制在更少的候选 row 上。
- 对非候选 row 的 fetch gap 进行重分配，使训练目标与推理阶段的候选分组保持一致。
- 该方向用于验证“详细仿真中的 fetch group 是否能由部署侧可见特征稳定恢复”这一假设。

阶段性结论：

- 该方案完成了端到端链路验证，但尚未形成可用于正式对比的完整训练结果。
- 候选分组会显著改变 fetch 目标分布，可能将分支恢复后的真实 gap 移动到后续候选 row 上。
- 该方向暂不作为默认推理主路径，后续仅作为 fetch 目标重构方案继续评估。

## 后续优化方向

基线选择：后续优化以 v10_3_fetchdecomp_soft15 为主要基线。选择该基线的原因是：

- soft gate 能降低二值门控漏判对 fetch gap 的放大影响。
- fetch decomposition 提供了 after-mispred 和 residual tail 的辅助监督。
- 推理口径仍接近原始 latency 累积逻辑，没有引入候选分组造成的目标重分配。
- H01、H02 和 H03 在 4 核 gate ablation 中显著优于后续 MLP 和候选分组分支。

主要问题：

- W13 的 CPI 低估仍然显著，主要缺口集中在 after-mispred fetch recovery。
- 现有 row-level loss 与端到端 fetch 累计误差之间仍存在目标不一致。
- 后续选择模型时不应只依赖训练损失，还应同时考察端到端 CPI、fetch sum error 和最坏核 fetch underestimation。

优化原则：

- 训练、验证和选点流程应围绕 effective fetch / effective exec 的累计误差闭环展开。
- learned gate 应作为 soft calibration 和辅助监督，不应作为不可绕过的硬清零条件。
- 候选分组和 gap 重分配方案不作为默认主路径，仅用于对 fetch target 设计进行对照验证。

成功标准：

- W13 fetch deficit 明显收敛。
- W14 保持在小误差区间。
- H01、H02 和 H03 不出现系统性回退。
- W12 不因 fetch 补偿而出现明显过估。
- 全量评估的平均绝对 CPI 误差低于 v10_3_fetchdecomp_soft15 的统一基线。
