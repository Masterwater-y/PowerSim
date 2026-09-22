# FastSim 采集用 gem5 修改的整合设计（overlay + patch）

日期：2026-09-22。

状态：设计已定稿，`gem5-collection/` 目录与内容按本文实现，并在
`tmp/evalute` 沙箱实机验证“上游 + overlay + patch 可重建出与生产 `gem5-fs`
逐字一致的采集源码树”。

本文取代 [`docs/gem5-baseline-alignment-runbook-20260921.md`](gem5-baseline-alignment-runbook-20260921.md)
中“从上游 + 应用全套 patch 重建”的错误前提（该前提已被实机否证：baseline
补丁去修改的 TaoTrace 基座文件在上游根本不存在，无补丁可从零创建）。

## 1. 目标与设计原则

把“采集工具对 gem5 的修改”从**一台机器上不可复现的未提交工作树**，整合成
FastSim 仓库内**单一、可版本化、可 review、协作者可自建**的目录。不 fork、不
包含 gem5 源码，协作者按文档自建同版本 gem5。

核心原则（已由现状取证驱动）：**新增文件与改动文件必须物理分离**。

- **新增自包含文件走 `overlay/`**（整文件原样 copy）。这些文件在上游不存在，
  无法用 patch 从 `/dev/null` 表达——本仓库既有 `patches/` 无法 bootstrap 的
  根因正在于此。
- **改上游文件的挂钩走 `patches/`**（`git apply` 的 diff）。

违反此分离（例如把 `tao_trace.cc` 塞进 patch）会导致协作者无法重建。这是本
方案的成败点。

## 2. 生产 gem5-fs 现状（2026-09-22 取证）

- 基线：`origin=https://github.com/gem5/gem5.git`，`git describe=v25.1.0.1`，
  HEAD=`c8222cc`，**detached、0 本地提交**。所有改动均为未提交工作树状态。
