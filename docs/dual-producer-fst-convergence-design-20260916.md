# 双 producer → FST 收敛与比对设计（2026-09-16，2026-09-17 修订）

状态：正式设计。本文汇总 gem5-FS 校准 trace 与 QEMU 最终用户 trace 如何收敛为
同一份 FST v7，并定义低成本、可归因的跨 producer 审查。它替代原先“全窗口 UOP/
微码分布逐桶相等”的跨 producer 门；不改变 FST 格式、模拟器默认或生产链。

---

## 1. 问题与成败关键

三方角色固定：

- **FastSim**：唯一消费者，输入只认 FST v7（`build/fastsim ... --manifest`）。
- **gem5 FS**：**校准 trace 的来源**（生成 FastSim 精度对齐用的 trace + oracle）。
- **QEMU 仿真**：**最终用户使用时的 trace 来源**（兼容 DynamoRIO/drmemtrace 事实）。

成败关键：**FastSim 在 gem5-FST 上校准，却在 QEMU-FST 上被用户使用。这只在两个
producer 产出的 FST 语义完全一致时才成立。** 若 lowering 规则不同（UOP 拆分、
op_class 归类、依赖距离），FastSim 在两侧看到的是不同的指令流，校准结果不迁移。

因此正确设计不是"写两个独立转换器"，而是**让两个 producer 收敛到同一份 FST +
同一个 lowering 权威**。

---

## 2. 三层架构：事实 / lowering / 输出

```text
        ┌──────── 事实层（producer 各自负责，权威不同）────────┐
gem5-FS ─► gem5 O3 已退休事实：宏序/字节/branch/地址/CPL/ASID（+ 直接 UOP）
QEMU    ─► QEMU 已退休事实：宏序/字节/branch/地址/CPL/ASID/事件 + 前态/访存
        └───────────────────────────┬───────────────────────┘
                                     ▼
        ┌──── lowering 层（共享权威 = gem5 x86 microcode，钉死版本）────┐
        │ gem5-FS：O3 在线直接产 UOP                                     │
        │ QEMU   ：gem5 v25.1.0.1 离线 ISA 功能投影，吃 QEMU 事实产同款 UOP│
        └───────────────────────────┬───────────────────────┘
                                     ▼
              ┌──── 唯一输出合同 = FST v7 + companions ────┐
              │ 64B 热记录 + .asmap/.vmap/.imap/.deps/syscall│
              └───────────────────────────┬───────────────┘
                                           ▼
                                     FastSim replay
```

三条纪律（均来自既有契约，非本文新增）：

1. **事实与 lowering 分离**：producer 只报"实际发生了什么"（宏、地址、branch
   outcome、CPL/ASID、事件），**不猜** UOP/OpClass/依赖。QEMU 侧明确"不生成
   gem5 UOP/OpClass/依赖距离或 FST 字段，也不接收 gem5 执行结果"
   （`functional-lowering-mainline-plan.md` §2.1）。
2. **lowering 单一权威**：两条路的 UOP/OpClass/src-dst/依赖/serialize-barrier/
   macro boundary/user-kernel OpClass 编码**都由 gem5 microcode 决定**，版本钉死
   （QEMU 路径固定 `gem5 v25.1.0.1@c8222cc6`；gem5-FS 侧 TaoTrace 用其在线 O3）。
   这保证语义分叉可在受控短窗口中逐字段归因；它不要求独立动态运行的全窗口
   分布相等。
3. **FST v7 是唯一输出合同**：两条路产出**同一种** 64B 记录 + 同一套 companion；
   FastSim 只认 FST，不认 producer。

---

## 3. 收敛点与实现现状

| 组件 | gem5-FS 路径 | QEMU 路径 | 收敛状态 |
|---|---|---|---|
| 热记录 UOP lowering | TaoTrace 直接产 FST v7 | QEMU→gem5 离线投影产同款 UOP | ✅ 两路已产 FST v7 |
| op_class / src-dst / 依赖 | gem5 O3 | gem5 v25.1.0.1（同引擎） | ✅ 权威统一 |
| syscall 元数据（128B 稀疏行） | ✅ | ✅（契约 §8 定义，DR 路径也验证过） | ✅ |
| `.asmap` / `.vmap`（ASID / 虚拟页） | ✅ | ✅（带 CPL/ASID） | ✅ |
| 内核指令流 | full-system | full-system | ✅ 均可采（见 §6） |
| **`.imap` 静态译码**（指令长度/后继/分支目标/v2 操作数） | 用中立适配器 `build_fst_instruction_map.py` | 同 | ⚠️ **待建**：producer 模块译码接入中立 schema（契约 §12 明列为 remaining step） |
| 物理地址严格性 | strict（完整 guest PA） | strict 或 non-strict（见 §5） | ⚠️ 需显式标记 |

