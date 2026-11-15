# Marian C4：现有 store 四行窗口单项消融

该实验只打开现有 `core.spr_store_window_feedback`；它近似普通 store
的四行发送重叠，不实现 committed store buffer 合并、offload、共享信用
或 prepare，不能视为 store buffer 修复。gem5-spr Marian 配置的四核均为
`tsoOrderedStoreLineWindow=4`；模型使用 4 行及 2 个 store 端口。未知或
不合条件的请求走旧路径。四核 `.sreq` source 审计已完成，功能输入由
OoO 采集器获得，独立 Atomic/QEMU 生产者等价尚未证明。

使用首核达到 10M user UOP 的共同结束、50 万指令预热、同一 manifest、
同一冻结 FastSim（二进制 SHA-256
`2763039c86a1c1eb0078b085749b556d5271f5356cfd1ae6da54a8f206f3c076`）。
OFF 是 `tmp/spr-alignment-v2-c4/c04-872.marian_s/off.json`；ON 是
`tmp/spr-store-window-marian-v2-20261105/on.json`。两次完整功能身份均为
`sha256:ff6b325b7ad7edda68253ed5e69a08f7f6dd8425c93c5d9fc3c2fc427f4d6523`，
实际输出配置唯一差异是 store window OFF/ON；workload-task 的 CPI 分母
均为 24,463,924 条宏指令。v2 OFF 的维护 validator 已通过来源检查并给出 gem5 workload-task
CPI 1.428304；由于用户允许起点非严格一致，以下为非严格同起点比较。
ON 的独立维护 validator 已完成：1 案 passed、0 failed/pending，
workload-task CPI 人口匹配。passed 表示来源与合同检查，不代表精度达标
或硬件事件等价；三个退休 cache miss 因原生 unknown 仍不可评分。

| 模型 | FastSim CPI | gem5 CPI | 绝对误差 cycles/macro | 相对误差 |
|---|---:|---:|---:|---:|
| OFF | 2.934776 | 1.428304 | 1.506472 | +105.47% |
| ON | 2.731018 | 1.428304 | 1.302714 | +91.21% |

ON 的模型计数：窗口接纳 4,656,846，回退 3,901；模型 SQ-full 事件
4,237,585 → 3,140,087，TSO 等待事件累计周期 5,178,367,205 →
4,089,223,394。这些事件周期可能重叠，不可相加或直接解释为 CPI 差。

同一 workload-task 范围内，模型退休 load L1/L2/L3 miss 估计 OFF 为
81,710 / 18,696 / 16,553，ON 为 81,708 / 18,690 / 16,563；模型三项
unknown 各 4。先前维护 validator 的原生已知数为 43,387 / 16,461 /
13,987；ON 维护重评分确认 native unknown 为 4 / 1,856 / 2,692，故三个严格 PMU
误差均**不可评分**；不把已知子集差值当完整误差。

两次完整 `functional_source_identity` 对象逐字段相同，各核模拟退休 UOP
人口也分别一致（7,882,977 / 9,269,937 / 8,388,333 / 10,009,918）。ON
窗口接纳数按核为 1,115,282 / 1,262,237 / 1,152,907 / 1,126,420，
回退数分别为 595 / 1,150 / 1,010 / 1,146；这些是模型窗口记录，不能
当作 gem5 原生 offload 或合并人口。

ON 内部记录预热 17.52 秒、测量 66.83 秒、输入身份核验 61.36 秒；
外部进程墙钟 396.80 秒。实验与其他任务并发，且 OFF 没有同条件外部
墙钟对照，这些数字不是吞吐对比或性能验收。`comparison-provisional.json`
记录配对输出、身份和参考路径；ON 最终验证证据为
`tmp/spr-store-window-marian-v2-20261105/on-validation/summary.json` 与
`retired-cache-summary.md`。