- 41 个 **modified** 上游文件：`git diff c8222cc` 约 855 插入 / 35 删除，
  ~2162 diff 行。覆盖 O3（fetch/decode/iew/commit/lsq/dyn_inst）、Ruby 协议与
  system（Sequencer/RubyPort/MESI_Three_Level 等）、KVM、`sim/system`、
  x86 faults、serial terminal 等。→ **走 patches/**。
- 9 个需整文件携带的**新增**项（上游确认不存在）：
  | 文件 | 行数 | 说明 |
  |---|---:|---|
  | `src/cpu/o3/probe/tao_trace.cc` | 6410 | TaoTrace 核心 producer |
  | `src/cpu/o3/probe/tao_trace.hh` | 991 | |
  | `src/cpu/o3/probe/TaoTrace.py` | 103 | SimObject 定义 |
  | `src/mem/taotrace_response.hh` | 903 | 原生响应侧车 |
  | `src/cpu/o3/kernel_event.hh` | 40 | |
  | `src/cpu/o3/probe/branch_events.cc` | 89 | |
  | `src/cpu/o3/probe/branch_events.hh` | 46 | |
  | `src/cpu/o3/probe/BranchEvents.py` | 7 | |
  | `build_opts/X86_MESI_Three_Level` | 6 | 上游只有 ARM 变体，此 X86 变体上游无 |

  → **走 overlay/**。
- 不纳入采集目录：`*.orig` 备份、`resources/`（多 GB 内核/磁盘镜像，运行期
  输入而非源码）、`build/`（构建产物）。
- 当前在用二进制：`build/X86_MESI_Three_Level/gem5.opt`，
  `sha256=c2c6be9dbdc0da94ec217dda0cdf042ebbf92a8a8176f7e2de5798357592837b`，
  size `2724046376`，构建于 2026-09-18 12:54。
  注意：`tao_trace.cc` 源码 mtime（2026-09-18 19:14）**晚于**该二进制，工作树
  源码已比在用二进制新——这正是“未版本化导致漂移”的实证，迁移时应重建对账。

## 3. 目录结构

```
FastSim/gem5-collection/
├── README.md                    # 主文档:baseline pin、build 步骤、验证判据
├── manifest.json                # 机器可读锚:上游 commit / 工具链 / 期望指纹 / 协议
├── overlay/                     # 【整文件】新增模块,原样 copy 进 gem5 树
│   ├── build_opts/X86_MESI_Three_Level
│   ├── src/cpu/o3/kernel_event.hh
│   └── src/cpu/o3/probe/{tao_trace.cc,tao_trace.hh,TaoTrace.py,
│                          branch_events.cc,branch_events.hh,BranchEvents.py}
│   └── src/mem/taotrace_response.hh
├── patches/                     # 【diff】改上游文件,git apply -p1
│   └── 0001-fastsim-collection-upstream-hooks.patch   # 41 文件合并补丁
├── variants/
│   └── tso-buffer.patch         # opt-in variant:--tso-store=buffer(叠加在 baseline 之上)
└── CHANGELOG.md                 # 修改历史(叙述 + 补丁分组来源)
```

设计取舍：baseline 的上游挂钩用**一份快照合并补丁**（由
`git diff c8222cc` 生成），而非几十个手工排序的小补丁——避免跨补丁顺序脆弱与
offset 漂移（本会话已见 buffer TSO 的 Sequencer hunk 在纯上游都会 offset/
FAILED）。若日后需要分组可读性，可按子系统切成少数几个补丁，但仍以“上游快照
diff”为唯一真值。variant 的 `tso-buffer.patch` 单独保留，因为它是 opt-in、可
与 baseline 解耦。

## 4. manifest.json schema

```json
{
  "schema": "fastsim-gem5-collection/v1",
  "generated": "2026-09-22",
  "upstream": {
    "repo": "https://github.com/gem5/gem5.git",
    "tag": "v25.1.0.1",
    "commit": "c8222cc67a399bfc01e8658dd14b30d5bfd634f9"
  },
  "toolchain": {
    "python": "3.11",
    "scons": "4.9",
    "protocol": "MESI_Three_Level",
    "build_target": "build/X86_MESI_Three_Level/gem5.opt",
    "env": {"TAOGEN_SHARED": "<本机 taogen shared 路径>"}
  },
  "apply": {
    "overlay": "copy overlay/* into gem5 tree root",
    "patches": ["patches/0001-fastsim-collection-upstream-hooks.patch"],
    "variants": {"tso-buffer": "variants/tso-buffer.patch"}
  },
  "artifact": {
    "gem5_opt_sha256": "c2c6be9dbdc0da94ec217dda0cdf042ebbf92a8a8176f7e2de5798357592837b",
    "size": 2724046376,
    "note": "参考指纹;跨机器/时间戳可能不同,权威判据以身份门禁为准"
  },
  "verify": {
    "identity_gate": "python tools/validate_fs_oracle_identity.py <run-dir>  # 期望 valid=true"
  }
}
```

## 5. 协作者重建流程（写入 README）

```bash
# 0) 变量
GEM5=<你的工作路径>/gem5
COLL=<FastSim 仓库路径>/gem5-collection

# 1) 取上游并钉到 baseline commit
git clone https://github.com/gem5/gem5.git "$GEM5"
git -C "$GEM5" checkout c8222cc67a399bfc01e8658dd14b30d5bfd634f9
git -C "$GEM5" rev-parse HEAD    # 必须 = c8222cc...,否则 patch hunk 会错位

# 2) 先铺 overlay(整文件新增),再打 patch(改上游文件)
cp -a "$COLL"/overlay/. "$GEM5"/
for p in "$COLL"/patches/*.patch; do
  git -C "$GEM5" apply --index "$p"
done

# 3) (可选)叠加 buffer TSO variant
git -C "$GEM5" apply "$COLL"/variants/tso-buffer.patch

# 4) 构建(Python 3.11 + SCons 4.9)
cd "$GEM5"
export TAOGEN_SHARED=<本机 taogen shared 路径>
python3.11 "$(which scons)" build/X86_MESI_Three_Level/gem5.opt -j"$(nproc)"

# 5) 验证(客观判据)
sha256sum build/X86_MESI_Three_Level/gem5.opt      # 对照 manifest.artifact(跨机可能不同)
python <FastSim>/tools/validate_fs_oracle_identity.py <run-dir>   # 期望 valid=true(权威)
```

顺序不可换：overlay 必须先于 patch。若 patch 报 “No file to patch”，多半是
overlay 未铺或 baseline commit 不对。

## 6. 从生产树导出（维护者操作，一次性）

以下命令生成 overlay 与合并补丁；在 `/data00/yinhaolang/gem5-fs` 上执行，
产物写入 `FastSim/gem5-collection/`。**overlay 只收整文件新增，patch 只收
tracked-modified diff，`.orig`/`resources/`/`build/` 排除。**

```bash
PG=/data00/yinhaolang/gem5-fs
COLL=/data00/yinhaolang/FastSim/gem5-collection

# overlay:untracked 且非 .orig/构建产物/资源镜像
git -C "$PG" status --porcelain | awk '/^\?\?/{print $2}' \
  | grep -vE '\.orig$|^build/|^resources/' \
  | while read f; do install -D "$PG/$f" "$COLL/overlay/$f"; done

# patch:上游 c8222cc → 工作树,仅 tracked-modified,排除 .orig
git -C "$PG" diff c8222cc -- . ':(exclude)*.orig' \
  > "$COLL/patches/0001-fastsim-collection-upstream-hooks.patch"
```

导出后须校验：`variants/tso-buffer.patch` 与本仓库既有
`patches/gem5-o3-tso-store-buffer.patch` 语义一致（buffer TSO 已并入生产树，故
其改动已被上面的合并补丁包含；variant 目录用于把它拆成可选叠加时使用）。
若要 baseline 不含 buffer TSO，须先在导出前从生产树 reverse 掉 buffer 补丁再
生成合并补丁，二者取其一，勿双重计入。

## 7. 验证判据

- **重建等价**（本文实现后已在 `tmp/evalute` 沙箱验证）：上游 `c8222cc` +
  overlay + patch 后，工作树与生产 `gem5-fs` 采集相关源码**逐字一致**
  （`diff` 为空，排除 `.orig`/`build/`/`resources/`）。
- **二进制指纹**：`gem5.opt sha256` 对照 manifest（跨机器可能不同，仅参考）。
- **身份门禁**（权威）：`tools/validate_fs_oracle_identity.py` 返回 `valid=true`；
  见 [`docs/accuracy-reporting-contract.md`](accuracy-reporting-contract.md) 第 0、8 节。

## 8. 风险与约束

- **overlay/patch 不可混**：新增文件一旦进 patch，协作者无法重建。导出脚本用
  untracked vs tracked-modified 机械区分，勿手工搬运。
- **baseline 必须钉 commit**：只写“某版本”不够；`v25.1.0.1` 对应唯一
  `c8222cc`，patch offset 依赖它。
- **resources/ 不入库**：内核与磁盘镜像是运行期输入，另行分发并在 manifest 或
  采集契约中按 SHA 锚定。
- **二进制不入 git**：2.7GB；只锚 sha256，必要时走制品库。
- **variant 双重计入**：buffer TSO 已在生产树，导出时须明确它归 baseline 还是
  归 `variants/`，不可两处都算。

## 9. 参考

- [`patches/README.md`](../patches/README.md)
- [`docs/gem5-spr-tso-store-buffer-design-20260917.md`](gem5-spr-tso-store-buffer-design-20260917.md)
- [`docs/accuracy-reporting-contract.md`](accuracy-reporting-contract.md) 第 0、8 节
- [`docs/gem5-baseline-alignment-runbook-20260921.md`](gem5-baseline-alignment-runbook-20260921.md)（已被本文取代）
</content>