要点：**这不是从零设计，而是收敛已完成大半**。两条路都已产 FST v7、lowering
权威已统一到 gem5。真正待建的是 `.imap` 静态译码的 producer 接入、§5 严格性标记
和 §7 的分层比较入口。

---

## 4. 依赖表示：已解除 4 槽限制（回答"是否支持任意长度依赖"）

**是。当前 FST 已不再限于 4 条 producer 依赖。**

- 64B 热记录仍含 `producer_dists[4]` + `producer_classes[4]`（`types.hpp:327-333`），
  布局不变（保持热路径紧凑）。
- 自 2026-09-09（`fst-complete-dependencies-20260909.md`）新增：主文件 **feature
  bit 5**（`fst::kCompleteDependencies`，`trace.cpp:1446`）+ 同名 **`.fst.deps`
  冷附件**。前 4 条 producer distance 存主记录，其余按距离递增写入附件，
  **支持 8/16/20+ producer，不在 8/16 处重新截断**。
- 上限：`n_src` 为 uint8（255），distance 为 uint32；**超范围采集明确失败，不再
  饱和裁剪**（旧行为是截断）。
- FastSim **确实消费**扩展边：`interval_core.cpp:1300-1316` 读取
  `current_dependency_extensions()`，并断言"扩展 RAW 依赖要求 complete 动态元数据"
  （feature bit 未置时带扩展直接报错）。
- 语义边界："完整"指现有 Int/Float/Vec/CC 跟踪类别的寄存器 RAW 边；内存顺序、
  系统操作、前缀前初始寄存器状态仍各自遵守其合同。
- 一个纠正（该 doc 明确）：`n_src` 是源寄存器计数，**不等于**不同 producer 个数
  （7 个源寄存器可能只依赖 1 条生产指令）。因此"`n_src>4` 即丢边"是错误判据，
  只应称"存在截断风险"。

**对本管线的含义**：两个 producer 都必须在采集侧按 producer UOP 身份去重、按距离
排序、前 4 存主记录余下写 `.fst.deps`，并置 feature bit 5。完整性由各 producer
的 ABI 审计和 FastSim 消费负责；§7 仅在可比的短窗口中归因依赖分叉。

---

## 5. 唯一实质分歧：物理地址与严格性

这是两条路唯一实质不同、必须在设计里显式处理的点：

- **gem5-FS**：完整 guest 物理地址 → FST 可做**严格 cache/coherence/CHA/DRAM** 校验。
- **QEMU full-system**：可提供 VA→PA 证据（`official.md` 记载的 load/store overlay
  逐字节 VA→PA） → 可为 strict。
- **portable user-mode drmemtrace**：拿不到可移植物理地址 → 契约 §10 与 checklist
  #10 强制**标 non-strict，排除严格物理 PMU 精度声明**。

**设计要求**：

1. FST 头/manifest 必须携带 **strict / non-strict 标记**（物理地址是否可信）。
2. FastSim 在 non-strict 输入上只做 timing 近似，**不报** cache/一致性精度。
3. **校准与使用的精度声明范围必须分开**：不能用 strict gem5-FST 校准出的 cache/CPI
   数字，去背书 non-strict QEMU/DR trace 使用时的结果。strict-QEMU 使用可继承全
   保真声明；portable-DR 使用只能声明 timing-only。

---

## 6. 内核态覆盖（与本管线的关系）

- 能否采内核指令流是 **full-system vs user-mode** 的区别，不是 QEMU vs DynamoRIO
  的区别。gem5-FS 与 QEMU-FST 均为 full-system，都能采内核已退休 UOP 流（实测
  QEMU C4 stockfish 内核记录约占 62%，带 CPL/ASID）。portable user-mode drmemtrace
  只有 syscall marker、无内核指令流。
