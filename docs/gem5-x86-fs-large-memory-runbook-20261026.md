# gem5 X86 FS 使用大于 3 GiB 内存的操作手册

日期：2026-10-26。

**2026-10-27 更新：当前项目采用默认关闭、独立组件的大内存入口，见第 12 节。**
第 0–11 节保留当日两文件实验补丁及 Marian 试跑的历史来源，不作为当前生产安装
步骤。尤其不要把旧补丁覆盖到公共内存类；HBM 兼容依赖新入口的隔离。

面向对象：需要在 gem5 X86 full-system（stdlib `X86Board` + Ruby
`MESI_Three_Level`）中配置 **大于 3 GiB RAM** 的 FastSim 协作者。

本手册是 step-by-step 操作手册。所有"已验证"结论均有当日本机实测或源码行号
支撑；没有实测的容量/通道组合明确标注为"仅接线验证"或"未验证"。

---

## 0. 结论速览（TL;DR）

1. **库存 gem5 stdlib 的 X86 FS 有 3 GiB 硬上限**（I/O hole），超过即 fatal：
   `X86Board currently only supports memory sizes up to 3GiB because of the I/O hole`。
2. 突破方式是 backport 上游 PR #2861 的**双区间内存语义**（纯 Python 补丁，2 个
   文件）。补丁归档：
   [`../patches/gem5-stdlib-x86-multirange-memory.patch`](../patches/gem5-stdlib-x86-multirange-memory.patch)。
3. **独立副本树打了补丁**：`/data00/yinhaolang/gem5-largemem`，二进制
   `/data00/yinhaolang/gem5-largemem/build/X86_MESI_Three_Level/gem5.opt`。
   原树 `/data00/yinhaolang/gem5` 与生产采集树 `gem5-fs` **保持不动、仍是 3 GiB
   上限**，两边可并行跑实验。另有基于当前 producer 的独立验证副本，见第 11 节。
4. 副本树从 FastSim 定制树复制，包含 TaoTrace / buffer TSO 符号，**但符号存在
   不代表当前采集接口完整**。后续实跑发现该二进制缺少
   `TaoTrace.functional_sync_marker_pc`，不能直接运行现有 FS 采集脚本；KVM
   boot 已验证，Marian/O3 采集须使用与当前 runner 匹配的 producer 后再验证。
5. 已用 **KVM 实启动**验证的容量：**6 GiB、32 GiB**（8 通道，guest `MemTotal`
   分别为 6 109 868 kB / 32 963 128 kB）；3 GiB 走原路径，接线与上游逐行一致。
   4/5/7/8/32 GiB 另有纯接线检查；6 GiB/2 通道（生产 runner 默认几何）接线实测
   为 4 控制器 / 4 directory。
6. 容量是**机器身份参数**，不是自由旋钮：做 CPI/PMU 对照时必须与 native target
   一致，且 >3 GiB 相对历史 3 GiB 属于新拓扑（控制器/directory 数翻倍），按
   [accuracy-reporting-contract.md](accuracy-reporting-contract.md) 必须另开矩阵、
   重跑 identity 与 native 校验，不得与 3 GiB 数据混汇。
7. 两段 DRAM"几何不对称"（rowsPerBank 不同）对 gem5 仿真时序的影响为**严格零**
   （第 6 节给证明）；真实变化轴是拓扑、Linux 流量放置与机器身份。

---

## 1. 背景：3 GiB 限制与补丁原理

32 位 x86 物理地址空间中 `[3 GiB, 4 GiB)` 保留给 PCI MMIO / APIC / BIOS（I/O
hole）。库存 `X86Board._setup_memory_ranges()` 只构造**单个** DRAM 区间且要求
连续，因此 RAM 一旦超过 3 GiB 就会与 hole 重叠而 fatal。

补丁采用上游 gem5 develop 已合入的 PR #2861 语义（与上游**逐行等价**，低段
3 GiB 硬编码）：

- 容量 $M\le 3$ GiB：走原始代码路径，行为与未打补丁完全一致；
- 容量 $M>3$ GiB：拆成两个数据区间，各自独立挂一整组交织通道：

$$
\text{low}=[0,\,3\,\text{GiB}),\qquad
\text{high}=[4\,\text{GiB},\,4\,\text{GiB}+(M-3\,\text{GiB}))
$$

- `board.mem_ranges` 依次为：低段、高段（$>3$ GiB 时）、I/O 区间
  `[0xC0000000, 0xC0100000)`；
- e820 表为高段**逐条追加** type=1（usable）条目，64 位内核因此能枚举全部内存；
- `ChanneledMemory.set_memory_range()` 每区间建 `num_channels` 个 `MemCtrl`，
  区间内 64 B 交织，`intlvMatch` 每段从 0 重新编号；并校验各段大小之和等于总
  容量、每段是 `interleaving_size × num_channels` 的整数倍。

