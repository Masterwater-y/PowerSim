# SPR gem5 独立导入与 C4 验证（2026-11-03）

独立导入、源码重编译、C4 Zstd 采集、业务正确性、功能来源审计和两组 FastSim
回放均完成。**严格验收未通过**：目标身份合同缺失，模型执行尾部覆盖不完整，严格
cache PMU 证据不全。下列 CPI 只作为物理核窗口诊断，不推广为完整 C4 精度结论。

## 分支与隔离

用户要求先保存已有工作，再迁入兼容支持。已有关键代码与文档共 89 个文件已提交为
`c14c78054d10e04e4c941cf0945e0fd8ee3d15da`，推送至 Codebase `FastSim`，并核对远端
提交号一致。提交前默认构建、完整 `fastsim_tests`、动态翻译/DTLB/hierarchy、DRAM
refresh/controller 独立测试及相关 Python 检查通过。构建、trace 和批量实验数据未提交。

本轮工作分支为 `spr-c4-validation-20261103`，从上述提交建立。现有 `gem5-fs/`
未被修改。新增独立 Git checkout：

| 目录 | 来源与基线 | 更新分支 |
|---|---|---|
| `gem5-spr/` | `https://github.com/shown-net/gem5`，`02119d168de477a2782f33d14ad229484ad17de4` | `calibration-deploy` |
| `spr-framework/` | 用户提供的 `simulation-framework` 本地目录，保留 Codebase origin | `calibration-dev` |
| `spr-fastsim/` | 框架原配 FastSim，`99ab5cd1d45798e783b4ddf3a6e433d6c2f0c692` | `calibration-dev` |

三份 checkout 均用 `git clone --no-hardlinks` 创建，Git 元数据、源码与构建目录独立，
未复用原 gem5 的工作树或 `.git`。同时保留 `source-local` remote 指向各原目录。
gem5 远端分支已实查，与导入基线一致；配套 FastSim 远端可读。框架 Codebase origin
对当前凭据返回 403；本地副本可用，也可从 `source-local` 获取已提交更新，但不宣称
框架 origin 更新权限已具备。凭据选择仅设置在各新 checkout 的本地 Git 配置。

原 gem5 和框架均有未提交改动，已复制保留，并另外保存源差异；不能仅用上述基线
SHA 表示其完整源码身份。更新前先保存这些本地改动，检查上游差异，再合并或变基，
不要使用强制 submodule update 或 reset --hard。例：

```bash
git -C gem5-spr status --short
git -C gem5-spr fetch origin calibration-deploy
git -C gem5-spr diff HEAD
# 保存/提交本地改动并审查远端差异后，再 pull/rebase。
```

## 构建与兼容支持

复制的原 `gem5.fast` 哈希为
`6688fcbf4a0cd91fce80006cb7299f052c062f1c1c8e71511811bebc1952afc7`。
源目录的 `cpu.hh`、`tma_stats.cc` 修改时间晚于该二进制，因此它只用于首轮试采；
正式记录本轮独立重编译的 `gem5.opt` 身份，不将原二进制等同于全部导入源码。

独立构建入口：`scripts/build_gem5_spr.sh`。使用 affinity 的 80%，本次 192 核取
153 个构建任务；Python 3.11、GCC 11、protobuf 和 `TAOGEN_SHARED` 均为进程局部
环境，`TAOGEN_SHARED` 指向新 checkout 自带的 `taogen/shared`。未修改共享 shell
或原作者环境。构建中发现并处理了 libpython、protobuf include 与共享头搜索路径。

当前项目原已支持 FST v7；新增兼容点是 SPR OpClass feature bit 和独立计分区间。
兼容提交引入现有上游支持，并在冲突处理中保留当前动态翻译 reader/DTLB/hierarchy
路径。包含可选多级 BTB 和共享 FU 支持；这些是近似模型，不能称为完整 SPR 时序。
隔离副本 `tmp/spr-import-20261103/fastsim-current/` 基于同一已推送基线，用于冻结
编译和采集入口的源码身份；最终应以实际运行记录的二进制/源码哈希为准。

框架参数映射原假定单 IQ，而实际新 gem5 有 execution=128、memory=72、store-data=48
三组 IQ，共享一个 FU pool。已修复读取与共享 pool 去重，并保留每组布局；映射为
FastSim 单个 IQ 的 248 总容量只是近似，不代表相同分区准入。三个回归检查通过。
刷新开启配置还会从每次实际 `config.ini` 重新读取 tREFI/tRFC/clock，避免更新 gem5
后沿用冻结参考中的旧周期；该反例检查通过，总计四个映射检查。最后的来源归因修正
与已回放 leaf config 逐字节一致，保留了原始 stats，不属于模型或吞吐重跑。