- 内核指令与用户指令在 FST 中同构表示（同 UOP lowering），差异仅为特权标记
  （op_class 符号位编码）+ 单独 PMU 归属；执行时序上特权/系统指令被塌缩到
  `kSystem` pool（粗粒度），ASID 切换按 gem5 `flushNonGlobal()` 建模
  （`interval_core.cpp:1128`）。
- **泛化边界**：内核控制流（缺页次数、中断相位、handler 路径）冻结在采集时刻。对
  datapath 参数（cache/DRAM/TLB/宽度/分支器）变化泛化好（时序重新推导）；对会
  反馈改变内核控制流的变化（中断频率随速度变、页表初始状态、调度）泛化受损。
  这是"不做 OS 仿真"的必然代价，须显式声明，不靠工程掩盖。

---

## 7. 唯一跨 producer FST 功能特征报告

跨 producer 工作流只有一条：以 gem5-FST 与 QEMU-FST 为独立数据源，读取各自完整
measurement 区间，输出 agent 可读 JSON 功能特征及 `qemu - gem5` 数值差。它不要求
同 ELF、同 PC、同地址、同时间线、同 ASID/CPL 转换位置或同动态宏序列，也不构造
`min(measurement_user_records)` 伪共同窗口。

入口为 `python -m tools.fst_producer_report`。报告保留每个原始 core ID 和 aggregate，
字段为 CPL3 macro/UOP、UOP/macro、OpClass、load/store/atomic 与访问大小、branch
类别/outcome、serialize、syscall、CPL0 人口，以及完整 `.deps` 的 inline/extension
边、producer class 和固定对数 distance bucket。每个差值同时给出两侧绝对数、
`qemu_minus_gem5` 和以 gem5 为分母的相对差；gem5 为零时相对差为 `null`。

实现使用 NumPy 的固定 64B record `dtype` 分块 mmap 扫描，以布尔掩码、`bincount` 和
稀疏计数聚合特征；`.deps` 只顺序读取 extension row，并回查对应热记录 hash。这消除
旧比较器的 ASID transition、用户 ordinal、`bisect` 距离重映射、高基数 operand
tuple Counter、全量精确 dependency Counter、排序/digest 与 first-difference 链路。
distance 使用固定对数桶，避免为稀疏 `uint32` 最大距离分配数组。

报告输入仍要求 canonical FST v7、合法 boundary、完整 `.deps`、稠密 core 集合和
measurement CPL3 UOP 守恒；这是输入格式错误，不是跨 producer 的等价结论。报告不
产生 pass/fail、status、certificate、阈值、Markdown 语义结论或 FastSim replay
CPI/PMU。`acceptance.json` 只可附加该 JSON 或 `report_error`，reference 缺失/报告
错误不得影响 QEMU 生产 acceptance。

## 8. 待建项与验收

1. **`.imap` 静态译码 producer 接入**：继续按
   `build_fst_instruction_map.py` 的中立 schema 与既有 checklist 发布和审计；
   该报告不代替它。
2. **strict/non-strict 标记**：继续由 FST/manifest 与 FastSim 消费者定义；报告不
   比较 producer-local physical address。
3. **报告实现**：`tools/fst_producer_report.py` 为唯一实现；不保留旧 gate
   compatibility path。

验收为：对 fixture 的每 core 和 aggregate JSON 数值、对数 distance bucket、零分母
`null`、`.deps` extension 和损坏输入均有测试；既有构建、`fastsim_tests` 与 producer
functional gates 不回退。本文不运行 formal 矩阵，未提交或推送。

---

## 9. 出处

- `docs/fst-v7-drmemtrace-conversion-contract.md`（FST v7 布局、producer 路径 §12、
  adapter checklist §13、非严格标记 §10、syscall 映射 §8）；
- `docs/fst-complete-dependencies-20260909.md`（feature bit 5 + `.fst.deps`，解除
  4 槽限制）；
- QEMU-FST 侧 `docs/qemu_fst/official.md`、`native-kernel.md`、
  `functional-lowering-mainline-plan.md`（QEMU→gem5→FST v7 收敛、lowering 权威、
  VA→PA 证据）；
- `docs/qemu_fst/mainline.md` 与
  `docs/qemu_fst/stockfish-c4-dual-elf-cross-producer-inconsistency-20260917.md`
  （当前 production gate、C4 common-end 不一致和归因边界）；
- `tools/fst_producer_report.py`（当前 NumPy 单次特征扫描实现）。
