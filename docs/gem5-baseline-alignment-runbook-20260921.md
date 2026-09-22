# 对齐 FastSim baseline 采集用 gem5 的操作手册

日期：2026-09-21（2026-09-22 按 overlay + patch 模型重写正文）。

面向对象：需要在自己机器上把 gem5 对齐到 **FastSim 采集 baseline（FST + 原生
Ruby/SLICC PMU）所用的 `gem5-fs` 工作树**，并可选地叠加 buffer TSO
（`--tso-store=buffer`）variant 的协作方。

本手册是 **协作者操作手册（step-by-step）**；设计原理、目录规范与维护者导出流程
见 [`docs/gem5-collection-overlay-design-20260922.md`](gem5-collection-overlay-design-20260922.md)，
目录内速查见 [`../gem5-collection/README.md`](../gem5-collection/README.md)。三者一致，
均以 `gem5-collection/` 为唯一真值来源。

## 0. 已验证结论（2026-09-21 实机沙箱，2026-09-22 复核）

在 `/data00/yinhaolang/FastSim/tmp/evalute` 用纯净上游 gem5 `v25.1.0.1`
（`git ls-remote` 确认 tag = `c8222cc67a399…`）实机 dry-run 得到两条**决定性
结论**，据此本手册的对齐方式已从“下载上游 + 应用全套散补丁”改为
**overlay（整文件新增）+ 单份合并补丁（改上游文件的 diff）**：

1. **纯净上游 gem5 不能只靠散补丁重建。** baseline 采集栈会去**修改** TaoTrace
   基座文件（`src/cpu/o3/probe/tao_trace.hh`、`tao_trace.cc`、`TaoTrace.py`、
   `src/mem/taotrace_response.hh`），而这些文件在上游 v25.1.0.1 中**根本不存在**
   （上游 `probe/` 只有 ElasticTrace/SimpleTrace）。历史散补丁中**没有任何一个从
   `/dev/null` 创建 TaoTrace 核心 producer**，在生产 `gem5-fs` 中它们是**未提交的
   untracked 工作树文件**。因此“新增整文件”必须走 `overlay/`，无法用 patch 表达。
2. **散补丁跨补丁全局顺序脆弱、offset 漂移。** 例如 buffer TSO 的 `Sequencer.cc`
   hunk 在纯上游即 offset/FAILED（它依赖 TaoTrace/Ruby 修改带来的上下文）。故
   本手册改用一份由 `git diff c8222cc` 生成的**快照合并补丁**，消除排序与漂移。

**正确的对齐方式：上游 `c8222cc` → 先铺 `overlay/` 整文件 → 再打
`patches/0001-*.patch` → 构建 → 身份门禁验证。** 重建结果已在沙箱验证与生产
`gem5-fs` 采集源码**逐字一致**（`checked=50 mismatch=0`）。

### 其他事实与声明

- overlay 与合并补丁均从生产 `/data00/yinhaolang/gem5-fs` 机械导出（overlay 收
  untracked 非 `.orig/build/resources`；patch 收 tracked-modified 的
  `git diff c8222cc`），非人工搬运。导出命令见设计文档第 6 节。
- buffer TSO 目前是 **opt-in 实验模式**，只支持 `MESI_Three_Level` 协议，生产
  默认仍为 `serial`。它**已并入生产树**，因此已包含在 `0001` 合并补丁内；按本手册
  重建的树已带 buffer 能力，**不要**再单独 apply `variants/tso-buffer.patch`
  （会 double-count / 冲突）。其完整 litmus / Ruby tester / 10M common-end
  FST/oracle / 10-workload 精度矩阵详见设计文档第 9.5、10 节所引资料。
- 生产 `gem5-fs` 基线 = 上游 `v25.1.0.1`（`c8222cc`）+ overlay 新增文件 +
  合并补丁（含已应用的 buffer TSO 改动），均为未提交工作树状态——`gem5-collection/`
  正是把它固化为可版本化、可复现的形式。

## 1. 前置条件