## 冻结负载与口径

选用框架 `zstd-A`：四 worker，各自压缩 128 KiB；每 worker 预热一次 64 KiB。
这是固定工作量 C4 试验，不是完整 SPEC ref 测试。CPU0 是系统核，CPU1--4 为业务核，
回放映射为 FastSim core0--3。计分是各自 `(BEGIN, END]` 的物理核 U+K 宏指令人口；
连续 prefix 与 suffix 仍执行，不以裁剪后冷启动代替暖态。

镜像、内核、initrd SHA256 与 ELF 九个唯一 marker 已通过维护入口检查。负载在本地
`spr-framework/build/fixed-work/spr-c4-20261103-v2/` 重新编译冻结。

实际配置包括：名义 2.6 GHz；fetch/decode/rename/issue/commit=8/8/6/12/8；ROB512；
LQ240；SQ112；L1D48 KiB/12 路，L1I32 KiB/8 路，私有 L2 2 MiB/16 路；LLC96 MiB、
8 banks/12 路；DDR5-4400，16 channels，总地址空间 2 GiB；DTLB96 项/6 路。
最终参数须从本次 `config.ini` 映射并保留 `mapping.json`，不沿用旧 gem5 的 geometry。

首轮 `output/spr-c4-zstd-20261103` 在默认 600 秒超时，未完成 drain，不计入准确性。
历史同一案例约 695 秒，后续显式设置 2400 秒上限；没有改业务工作量。

## 必须保留的证据缺口

- 普通 common-end 的 `validate_fs_oracle_identity.py` / native validator 不支持该
  fixed-work 布局；初次调用没有发现合格案例，不能把 0 个 discovered 当作通过。
  此入口缺少它要求的 `request.json` / `effective-target.json` 身份合同。
- 固定窗口的 native cache origin 归属尚未被框架认证。即使有 Ruby/SLICC 摘要，
  也不能把整段摘要当作各 BEGIN/END 窗口的退休 load cache PMU 真值；禁止退回
  path-class 代理。四份 trace 均无 `.fsem` 退休成员 companion（feature bit7 未声明），
  严格 cache PMU 准确性保持 unavailable。
- task-active 覆盖不可得，采用物理核窗口；CPU0 不回放。team elapsed 还须独立检查
  全部 execution context 是否覆盖最后一个模型 score，不能用 sum cycles 替代 wall。
- IQ 分区、SPR 整数执行结构、MITE/DSB 前端、硬件预取、完整错误路径、STLB 分域
  与存储行合并/共享 credit 回收等未被完整复现。对齐配置值不等于硬件等价。
- 本次 DDR5 的 `tREFI=3900000`、`tRFC=295000`、`tRP=14545 ticks`。原框架映射未
  开启当前项目已有的刷新模型；项目分支另以这些参数和实际 385 ticks/cycle 开启刷新。
  FST 起点的原生刷新相位仍缺失，`refresh_start_tick=0` 仅为模型相对原点近似，不能
  当作已验证硬件相位；未复用旧 DDR4 参数。
- cache TBE -> MSHR、单级/多级 BTB、共享 FU 和 store buffer 选择须记录具体模式，
  不按 CPI 误差挑选参数。组合延迟边与五阶段 NoC 近似沿用映射审计，明确标注。
- CPI 表始终给相对误差及 `abs(FastSim CPI - gem5 CPI)`，单位 cycles/宏指令，
  聚合给 CPI MAE；验证通过不代表精度阈值、宿主吞吐或 SPR 硬件等价通过。

## 完成的采集与结果

本轮 gem5 二进制 SHA256 为
`77172ad5ff80bb7047829e4009a9e83ae4bb9954f2a800fd228d0f4fa0b0fc2c`；
FastSim SHA256 为 `feda7fa3d042870524c24bc303d5a2ba375fffda5d32de6a6fde6d9da408bbf9`。
采集阶段退出码 0、墙钟 821.99 秒，不含 boot、独立来源审计及回放。
4 份 FST 共 3,384,226,336 bytes，52,878,506 条热记录。四个输出均在宿主机解压后
逐字节比较、输入/输出哈希校验通过。依赖、syscall、虚拟页、4 个严格动态翻译审计
及 privilege 审计通过；16,010,433 个内存记录的翻译 unknown 为 0。

