# Retired service expectation：默认关闭的通用 PMU 近似

2026-11-17，用户要求实施通用近似并验证、保留回退。本轮在现有并行两阶段模型旁实现两个观察器：`bounded-v1`、`probabilistic-v1`。默认 `off`，原始资格、unknown、legacy `estimated_count` 和目标时序均保留。新预测在独立命名空间，不覆盖旧计数；是否改善精度由同输入对照决定。

本轮验证已完成：14 例、28 次 ON 对照保持旧计数和时序不变，另有 4 次 OFF 回放；fresh strict zstd 来源/合同检查 1/1 通过。原 12 例 PMU MAPE 略有改善，但加入可评分 Marian C4 后 L1 MAPE 从 27.289% 变为 27.739%，未得到可默认启用的通用修复。12 次宿主单样本初测中，四负载总端到端 wall 相对 OFF 分别增加 1.38%（bounded）和 2.25%（probabilistic）；不能据此宣布稳定吞吐验收通过。

完整逐例计数、误差、CPI 绝对误差/MAE、unknown 敏感性和宿主证据见 [验证报告](retired-service-expectation-validation-20261117.md)。默认继续 M5，两个新模式保留 opt-in。

## 模型与输入边界

仅使用退休功能成员的地址、PC、写/转发事实、已有 FastSim cache 服务分类、FastSim 自身的选定服务/最终接收时钟，以及已有分支预测器的结果。target cache 几何与 ROB 来自同源配置。PC 只标识局部流历史，不是 workload ID；没有逐 workload 参数、native cache outcome、OoO 时间戳或目标答案输入。

每核固定 4096 项四路 history、256 项 PC 历史；宏指令内最多 16 个关联组。每个 accepted member 单次前向处理，观察器持久状态与每成员工作量有界。启用时还复用 resident chunk 的 audit-PC 向量，其额外存储随 resident 功能 UOP 数变化；它不属于固定 history 项数的承诺，off 时不分配。没有新请求、虚构 cache fill、逐周期执行、额外因果运行器或整窗口迭代。它是 PMU 预测观察器，不是 cache 状态或 CPI 修复。这里 bounded 约束状态量与计算量，不保证 PMU 相对误差上界。

### Bounded

同线、同观察代次可复用已有 read service。功能 write 终止旧 read 关联；不会把 store permission request 冒充数据填充。child 的 selected/final admission 之间采用显式均匀不确定性近似，计算落在 parent 半开区间的概率。future owner、时间倒退、缺失来源保留显式 fallback；无法处理的来源使用完整 legacy 点预测，不删除该成员。forwarding 和软件预取的零 miss 资格按原有 SPR 语义保留。

代次是观察器根据已见服务/写事实建立的身份，不声称已完整重建物理 coherence generation。缺失服务、不可见请求、有限 history 替换仍是模型近似边界。

### Probabilistic

在 bounded 结果上增加双向残差：已有模型分支误预测的 ROB 距离暴露、同线复用、稳定小步长，以及已见同 target L2 set 的局部压力。它可把部分下层服务概率移向 L1 pending-fill，也可把部分 hit 概率移向下一层 miss。单方向最大移动质量为二分之一；这是冻结的解析不确定性先验，**不是从 zstd 或本轮评估计数拟合的系数，也没有完成独立原生微基准校准**。

该层只改变事件的条件期望，不给实际 cache 插行、不生成 wrong-path 或预取流量，不向队列/DRAM 注入请求。`warm_mixes`、`pollution_mixes` 是触发诊断，不能当作误差的因果贡献。

## 宏指令与完整人口

三个事件保持层级一致的边际概率，可表示 resident/forwarded、L1 pending 或 L2 service、LLC service、memory service 的联合类别。相同 line/generation 的片段共享关联；不同关联组不假设独立，使用 Frechet 下界 `max(p)` 和上界 `min(1,sum(p))`，点估计取中点。超过 16 组时保留有界的 max/sum 与 fallback，不删除成员。

概率以 Q16 累计到完整退休宏指令。支持跨 chunk、warmup-spanning、precise-fault abandon 和不完整尾部；只有完成的 read 宏指令计入 load 人口。新 namespace 的逐核/逐 scope eligible-load 数必须与旧 ledger 相等，序列化不相等即报错。未知来源、时钟不可用、相关性不确定和 fallback 单独计数。上述区间是**给定估计边际概率的相关性界，不是 native 真值的置信区间**。

## 使用与回退

配置：

```ini
# 通用库默认；回退到旧 PMU 观察器
core.retired_cache_service_model = off

# 两个独立候选，三选一
core.retired_cache_service_model = bounded-v1
# core.retired_cache_service_model = probabilistic-v1
```