| 项 | 值 |
|---|---|
| 上游 gem5 版本 | tag `v25.1.0.1`，commit `c8222cc`（`misc: Hotfix Release: v25.1.0.1 (#3082)`）|
| Ruby 一致性协议 | `MESI_Three_Level`（buffer TSO 仅支持此协议）|
| gem5 构建目标 | `build/X86_MESI_Three_Level/gem5.opt` |
| 构建 Python | Python **3.11**（系统 Python 2 / 3.7 不兼容此 gem5/pybind11 组合）|
| 构建工具 | SCons **4.9** |
| 环境变量 | `TAOGEN_SHARED=<本机 taogen shared 路径>`（TaoTrace 共享侧车）|
| 参考宿主 | Intel Xeon Platinum 8457C（我方 SPR 对齐校准用主机；非硬性要求）|

参考构建指纹（我方 2026-09-18 记录，供对照，非强制——不同机器/时间戳会导致
hash 不同；权威判据以第 6.4 节身份门禁为准）：

- 生产 `gem5-fs` base：`X86_MESI_Three_Level/gem5.opt`
  `sha256=c2c6be9dbdc0da94ec217dda0cdf042ebbf92a8a8176f7e2de5798357592837b`，
  size `2724046376`。

### 1.1 需要准备的三样东西

```text
<WORKROOT>/
├── gem5/                    # 上游 gem5 克隆（第 2 节），overlay+patch 落位于此
├── FastSim/                 # 本仓库，提供 gem5-collection/ 与 tools/
└── (可选) SPEC benchspec    # 仅复现 SPEC workload 时需要，见第 3.3 节
```

与旧版不同：**不再需要按 `gem5-fs/`、`TCSim/` 两个父目录区分 `-p1` 前缀**。所有
上游挂钩统一为一份 `git apply -p1` 的合并补丁，新增文件统一 `cp` 进 gem5 树根。

## 2. 下载正确版本的 gem5

```bash
cd <WORKROOT>

# 上游 gem5，锁定到 v25.1.0.1 / c8222cc
git clone https://github.com/gem5/gem5.git gem5
cd gem5
git checkout c8222cc67a399bfc01e8658dd14b30d5bfd634f9
git rev-parse HEAD          # 必须以 c8222cc 开头，否则补丁 hunk 会错位
cd ..
```

> TCSim 采集驱动/配置的对齐改动（checkpoint/restore、ROI 驱动、buffer TSO CLI
> plumbing）随对应工作推进单独交付，不在本 gem5 重建手册范围内。SPEC workload
> 相关改动（第 3.3 节）另需 SPEC benchspec 源码树；仅在复现对应 SPEC workload 时准备。

## 3. 修改集清单（来自 `gem5-collection/`）

所有内容位于本仓库 `FastSim/gem5-collection/`，机器可读锚点见其 `manifest.json`。

### 3.1 生产 baseline 采集栈（必需）

| 类别 | 位置 | 内容 | 应用方式 |
|---|---|---|---|
| 新增整文件 | `overlay/` | 9 个上游不存在的文件：`src/cpu/o3/probe/{tao_trace.cc,tao_trace.hh,TaoTrace.py,branch_events.cc,branch_events.hh,BranchEvents.py}`、`src/cpu/o3/kernel_event.hh`、`src/mem/taotrace_response.hh`、`build_opts/X86_MESI_Three_Level` | 整文件 `cp -a overlay/. gem5/` |
| 上游挂钩 | `patches/0001-fastsim-collection-upstream-hooks.patch` | 41 个上游文件的合并 diff（约 855 插入 / 35 删除）；覆盖 O3（fetch/decode/iew/commit/lsq/dyn_inst）、Ruby 协议与 system（Sequencer/RubyPort/MESI_Three_Level 等）、KVM、`sim/system`、x86 faults、serial terminal 等 | `git apply --index -p1` |

> `overlay/` 的 `tao_trace.cc` 等文件即历史手册中 `gem5-taotrace-*`、`p1/p2/p7`
> 补丁试图修改却在上游缺失的基座；合并补丁则等价于历史 `p0/p1/p2/p3/p5/p7` 及
> `first-core-common-end`、`complete-dependencies` 等对上游已有文件的全部改动之
> 净效果。各改动的语义分组见 `gem5-collection/CHANGELOG.md`。