补丁只改两个 stdlib Python 文件：

- `src/python/gem5/components/boards/x86_board.py`（区间拆分 + e820）；
- `src/python/gem5/components/memory/memory.py`（多区间通道接线）。

物理布局（以 6 GiB 为例，实测 e820）：

```text
0x0000000000 - 0x000009fbff  usable        (常规低端内存)
0x000009fc00 - 0x00000fffff  reserved
0x0000100000 - 0x0bfffffff  usable  低段  (1 MiB .. 3 GiB)
0x000c000000 - 0x00c00fffff  I/O (board.mem_ranges 末项)
0x00ffff0000 - 0x00ffffffff  reserved     (I/O hole 顶端)
0x0100000000 - 0x01bfffffff  usable  高段  (4 GiB .. 7 GiB, 补丁新增)
```

32 GiB 时高段为 `[0x100000000, 0x840000000)`，即 29 GiB。

---

## 2. 资产与环境

| 用途 | 路径 |
|---|---|
| 原大内存副本树（已验证 KVM boot；现有采集接口不完整） | `/data00/yinhaolang/gem5-largemem` |
| 当前 producer 的独立大内存副本（Marian 八线程已进入 O3） | `tmp/spec-marian-c8-32g-20261026/gem5-fs-largemem/` |
| gem5 二进制 | `/data00/yinhaolang/gem5-largemem/build/X86_MESI_Three_Level/gem5.opt` |
| 原树（勿动，3 GiB 上限） | `/data00/yinhaolang/gem5` |
| 补丁归档 | [`patches/gem5-stdlib-x86-multirange-memory.patch`](../patches/gem5-stdlib-x86-multirange-memory.patch) |
| 接线检查脚本（工作副本，`tmp/` 不入库） | `tmp/largemem-routec/routec_check.py` |
| KVM 启动冒烟脚本（同上） | `tmp/largemem-routec/routec_boot.py` |
| guest 资源缓存 | `/root/.cache/gem5`（kernel `x86-linux-kernel-5.4.49`、disk `x86-ubuntu-18.04-img`） |

**运行时**必需环境变量（gem5.opt 链接 conda 环境的 libpython）：

```bash
export LD_LIBRARY_PATH=/root/miniconda3/envs/yhl/lib:$LD_LIBRARY_PATH
```

**重新编译时**还需（见第 8 节）：

```bash
export PATH=/root/miniconda3/envs/yhl/bin:$PATH
export TAOGEN_SHARED=/data00/yinhaolang/gem5-largemem/taogen/shared
```

> 注意：stdlib 的 `.py` 被编译进二进制（`*.py.cc`），修改 Python 后**必须重新
> scons**，否则跑的是旧字节码。

---

## 3. 快速使用

### 3.1 接线检查（秒级，不仿真，先确认容量被接受）

```bash
cd /data00/yinhaolang/gem5-largemem
LD_LIBRARY_PATH=/root/miniconda3/envs/yhl/lib \
./build/X86_MESI_Three_Level/gem5.opt \
  --outdir=/data00/yinhaolang/FastSim/tmp/largemem-routec/check-32g \
  /data00/yinhaolang/FastSim/tmp/largemem-routec/routec_check.py \
  --mem-size 32GiB --mem-channels 8
```

输出应包含每个数据区间的 `DATA_RANGE`、通道数（`MEM_PORTS`）、Ruby
`DIRECTORY_CONTROLLERS` 数与完整 `E820_TABLE`，并以 `ROUTEC WIRING OK` 结束。
任何新容量/新通道数上线前先跑这一步。

### 3.2 KVM 实启动冒烟（guest 真实枚举内存）

```bash
LD_LIBRARY_PATH=/root/miniconda3/envs/yhl/lib \
./build/X86_MESI_Three_Level/gem5.opt \
  --outdir=/data00/yinhaolang/FastSim/tmp/largemem-routec/boot-32g \
  /data00/yinhaolang/FastSim/tmp/largemem-routec/routec_boot.py \
  --mem-size 32GiB --mem-channels 8
```

脚本通过 m5 readfile 让 guest 收集 `free -m`、`/proc/meminfo`、dmesg e820、
`/proc/iomem`，然后 `m5 exit`。guest 串口在 outdir 的
`board.pc.com_1.device`（含 `\r`，用 `tr -d '\r'`）。需要 `/dev/kvm`。

判定通过的关键行（32 GiB 实测）：

```text
BIOS-e820: [mem 0x0000000100000000-0x000000083fffffff] usable
Memory: 32957888K/33554040K available
MemTotal:       32963128 kB          # ≈ 31.44 GiB
/proc/iomem: 100000000-83fffffff : System RAM
```