候选 profiles 为 `configs/spr-retired-service-bounded-candidate.cfg` 和 `configs/spr-retired-service-probabilistic-candidate.cfg`，继承冻结 M5。每个来源仍单独绑定 geometry、timings 和 thread plan。旧的 `core.retired_cache_final_admission_clock` 实验不能同时开启。event-only、context/fixed-work 和 `advance()` 暂不支持，明确拒绝，避免名义激活却回退旧算法。

关闭后不分配 observer/history，不启用额外最终接收时钟或 PC 采集。安装的 M5 ELF 和 alias 不由这两个 profile 改动。精确旧 ELF 回退副本在 `tmp/retired-service-expectation-20261117/rollback/fastsim-m5`；其源配置使用 roster 中的 base config，而非把候选输出冒充 M5。

新 ELF 使用 `tmp/retired-service-expectation-20261117/frozen/fastsim`。激活应核对 `configuration.retired_cache_service_model` 和 `scope_metrics.retired_service_expectation.available/model`；旧 M5 ELF 不含本轮新观察器。

实际复跑应使用已绑定该输入的 case config。例如以下命令写入独立结果；将配置路径中的 `probabilistic-v1` 改成 `off` 即是同 ELF 行为回退：

```bash
source scripts/project_tmp.sh
LD_LIBRARY_PATH=/opt/gcc-11/lib64 python3 scripts/run_high_frequency.py -- \
  taskset -c 52-55 tmp/retired-service-expectation-20261117/frozen/fastsim simulate \
  --config tmp/retired-service-expectation-20261117/configs/probabilistic-v1/c04-777.zstd_r.cfg \
  --manifest tmp/spr-no-prefetch-no-early-prepare-c4-20261110/sources/c04-777.zstd_r/tao_trace/manifest.txt \
  --cores 4 --measurement-scope user-plus-kernel --native-kernel-trace true \
  --allow-mmio-escape true --allow-cross-page-without-virtual-token true \
  --preload-validated-input true --perf-cache-input-identity true \
  --output tmp/retired-service-expectation-20261117/manual-check.json
```

这是精度复跑命令，含额外身份扫描，不能把它的全进程 wall 当作下述排除额外审计的吞吐 pilot。

## 输出和维护验证入口

`scope_metrics.retired_service_expectation` 提供模式、语义版本、Q16 scale、输入身份、task IDs、per-core/scopes、expected/lower/upper、unknown/fallback，以及无 timing feedback/无 synthetic requests 声明。旧 `scope_metrics.retired_cache` 保持原样。期望数允许小数，不是观测事件整数。

维护入口显式选择新预测：

```bash
source scripts/project_tmp.sh
/root/.pyenv/versions/3.11.14/bin/python3 \
  tools/run_native_kernel_fastsim_validation.py \
  --validation-mode strict \
  --source-root tmp/retired-service-expectation-20261117/strict-control-sources \
  --stats-dir tmp/retired-service-expectation-20261117/stats/probabilistic-v1 \
  --output-dir tmp/retired-service-expectation-20261117/recheck \
  --expected-cases 1 --jobs 1 --retired-service-expectations
```

它复用维护的 native source/auditor、内容身份、task/core/scope 和人口检查，再验证期望数的完整性、非负、有界、嵌套及逐核守恒。新模型和 native 参考必须采用当前 `spr-retired-load-miss-v2` 语义；不能用旧事件投影替代。默认不允许 native unknown；`--retired-service-native-unknown-limit 2` 是用户已允许的极少 unknown 的显式暂定报告选项，实际数量不清零。超过上限不评分。

结果独立写入 `retired_service_expectation_validation` 和 `retired-service-expectations/`，不混入 legacy 整数计数。strict passed 仍表示来源与合同门禁，不是精度阈值、硬件等价或吞吐认证。

## 验证与证据

规则在读取本轮预测结果前冻结，见 `tmp/retired-service-expectation-20261117/freeze.json`。合成测试只验证模型机制/数学性质，不冒充原生经验校准。评估覆盖原四负载 C4/C8/C16 共 12 例，另加未用于定参数的 Marian C4/C8；已知四负载参与过前期诊断，因此不能把整组称作严格未见 workload 泛化认证。

所有完整输入与 warmup 保留。14 例 target identity 单独核验；PMU 使用冻结的独立审计参考并核对本轮输入身份，完整矩阵报告暂定误差；另外做单例 fresh strict 验证。Marian C8 每事件 3 个 native unknown，不扩大 1–2 个的许可，运行及原始计数保留，PMU 误差 unavailable。

精度回放开输入身份扫描，其全进程时间不作为主吞吐。宿主对照另关额外身份审计并绑定已审计输入，在 CPU52–63 上记录实际 monotonic 进程跨度；required preload/初始化/warmup/模拟/drain 保留在端到端分母，measurement-only rate 单列。少次配对只作为开销诊断，不升级稳定吞吐接受门禁。

测试、每例命令/配置/ELF 身份、source/target 检查、完整状态和所有误差均保存于该实验目录。数值结果及各负载回归已写入上述验证报告；默认继续保持 M5。