### 3.2 buffer TSO variant（可选，opt-in）

| 位置 | 内容 | 应用方式 |
|---|---|---|
| `variants/tso-buffer.patch` | `--tso-store=buffer` 机制（默认 serial，仅 `MESI_Three_Level`）：新增 `TSOStore` 枚举、`tsoStore`/`tsoStoreBufferSize`（默认 114）、`StorePrepareSenderState` 与 `STORE_PREPARE` 权限预取、Sequencer `m_store_prepare_outstanding` 独立计数、SLICC `Store_Prepare`/`SP_IE` 状态与转换 | **通常不 apply**（见下） |

**重要：该改动已并入生产树，故已包含在 `0001` 合并补丁内。** 按第 4 节重建的树
已带 buffer 能力（默认 serial）。`variants/tso-buffer.patch` 仅在需要把 buffer 从
baseline 拆成独立可选层时用作参考，**切勿在合并补丁之上再次 apply**（会 double-
count / 冲突）。

### 3.3 workload 专用（按需，随 TCSim/SPEC 交付）

731.astcenc_r ROI 冻结、SPEC 静态链接配置、777.zstd_r 父进程 fork 前 checkpoint
等 workload 专用改动，随对应 workload 复现需要单独提供，不在本 gem5 重建范围内；
应用 zstd prefork 类改动后须**重建对应 SPEC 二进制并新建 checkpoint**。

## 4. 应用顺序（分阶段命令）

**顺序不可换：overlay 必须先于 patch。**

```bash
cd <WORKROOT>
GEM5=<WORKROOT>/gem5
COLL=<WORKROOT>/FastSim/gem5-collection

# 阶段 1：铺 overlay（整文件新增，含 TaoTrace 基座）
cp -a "$COLL"/overlay/. "$GEM5"/

# 阶段 2：打上游挂钩合并补丁
git -C "$GEM5" apply --index "$COLL"/patches/0001-fastsim-collection-upstream-hooks.patch

# （不要执行）buffer TSO 已在合并补丁内，勿再单独 apply variants/tso-buffer.patch
```

> 若 `git apply` 报 “No file to patch” 或 hunk 定位失败，最常见原因是 **overlay
> 未先铺**或 **baseline commit 不是 `c8222cc`**。先用第 6.1 节的 `--check` 预检。

## 5. 重建 gem5

```bash
cd <WORKROOT>/gem5
export TAOGEN_SHARED=<本机 taogen shared 路径>
python3.11 $(which scons) build/X86_MESI_Three_Level/gem5.opt -j"$(nproc)"
```

应用了 workload 专用补丁的（如 zstd prefork），须**重建对应 SPEC 二进制并新建
checkpoint**——旧 checkpoint 不含修复后的进程状态，不能复用。

## 6. 简单验证

### 6.1 应用前 `--check` 预检（推荐先做）

```bash
cp -a "$COLL"/overlay/. "$GEM5"/                                   # 先铺 overlay
git -C "$GEM5" apply --check "$COLL"/patches/0001-*.patch          # 无输出即可干净应用
```

若 `--check` 报错，多为 overlay 未铺或 baseline commit 不对；修正后再正式 apply。

### 6.2 serial 一键回退（bit-identical，最关键）

新二进制在 serial 下必须与“未加 buffer 补丁的等价构建”逐位一致。下列三种运行
的确定性仿真结果必须**完全相同**：

```text
（无 --tso-store 选项）
--tso-store=serial
--tso-store=serial --tso-store-buffer-size=<任意正整数>
```

判据：退出码、CPI、Ruby 请求/响应、原有统计、PMU、FST hash、checkpoint/drain
状态一致；`config.ini` 仅多出 inactive 新参数（应显示
`requested=…, active=false, effectiveSQEntries=32`），`stats.txt` 仅多出两个零值
prepare 统计。

### 6.3 buffer 机制冒烟