KVM 过程中的 `kvm-x86: MSR (...) unsupported by gem5. Skipping.`、
`DRAM device capacity ... does not match ...` 均为**既有无害告警**（3 GiB
生产配置同样打印），不影响结果。

### 3.3 在 FastSim FS 采集 runner 中使用

**接口前置检查（当日后续复核）**：本节的内存参数接法成立，但原
`gem5-largemem` 二进制在现有 runner 上实际报
`AttributeError: Invalid assignment for Class TaoTrace with parameter functional_sync_marker_pc`。
因此下述两处命令增量不能单独保证采集可运行。应从当前生产采集树另建独立副本，
应用本手册的两文件补丁并重编译；不要删去 runner 的同步/线程身份参数来绕过。
此次独立副本和逐文件差异收据位于
`tmp/spec-marian-c8-32g-20261026/gem5-fs-largemem/`、
`tmp/spec-marian-c8-32g-20261026/source-diff-verification.json`。
原副本的 conda Python 还缺 `pyelftools`，采集前须核对实际嵌入 Python 的依赖；
首次失败日志及重试命令均保留在该实验目录。

[`../scripts/run_gem5_sync_probe.py`](../scripts/run_gem5_sync_probe.py) 本身就
是 stdlib `X86Board + ChanneledMemory` 配置（见其 L523-L528），用补丁二进制运行
即自动获得多区间能力，**只需在原有命令上做两处增量**：

1. gem5 二进制换成 `/data00/yinhaolang/gem5-largemem/build/X86_MESI_Three_Level/gem5.opt`；
2. 加 `--mem-size 6GiB`（默认仍是 `3GiB`）。通道数用 `--mem-channels`
   （runner 默认 **2**，与冒烟脚本默认的 8 不同）。

三条硬约束：