每核 score 区间的宏指令与功能 UOP 人口均精确匹配；宏指令共 26,144,031，功能 UOP
共 33,540,933。功能 UOP 不等同于新 gem5 的 allocation-group `UOPS_RETIRED.SLOTS`，
也未宣称硬件 retired-slot 语义等价。

两个变体均使用本次实际几何、buffer112、三层 BTB 和共享 FU 近似；刷新配置在查看
CPI 前确定。聚合 CPI 使用各自 sum cycles / sum macro instructions。每个变体只有
一个 C4 案例，因此单案 CPI MAE 等于该行绝对误差，不把两变体当作两个独立工作负载。

| 变体 | gem5 CPI | FastSim CPI | 相对误差 | CPI 绝对误差（cycles/宏指令） | 单案 CPI MAE |
|---|---:|---:|---:|---:|---:|
| 原框架映射，刷新关闭 | 0.454815 | 0.477979 | +5.0931% | 0.023164 | 0.023164 |
| 项目分支，刷新开启、相位近似 | 0.454815 | 0.486574 | +6.9829% | 0.031759 | 0.031759 |

项目分支逐核结果（同一冻结来源）：

| FastSim core / source core | gem5 CPI | FastSim CPI | 相对误差 | CPI 绝对误差（cycles/宏指令） |
|---|---:|---:|---:|---:|
| 0 / 1 | 0.455038 | 0.491178 | +7.9423% | 0.036140 |
| 1 / 2 | 0.454191 | 0.484500 | +6.6731% | 0.030309 |
| 2 / 3 | 0.452692 | 0.483590 | +6.8255% | 0.030899 |
| 3 / 4 | 0.457330 | 0.487071 | +6.5031% | 0.029741 |

逐核等权 CPI MAE 为 0.031772 cycles/宏指令，和上表单案例聚合 MAE 的人口不同。
原框架映射仅 2/4 核 execution retirement endpoint 覆盖最后一个模型 score，刷新开启
为 1/4；两组 `all_execution_covers_last_score=false`。所有已采集记录已消费完且窗口
关闭，并不意味着尚未采集到的尾部已被证明充分。不能从这些结果发布合格 team elapsed
或完整 C4 精度；需要补齐连续上下文尾部与对应来源合同后重新验证。

目标身份维护工具显式检查本案，返回 0/1 valid，原因是缺 `effective-target.json`。
普通 native validator 按预期固定工作量不受支持的布局只发现 0/1、pending=1；没有
将 failed=0、空发现集或功能审计通过解释成正式验收成功。未评定严格 cache PMU 或
宿主吞吐阈值，`hardware_equivalence=unverified`。

原始结果与收据：

- 来源：`spr-framework/output/spr-c4-zstd-local-20261103/`。
- 项目刷新回放：`spr-framework/output/spr-c4-project-refresh-20261103/`。
- 详细摘要：`tmp/spr-import-20261103/validation-summary.json`；关键摘要另保存于
  `docs/spr-gem5-c4-validation-20261103/summary.json`，trace/sidecar 保留 SHA256。
- 具体数值映射与近似边：各 case 的 `fastsim/mapping.json` 和 `effective.cfg`。

复现入口（输出目录必须换新；已冻结的本次来源无需重采）：

```bash
scripts/build_gem5_spr.sh
cmake --build build -- -j16
LD_LIBRARY_PATH=/opt/gcc-11/lib64 ./build/fastsim_tests
/root/.pyenv/versions/3.11.14/bin/python3 spr-framework/tools/calibrate.py collect \
  --target gem5 --config spr-framework/configs/gem5/spr-fixed-work-project-refresh.json \
  --from spr-framework/build/fixed-work/spr-c4-20261103-v2 \
  --output spr-framework/output/<fresh-run-id> --case zstd-A \
  --adaptation aligned_buffer --collect-timeout-s 2400
```

首轮超时 trace 已按维护存储策略逐文件记录路径、大小和 SHA256 后退役，收据在
`tmp/spr-import-20261103/failed-trace-retirement.json`；失败 run/config/log/checkpoint
保留。没有删除本轮完成的来源或任何旧项目来源。

已补齐 privilege 审计器对 feature bit9/10 的识别，仍拒绝未知 bit11；两个回归检查
通过。这仅扩展 privilege 人口检查的输入兼容性，不代替翻译、依赖或退休成员审计。
框架另将 FastSim 未跟踪源码快照限制在明确的源码目录，并记录 scope，避免将旧
gem5 checkout、workloads、实验数据或其他嵌套仓库当作未跟踪 FastSim 源文件。