单核独立 store-stream 在 serial / buffer-32 / buffer-114 下 checksum 必须一致
（我方记录为 `8232763195715158016`），且 buffer 模式
`storePrepareIssued>0`、`storePrepareBlocked=0`，RubyPort drain 计数归零；双核
跨 cache-line Store→Store litmus `violations=0`、无非法 SLICC 转换/panic。

### 6.4 FastSim 身份门禁（权威）

对新产出的采集结果跑身份校验（详见
[`docs/accuracy-reporting-contract.md`](accuracy-reporting-contract.md) 第 0、8 节）：

```bash
python tools/validate_fs_oracle_identity.py <run-dir>   # 期望 valid=true
```

buffer variant 只有在 request/profile/effective-target 的 mode、size、active、
`needsTSO`、`SQEntries` 全部一致时才通过；serial 缺新字段时兼容为
`serial/114/inactive/SQ=32`。

### 6.5 （可选）重建等价核对

若需确认自建树与我方生产采集源码一致，可对照
[`gem5-collection/manifest.json`](../gem5-collection/manifest.json) 的
`artifact.gem5_opt_sha256`（跨机器可能不同，仅参考），或按设计文档第 7 节做逐字
`diff`（排除 `.orig`/`build/`/`resources/`）。权威判据仍是第 6.4 节身份门禁。

### 6.6 FastSim 侧回归

```bash
cd <WORKROOT>/FastSim
cmake --build build -- -j16
./build/fastsim_tests        # 期望 all FastSim tests passed
```

## 7. 风险与注意

- **overlay 与 patch 不可混**：新增整文件（上游 `/dev/null`）必须走 `overlay/`，
  一旦塞进 patch 协作者将无法重建；这是本方案成败点。改上游文件才走合并补丁。
- **baseline 必须钉 commit**：只写“某版本”不够；`v25.1.0.1` 对应唯一 `c8222cc`，
  合并补丁 hunk 定位依赖它。
- **buffer TSO 勿双重计入**：已在 `0001` 合并补丁内，勿再 apply `variants/`。
- **仅 `MESI_Three_Level`**：buffer TSO 在其他 Ruby 协议下应启动失败，不得静默
  回退到不同语义。
- **checkpoint 不可跨模式复用**：serial checkpoint 不能当作 buffer 用（身份 gate
  会按设计拒绝）；zstd prefork 修复后须重建 checkpoint。
- **variant 不复用 baseline trace**：每个 variant 须重新跑 gem5 生成自己的
  FST/oracle，并通过 `tools/validate_fs_oracle_identity.py` 与
  `tools/generate_fs_effective_target.py` 的身份链。
- **resources/ 与二进制不入库**：内核/磁盘镜像是运行期输入，二进制 2.7GB；均只
  按 SHA 锚定、另行分发。

## 8. 参考

- [`docs/gem5-collection-overlay-design-20260922.md`](gem5-collection-overlay-design-20260922.md)：
  overlay+patch 整合设计、目录规范、manifest schema、维护者导出流程（权威原理）。
- [`../gem5-collection/README.md`](../gem5-collection/README.md)：目录内速查与重建流程。
- [`../gem5-collection/manifest.json`](../gem5-collection/manifest.json)：机器可读锚点。
- [`../gem5-collection/CHANGELOG.md`](../gem5-collection/CHANGELOG.md)：改动历史与补丁分组来源。
- [`docs/gem5-spr-tso-store-buffer-design-20260917.md`](gem5-spr-tso-store-buffer-design-20260917.md)：
  buffer TSO 设计、实现结构、验证门槛与交付/构建指纹。
- [`docs/tso-buffer114-baseline40-collection-20260918.md`](tso-buffer114-baseline40-collection-20260918.md)：
  tso-buffer114 40-case 正式采集记录与二进制路径。
- [`docs/accuracy-reporting-contract.md`](accuracy-reporting-contract.md) 第 0、8 节：
  源矩阵、身份门禁、维护版校验器与发布清单。
- [`docs/fst-native-kernel-trace.md`](fst-native-kernel-trace.md)：native-kernel
  FST producer 的应用与验证命令。