- **不要传** `--ruby-functional-backing-store`。该开关构造的 Ruby functional
  backing store 只覆盖低段
  [`run_gem5_sync_probe.py:509-510`](../scripts/run_gem5_sync_probe.py#L509-L510)
  （`SimpleMemory(range=board.mem_ranges[0])`），高段 functional 访问会越界。
  flag 默认关闭，保持默认即可（第 7.2 节）。
- **boot checkpoint 必须按新容量重做**。runner 的 checkpoint 身份收据含
  `mem_size / mem_channels / ruby_functional_backing_store`
  ([L541-L546](../scripts/run_gem5_sync_probe.py#L541-L546))，跨容量 restore
  会被 receipt 校验拒绝。用 `--save-boot-checkpoint` 在新容量下重新生成。
- 运行命令同样要带 `LD_LIBRARY_PATH=/root/miniconda3/envs/yhl/lib`。

---

## 4. 容量与通道数怎么选

### 4.1 合法取值

- 只接受**整数字符串**（`4GiB`、`32GiB`、`6144MiB`）；`5.5GiB` 这类小数被
  `toMemorySize` 拒绝。
- 每段须为 $64\text{B}\times N$ 的整数倍，GiB/MiB 粒度自动满足。
- 仿真器侧无硬性容量上限（64 位 `Addr`；同区间交织通道在
  `src/mem/physical.cc` 合并为一次 mmap 且惰性分配，宿主开销按 guest 实际触页
  计，约为驻留集而非配置容量；宿主 2 TB RAM，实测 32 GiB 无压力）。但**只有
  6/32 GiB 做过 KVM boot**，其他容量先按 3.1/3.2 冒烟。

### 4.2 控制器 / directory 数量

$M>3$ GiB 时每段各挂 $N$ 个通道，因此总计 **$2N$ 个 mem controller 和 $2N$ 个
Ruby directory**，与 $M$ 大小无关：

| 配置 | 通道/段 | 控制器总数 | directory 总数 | 验证状态 |
|---|---|---|---|---|
| $\le$3 GiB，任意 $N$ | $N$ | $N$ | $N$ | 与上游一致（接线） |
| >3 GiB，$N=2$（生产 runner 默认） | 2 | **4** | **4** | 6 GiB 接线实测 |
| >3 GiB，$N=8$（冒烟脚本默认） | 8 | **16** | **16** | 6/32 GiB boot 实测 |

### 4.3 "几何对称"窗口（可选关注）

DDR4_2400_8x8：每通道行缓冲 8 KiB、16 bank/rank、2 rank/channel，行地址步进
$8\text{KiB}\times16\times2=256\text{KiB}$。每通道切片 $S$ 先向上取 2 次幂
$C=2^{\lceil\log_2 S\rceil}$，再得行数 $R=C/256\text{KiB}$。两段 $R$ 相等的
条件是高段切片取整后等于低段（低段 $3$ GiB 取整后恒等价于 4 GiB）：

$$
M-3\le 4\;\text{GiB}\ \wedge\ M-3>2\;\text{GiB}
\quad\Longleftrightarrow\quad M\in(5,7]\ \text{GiB}
$$

该窗口**与通道数无关**。其中 **6 GiB（3+3）是唯一容量等分点**；7 GiB 行数
相同但两段容量不等。按第 6 节，此窗口之外也没有时序惩罚，选容量不必迁就它，
除非需要两段同构以便聚合统计。

---

## 5. 已验证矩阵（2026-10-26，本机）

| 容量 | 通道 | 接线 | KVM 实启动 | 证据目录（`tmp/largemem-routec/`） |
|---|---|---|---|---|
| 3 GiB | 8 | 单区间 / 8 ch / 8 dir / 4 条 e820，与上游一致 | 未在副本重跑（原路径代码未变） | `out-patch3g/`、`out-baseline6g/`(负向) |
| 4 GiB | 8 | 通过 | 未测 | `out-t-4GiB/` |
| 5 GiB | 8 | 通过（高段 R=1024） | 未测 | `out-g-5GiB/` |
| **6 GiB** | 8 | 通过 | **通过**，MemTotal 6 109 868 kB | `out-patch6g/`、`out-boot6g/` |
| 7 GiB | 8 | 通过（R 同为 2048） | 未测 | `out-g-7GiB/` |
| 8 GiB | 8 | 通过（高段 R=4096） | 未测 | `out-g-8GiB/`、`out-g2-8GiB/` |
| 6 GiB | 2 | 通过，4 ch / 4 dir | 未测 | `out-6g-2ch/` |
| **32 GiB** | 8 | 通过 | **通过**，MemTotal 32 963 128 kB | `out-t-32GiB/`、`out-boot32g/` |

补丁前负向对照：6 GiB 在未打补丁二进制上稳定复现 I/O hole fatal
（`out-baseline6g/`）。

---

## 6. 几何不对称为什么对时序严格无影响

唯一随容量/分段变化的 DRAM 参数是 rowsPerBank $R$（8 通道例：低段恒 2048；
6 GiB 高段 2048；8 GiB 高段 4096；32 GiB 高段 16384）。

`decodePacket()` 先用 `range.getOffset()` 把请求地址换算成**段内从 0 起的偏移**
$x$（`/data00/yinhaolang/gem5-largemem/src/mem/mem_interface.hh:238`），
两种地址映射最终都是（`/data00/yinhaolang/gem5-largemem/src/mem/dram_interface.cc:856-899`）：

$$
q(x)=\left\lfloor\frac{x}{256\,\text{KiB}}\right\rfloor,\qquad
\text{row}=q(x)\bmod R
$$

而取整规则构造性地保证 $S\le C=R\cdot256\text{KiB}$，于是在整个合法段内
$q(x)<R$，**取模永不回绕**，row 恒等于 $q(x)$：

$$
\text{row}(x_1)=\text{row}(x_2)
\iff \lfloor x_1/256\text{KiB}\rfloor=\lfloor x_2/256\text{KiB}\rfloor
$$

即行命中/冲突判定与 $R$ 的取值无关。给定同一请求序列，activate/precharge/
row-hit 决策逐拍相同，访存延迟与 CPI 相同。其余面全部不变：

- tCK/tCL/tRCD/tRP/tRAS/tRRD/tFAW/tWTR、带宽、bank/rank/通道数、64 B 交织
  粒度、8 KiB 行大小：常量；
- 刷新：每 rank 固定 tREFI = 7.8 µs 间隔、tRFC = 350 ns 代价
  （`/data00/yinhaolang/gem5-largemem/src/mem/dram_interface.cc:1402-1424`），
  不随行数/容量缩放；
- $R$ 在代码中仅被取模译码与一行 `assert(row < R)` 消费，调度/刷新/统计路径
  不读它。

真实变化轴只有三个，且都不是"几何"：

1. **拓扑**：$N\to 2N$ 控制器/directory（第 4.2 节）；
2. **流量放置**：Linux 从低地址起分配，高段仅在低段耗尽后使用。6 GiB KVM
   boot 实测全程高段 8 个控制器零请求。工作集 <3 GiB 时高段流量为 0；
   graph500/marian 等 >3 GiB 工作集才有实质高段流量；
3. **机器身份**：容量本身即被建模硬件的一部分（第 7 节）。

边界例外：每通道切片 $S\le128$ KiB 时 $R=0$ 会触发除零（GiB 尺度不可能遇到）。

---

## 7. 上精度流水线前的合规清单

按 [AGENTS.md](../AGENTS.md) 与
[accuracy-reporting-contract.md](accuracy-reporting-contract.md)（第 0、8 节）：

1. **容量对齐 native target**：仿真容量由目标机决定，不图方便选；native truth
   只认 `tools/run_native_kernel_fastsim_validation.py`，身份单独用
   `tools/validate_fs_oracle_identity.py` 校验，native validator 的 `passed`
   不是身份/精度认证。
2. **新开矩阵**：>3 GiB（$2N$ 控制器/directory）相对历史 3 GiB（$N$）是新硬件
   配置，必须独立矩阵，禁止与 3 GiB 结果混汇；报告继续区分 MAPE/WAPE 并带
   CPI 绝对误差列与 CPI MAE。
3. **backing store 保持关闭**（第 3.3 节）；如确需 functional backing store，
   必须先给 [`run_gem5_sync_probe.py`](../scripts/run_gem5_sync_probe.py)
   补覆盖高段的 `SimpleMemory`（目前未实现）。
4. **checkpoint 不跨容量**（第 3.3 节）。
5. 各采集入口的二进制选择（现状，切换时逐个核对）：

   | 入口 | 二进制如何指定 | 注意 |
   |---|---|---|
   | `scripts/run_gem5_sync_probe.py` | 命令行直接调 gem5.opt | 换副本二进制 + `--mem-size` |
   | `tools/collect_uarch_common_end.py` | `--binary`（默认 `tmp/uarch-common-end-fix-20260917/gem5.opt`） | 显式覆盖为副本二进制；其 gem5 config 在 TCSim 树（本机无 `/data00/yinhaolang/TCSim`） |
   | `tools/collect_uarch_stats.py` | `--gem5`（默认硬编码 `/data00/yinhaolang/gem5/...`） | **SE 采集**，与 FS 大内存无关，勿混用 |
   | `scripts/smoke_spec2026_heldout_gem5.sh` | 顶部 `GEM5_ROOT=/data00/yinhaolang/gem5-fs` | 该树未打补丁；需要大内存时改指副本树并先验证 |

6. 本补丁**尚未登记**进 `gem5-collection` overlay/合并补丁体系与任何补丁应用
   脚本（参见
   [gem5-baseline-alignment-runbook-20260921.md](gem5-baseline-alignment-runbook-20260921.md)）；
   进入生产树前还需：高段 backing store 方案、checkpoint 迁移策略、安装脚本
   登记与一次 baseline 重建比对。

---

## 8. 从源码树重建（或在新树上应用补丁）

```bash
# 1) 在目标 gem5 树（基线需与补丁上下文一致：v25.1.0.1 + FastSim 定制）
cd /data00/yinhaolang/<gem5-tree>
git apply --check /data00/yinhaolang/FastSim/patches/gem5-stdlib-x86-multirange-memory.patch
git apply       /data00/yinhaolang/FastSim/patches/gem5-stdlib-x86-multirange-memory.patch

# 2) 编译（128 核示例；三项环境变量缺一不可）
PATH=/root/miniconda3/envs/yhl/bin:$PATH \
LD_LIBRARY_PATH=/root/miniconda3/envs/yhl/lib \
TAOGEN_SHARED=$PWD/taogen/shared \
scons build/X86_MESI_Three_Level/gem5.opt -j128
```

- 找不到 `Python.h` / `libpython3.11.so.1.0` → 检查 `PATH` 与
  `LD_LIBRARY_PATH`；
- `fatal error: lru_banked.hh: No such file` → `TAOGEN_SHARED` 未指到本树的
  `taogen/shared`；
- 改了 `.py` 行为不变 → 未重新 scons（stdlib 嵌入二进制）。

---

## 9. 故障排查

| 现象 | 原因 / 处置 |
|---|---|
| `X86Board currently only supports memory sizes up to 3GiB ... I/O hole` | 用了未打补丁的二进制（原树/`gem5-fs`）。换 `gem5-largemem` 的 gem5.opt |
| `error while loading shared libraries: libpython3.11.so.1.0` | 未设 `LD_LIBRARY_PATH=/root/miniconda3/envs/yhl/lib` |
| `toMemorySize` 解析失败 | 容量用整数字符串（`6GiB`），不支持 `5.5GiB` |
| 开 `--ruby-functional-backing-store` 后高段 functional 访问异常 | backing store 只盖低段；关闭该 flag（默认即关） |
| restore checkpoint 被收据校验拒绝 | 硬件身份含 mem_size/mem_channels；同容量重建 boot checkpoint |
| 裸配置脚本报 `_connect_things has not been called` | 调 `board._pre_instantiate(full_system=True)` 取得 root，不要手动 `Root(...)`（否则 multiple Root fatal） |
| 开发时 `Invalid assignment ... parameter mem_ctrl` | SimObject 子对象不能赋空列表；先构建完整 controller 列表再整体赋值 |
| KVM 启动大量 `MSR unsupported` / `device capacity does not match` 告警 | 既有噪声，3 GiB 生产同样出现，可忽略 |
| `while pgrep -f "scons build/X86" ...` 等待循环永不结束 | pgrep 自匹配（命令行内含该字符串）。用 `pgrep -f "[s]cons build/X86"` 或直接前台跑 scons |

---

## 10. 未覆盖事项（明确不做/待办）

- >7 GiB 两段几何同构：库存 MemConfig/Ruby.py/stdlib 均要求每段等通道数，无
  现成方案；而第 6 节已证明无时序收益，不建议投入。
- 高段 Ruby functional backing store、跨容量 checkpoint 迁移、补丁进入
  `gem5-collection` 固化流程：均未实现，需要时单独立项。
- 除 6/32 GiB（8 ch）与 6 GiB（2 ch，仅接线）外的组合未做 boot 冒烟；上线
  前按第 3 节自测。
- 不要把 FastSim 分支工作推到 `main` 或其他模拟器分支；不要修改原树
  `/data00/yinhaolang/gem5`（Codebase 仓库 `git@code.byted.org:yinhaolang/minesim.git`，
  FastSim 分支 `FastSim`，Git 工作流见
  [codebase-workflow.md](codebase-workflow.md)）。

## 11. Marian 八线程 32 GiB 实跑复核（当日后续）

历史 Marian C8 的容量失败来自宿主 ROI 预检：即使 mini-batch1、workspace32MiB，
RSS 仍为 3,897,200,640 bytes，无法放入原 3 GiB guest，因此当时未启动 guest
采集；不是一次已观察到的 guest OOM。

本次重新运行 8 核、每段 8 通道的 32 GiB KVM boot，通过并正常退出，guest
`MemTotal=32963128 kB`，高段 `[0x100000000,0x840000000)` 为 System RAM。
原大内存副本在当前 runner 上先后暴露 Python 依赖和 TaoTrace 接口缺失，详见
第 3.3 节。随后从当前 `gem5-fs` 完整复制独立树，仅应用两文件内存补丁；逐文件
比较确认只有这两个源码文件不同。按可用 affinity 的 80%（153 路）编译成功，
原生产树未改动。

新副本使用 Python 3.11 的运行环境：

```bash
export LD_LIBRARY_PATH=/root/.pyenv/versions/3.11.14/lib:/opt/gcc-11/lib64
```

独立 32 GiB staged checkpoint 已保存成功（423.76 秒），含低、高两个 RAM
payload 与身份收据；没有复用 3 GiB checkpoint，functional backing store 关闭。
恢复后，使用原首条 refspeed 的 mini-batch32、workspace64MiB、beam8、模型及输入，
八个计算 worker 身份握手完成，总 OS 线程 10 个，guest ROI
`VmRSS=4794040 kB`、`VmSwap=0 kB`，未观察到 OOM。已切入 O3+Ruby，
500K instruction-stop warmup 实测约 181.34 秒，统计窗口已打开。

100K user-UOP 短窗口由 core6 触发共同终点（实际 100001 user records、8 个参与核），
native response drain 在 3 次轮询后完成，8 个 FST 最终合计 1,205,864,768 bytes。
**短窗口已正常退出（exit 0、guard_reason=null），恢复及采集合计 730.67 秒。**
维护函数 `tools/common_end_capture.py::validate_common_end` 核验八核 FST、CPI
人口及 CPL 终点一致，八核测量 user UOP 合计 765,407。**这仍是启动/短窗口验证，
未认证正式完整来源，也未采集 10M 窗口或运行 CPI/PMU 精度验证。**
当前配置保留 1000µs quantum；源码
`src/sim/sim_events.cc::exitSimLoop` 将退出事件排到 `when + simQuantum`，共同
终点后仍可继续执行未记录的 O3 事件。两次调用栈采样已保存；暂停会影响墙钟，
本轮耗时不能作为吞吐测量。

证据均在 `tmp/spec-marian-c8-32g-20261026/`：

- `startup-verification.json`：本轮人工核验时点、实际参数和明确的待完成项；
- `source-diff-verification.json`、`current-producer-binary.json`、`build.log`；
- `checkpoint-current/fastsim-checkpoint.json`；
- `recovery-smoke-current-command.json`：实际短窗口命令；
- `recovery-smoke-current/c08-872.marian_s/common-end-audit.json`：维护函数核验收据；
- 同目录 `capture-receipt.json`：保护器生成的正常退出收据；以实际内容判断退出，
  不能用本节的启动成功代替来源、人口或精度门禁。

## 12. 默认关闭的大内存入口（2026-10-27）

这是当前项目的入口。用户已授权实施上一轮确认的隔离方案；没有把历史两文件
补丁覆盖到生产公共类。新增组件为 `X86LargeMemoryBoard` 和
`X86MultiRangeDDR4Memory`，源文件在 `gem5-collection/overlay/src/python/`，
同步安装到本地 `gem5-fs/src/python/`。注册补丁 0002 只改 `src/python/SConscript`，
原 `x86_board.py`、`memory.py`、`hbm.py` 的内容哈希保持不变。

### 12.1 选择规则

| 命令参数 | 所选组件 / 结果 |
|---|---|
| 默认 `--mem-size 3GiB` | 原 X86Board + ChanneledMemory |
| `--x86-large-memory --mem-size 3GiB` | 仍是原组件，实际大内存模式 false |
| 仅 `--mem-size 32GiB` | 仿真前拒绝，提示必须显式开启 |
| `--x86-large-memory --mem-size 32GiB` | 新 Board + 独立多区间 DDR4 |
| 大内存有效且带 `--ruby-functional-backing-store` | 拒绝；当前 backing store 不覆盖高段 |
| 向新 Board 传 HBM 或其他内存实现 | 拒绝；只支持独立 DDR4 组件 |

沿用原采集命令时，二进制使用当前项目的
`gem5-fs/build/X86_MESI_Three_Level/gem5.opt`，runner 使用维护中的
`scripts/run_gem5_sync_probe.py`，新增 `--x86-large-memory --mem-size 32GiB`。
若旧命令含 `--ruby-functional-backing-store`，先明确移除它；程序不会静默关闭。
当前二进制运行环境为：

```bash
export LD_LIBRARY_PATH=/root/.pyenv/versions/3.11.14/lib:/opt/gcc-11/lib64
```

分段规则仍是低段 3GiB、高段从 4GiB 起；8 通道/段产生 16 控制器和 directory。
profile 的 `dram.num_channels` 在大内存路径记录实际控制器数 16，另有
`channels_per_range=8`、`physical_ranges` 和 `memory_layout`，不能把它解释成
单个连续地址空间的 16 路交织。下游时序映射和正式 identity/native 门禁需要支持
这个分段拓扑；单独的启动验证不认证完整 FastSim 精度流水线。

### 12.2 checkpoint 与旧实验

请求和 checkpoint 硬件身份均记录 `x86_large_memory`（请求开关）以及
`x86_large_memory_active`（实际路径）。二进制/runner 哈希及容量/通道身份检查
继续生效。开关为 false 不会让旧收据接受新二进制，旧 checkpoint 应用采集时冻结
的二进制及 runner 恢复，或重新建点，不修改旧收据绕过校验。

旧生产二进制及公共类哈希收据保留在
`tmp/x86-large-memory-opt-in-20261027/legacy/`；原有冻结 runner、checkpoint、
FST 和历史大内存实验副本均保留。

### 12.3 可复查验证命令与范围

```bash
python3.11 -m unittest discover -s tests -p 'test_x86_large_memory_options.py' -v

LD_LIBRARY_PATH=/root/.pyenv/versions/3.11.14/lib:/opt/gcc-11/lib64 \
gem5-fs/build/X86_MESI_Three_Level/gem5.opt \
  --outdir=tmp/x86-large-memory-check \
  tools/gem5/validate_x86_large_memory.py \
  --x86-large-memory --mem-size 32GiB --mem-channels 8
```

第二条默认只实例化和检查接线；加 `--boot` 才真正启动 KVM guest。每次用新的
outdir 保存证据。该脚本还验证原 HBM 的 HBMCtrl/双 pseudo-channel、独立内存类
单区间保留原控制器、非法区间不重建控制器、双区间控制器/容量/E820 一致。

本轮原始证据及执行记录在 `tmp/x86-large-memory-opt-in-20261027/`。
第 11 节的 Marian 结果属于此前独立二进制，不能冒充本次新入口的应用/O3 实测。

### 12.4 本轮实测结果

当前生产 `gem5-fs` 已编译成功（Python 3.11、153 路），二进制 SHA256 为
`f7d06e8bd7a6a61c94b482fcdf931b83e8e4f6072ab9f2bfe4272e639f2d2506`。
最终测试均使用该二进制、`M5_OVERRIDE_PY_SOURCE=false`；调试时使用源码 override
的收据单独保留，不代替最终嵌入 Python 验证。

| 场景 | 实测结果 | 证据目录/文件（相对本轮实验目录） |
|---|---|---|
| 默认 3GiB、8 通道 | 原组件，8 controller / 8 directory，4 条 E820；KVM 正常退出，guest MemTotal 3,079,216 kB、swap 0 | `production-boot-3g-final/large-memory-validation.json` |
| 显式开启但仍为 3GiB | 实际模式 false，仍为原组件、8 controller / 8 directory | `production-optin-3g/large-memory-validation.json` |
| 显式 32GiB、8 通道/段 | 16 controller / 16 directory，5 条 E820；KVM 正常退出，guest MemTotal 32,963,128 kB、swap 0 | `production-boot-32g-final/large-memory-validation.json` |
| 显式 32GiB、2 通道/段 | 接线通过，4 controller / 4 directory；未另做该组合 boot | `production-32g-2ch/large-memory-validation.json` |
| HBM 原接口 | 原 HBMCtrl、两个 pseudo-channel 及 16 个 DRAM interface 契约通过；未认证 HBM FS 性能 | 上述最终验证脚本输出 |
| 不显式开启却请求 32GiB；大内存加 backing store | 实际 runner 均在仿真前明确拒绝 | `negative-cli.json` |
| 旧/新二进制默认 3GiB 比对 | 8,498 个内存及 Ruby/cache 配置 section 和 board.mem_ranges 一致，差异 0；不代表逐周期或 trace 逐字一致 | `legacy-config-comparison.json` |
| 实际 runner 保存 32GiB staged checkpoint | exit 0，无 guard 触发，两个 RAM payload 完整；请求和实际模式均 true，backing store false | `checkpoint-32g/fastsim-checkpoint.json`、`production-checkpoint-32g/capture-receipt.json` |
| checkpoint 身份拒绝 | 更改请求模式、实际模式或容量均被维护收据校验拒绝 | `checkpoint-identity-validation.json` |

9 项新开关/打包测试、14 项 SPEC 采集测试、1 项 staging、2 项 ready-checkpoint、
3 项 warmup policy、6 项空间保护测试，共 **35 项 Python 测试通过**。
`cmake --build build -- -j16` 成功，`LD_LIBRARY_PATH=/opt/gcc-11/lib64 ./build/fastsim_tests`
输出 `all FastSim tests passed`。本机直接运行测试需要上述 GCC 运行库；首次未设置
时是动态链接器拒绝加载，不是断言失败。

独立只读审核发现并修复非默认 `RoRaBaChCo` 的对齐边界：验证现在使用实际行缓冲
交织周期，512B 对齐但未达到 64KiB 周期的分段在替换控制器前被拒绝，真实 gem5
用例已 RED→GREEN。真实实例化还捕获并修复 E820 列表重新挂载造成的 orphan 条目。
早期验证脚本的 tick 参数类型及串口输出等待问题已修复，失败日志保留；最终两组
boot 均正常结束。审核无剩余阻塞项。

范围仍限于组件隔离、接线、guest boot、checkpoint 保存与身份校验。**本轮没有
执行新入口的 checkpoint 恢复应用/O3 采集、10M 正式矩阵或 CPI/PMU 精度验证。**
运行中的内存重配置不在组件契约中；必须在 Board 端口接线前设定 RAM 区间。
全部机器可读完成凭据见本轮 `completion.json`，其中保留独立审核结论和适用边界。

## 13. FastSim 回放双段 RAM（2026-10-27）

Gem5 大内存开关只决定采集端的物理拓扑；FastSim 回放需显式匹配该拓扑。
新增默认值为 `contiguous` 的配置项 `dram.memory_layout`，旧配置不需增加此项。
本次 C8 Marian 来源使用：

```ini
dram.memory_layout = x86-low3g-high4g-v1
dram.size = 32GiB
dram.channels = 16
```

这里 `dram.channels` 是实际独立控制器总数：低段 `[0,3GiB)` 和高段
`[4GiB,33GiB)` 各8个控制器，每段按64B cache line交织。不能将它解释成
平坦16通道，也不能把 `dram.size` 改成33GiB；I/O hole不计入RAM容量。
通道、bank和row按各段本地偏移解码，缓存和目录继续使用原始物理地址。
普通、mixed/projected DRAM、数据访问、I-side、PTE及边界状态恢复共用地址资格
检查；合成I-side/PTE的容量内偏移映射到RAM时跳过I/O hole。

该选项只支持上述双段布局、每段相同且为2的幂的通道数、`RoRaBaCoCh`及
cache-line位交织。布局名错误、容量不足、stripe未对齐、其他交织mask或地址映射
必须拒绝。单区间来源保留原有路径。有效配置JSON记录 `dram.memory_layout`。

维护的 `generate_fs_effective_target.py` 与 `audit_fs_profile_identity.py` 共用
分段范围和通道解析；后者使用实际MemCtrl的`dram`引用，支持`mem_ctrl00..15`，
并以实际core clock对原生DRAM tick时序逐项向上取整。参数匹配不代表时序模型或
硬件等价。Marian回放配置还显式使用采集的TSO buffer114；该buffer模式要求
`core.store_wait_relief=false`，并未开启实验性的bounded analytic模型。

边界、空洞、区域控制器独立性、两种DRAM实现及合成地址放置均有回归测试。
本轮执行记录在 `tmp/marian-c8-32g-10m-replay-20261027/`，包含冻结二进制、
配置、工具和辅助哈希程序；实验结果与资格边界另见本轮Marian回放报告。
