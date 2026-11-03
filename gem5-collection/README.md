# FastSim 采集用 gem5 修改集（gem5-collection）

本目录把 FastSim 采集 baseline（FST + 原生 Ruby/SLICC PMU）所用 gem5 的全部
修改，整合为**单一、可版本化、协作者可自建**的形式。不 fork、不包含 gem5 源码；
协作者按本文自建同版本 gem5。

设计与取证依据见
[`../docs/gem5-collection-overlay-design-20260922.md`](../docs/gem5-collection-overlay-design-20260922.md)。

## 内容

| 路径 | 内容 | 应用方式 |
|---|---|---|
| `manifest.json` | 机器可读锚：上游 commit、工具链、期望指纹、apply 顺序 | — |
| `overlay/` | 9 个**新增**文件（上游不存在，如 TaoTrace producer） | 整文件 copy 进 gem5 树 |
| `patches/0001-fastsim-collection-upstream-hooks.patch` | 41 个上游文件的合并 diff（855+/35−） | `git apply --index -p1` |
| `patches/0002-fastsim-x86-large-memory-python.patch` | 注册两个独立的大内存 Python 组件；不改原 Board/Memory/HBM | 在 0001 后应用 |
| `patches/0003-mesi-clean-fetch-functional-read.patch` | 允许功能性读取目录干净取数的 IM 状态；保留脏写回/DMA语义 | 在 0002 后应用 |
| `patches/0004-functional-translation-capture.patch` | 实际叶页/PTE路径及翻译fragment功能事实 | 在已有采集hooks后应用 |
| `variants/tso-buffer.patch` | buffer TSO variant（reference-only，见下） | 通常**不要**再 apply |
| `CHANGELOG.md` | 修改历史与补丁分组来源 | — |

**核心规则：overlay 与 patch 不可混。** 新增文件（上游 `/dev/null`）无法用
patch 表达，必须走 overlay；改上游文件走 patch。混用会导致协作者无法重建——这
是本仓库旧 `patches/` 无法 bootstrap 的根因。

## 前置条件

- 上游 gem5 `v25.1.0.1` / commit `c8222cc`（唯一 baseline，patch offset 依赖它）
- Ruby 协议 `MESI_Three_Level`，构建目标 `build/X86_MESI_Three_Level/gem5.opt`
- Python **3.11** + SCons **4.9**（系统 Python 2/3.7、旧 SCons 3.0 不兼容）
- `TAOGEN_SHARED` 指向 `<GEM5>/taogen/shared`（overlay 已自带该目录下两个共享头，
  **无需任何外部 taogen 目录**）

## 协作者重建流程

```bash
GEM5=<你的工作路径>/gem5
COLL=<FastSim 仓库路径>/gem5-collection

# 1) 取上游并钉到 baseline commit
git clone https://github.com/gem5/gem5.git "$GEM5"
git -C "$GEM5" checkout c8222cc67a399bfc01e8658dd14b30d5bfd634f9
git -C "$GEM5" rev-parse HEAD          # 必须 = c8222cc...

# 2) 先铺 overlay(新增文件),再打 patch(改上游文件) —— 顺序不可换
cp -a "$COLL"/overlay/. "$GEM5"/
git -C "$GEM5" apply --index "$COLL"/patches/0001-fastsim-collection-upstream-hooks.patch
git -C "$GEM5" apply --index "$COLL"/patches/0002-fastsim-x86-large-memory-python.patch
git -C "$GEM5" apply --index "$COLL"/patches/0003-mesi-clean-fetch-functional-read.patch
git -C "$GEM5" apply --index "$COLL"/patches/0004-functional-translation-capture.patch

# 3) 构建（overlay 已把两个共享头铺到 <GEM5>/taogen/shared，指向它即可）
cd "$GEM5"
export TAOGEN_SHARED="$GEM5"/taogen/shared
gem5_jobs=$(python3.11 -c 'import os; print(max(1, len(os.sched_getaffinity(0))*80//100))')
python3.11 "$(which scons)" build/X86_MESI_Three_Level/gem5.opt -j"$gem5_jobs"

# 4) 验证
sha256sum build/X86_MESI_Three_Level/gem5.opt   # 对照 manifest.artifact(跨机可能不同)
python <FastSim>/tools/validate_fs_oracle_identity.py <run-dir>   # 期望 valid=true(权威)
```

若 patch 报 “No file to patch”，多为 overlay 未铺或 baseline commit 不对。

## 动态页大小与压缩翻译 sidecar（2026-10-29）

FS/FST producer 新增 `.fst.xlat.gz`：实际 walker 的PTE物理路径和TLB叶页信息，经翻译完成的Request传到提交的功能记录，按映射字典去重、token关联变化稀疏记录，再用gzip level1压缩。大小页切换、同VA/PA但PTE路径变化均保留新关联；跨页fragment单独保存。无可用事实或旧checkpoint缺路径时显式unknown，不重走最终页表补造证据。

热记录仍为64bytes，旧vmap保持v1。companion用feature bit9声明；当前FastSim已接入v1/v2 reader、混页DTLB和物理PTE链，缺少声明要求的companion会报错。v2新增特殊地址kind与实际请求VA/PA锚点，保留分页APIC的真实叶/PTE路径，区分非分页IO/MSR。可用 `tools/audit_fst_dynamic_translations.py SIDE --fst FST --require-complete` 做严格校验：分页成员要求完整叶/PTE证据，已认证非分页成员要求正确类型，普通unknown仍拒绝。实施与单项验证见[动态翻译消费端记录](../docs/dynamic-translation-consumer-results-20261029.md)。

本次gem5构建按用户要求使用 `-j128`，Python3.11；需令运行/构建辅助程序能找到 `libpython3.11.so`，例如设置 `LD_LIBRARY_PATH=/opt/gcc-11/lib64:/root/.pyenv/versions/3.11.14/lib`。格式与结果见 [动态翻译producer说明](../docs/dynamic-translation-producer-results-20261029.md)。

## X86 大内存：默认关闭（2026-10-27）

新组件分别在 overlay 的 `src/python/gem5/components/boards/x86_large_memory_board.py`
和 `src/python/gem5/components/memory/x86_multirange_memory.py`。0002 只将它们注册到
嵌入 Python，不修改 `x86_board.py`、`memory.py`、`hbm.py`。manifest 的
`features.x86_large_memory` 记录新文件指纹、开关及支持范围；原 artifact 是历史参考，
不能充当新二进制身份。

运行 `scripts/run_gem5_sync_probe.py` 时，`--x86-large-memory --mem-size 32GiB`
才启用双区间 DDR4。容量不超过 3GiB 始终使用原组件；未开启却请求更大容量会报错。
新路径不支持 HBM 或 `--ruby-functional-backing-store`，每段 N 个通道，总计 2N 个
控制器/directory。旧 checkpoint 仍要求采集时的二进制和 runner 哈希；请保留对应
冻结工具，或为新二进制重建 checkpoint。

**不要再将历史 `patches/gem5-stdlib-x86-multirange-memory.patch` 应用于生产公共类。**
它只保留为 2026-10-26 独立实验的来源记录。用法、验证范围和证据见
[大内存 runbook 第 12 节](../docs/gem5-x86-fs-large-memory-runbook-20261026.md#12-默认关闭的大内存入口2026-10-27)。

## buffer TSO variant 说明

`variants/tso-buffer.patch` 是 `--tso-store=buffer` 机制（opt-in 实验模式，仅
`MESI_Three_Level`，默认 serial）。**该改动已并入 `0001` 合并补丁**（生产树本身
已含 buffer TSO，默认走 serial），因此按上面流程重建的树**已经带 buffer TSO
能力**，无需也**不要**再 apply 此 variant 补丁（会 double-count / 冲突）。它仅
在需要把 buffer 从 baseline 拆出为独立可选层时用作参考。variant 的机制、验证与
一键 serial 回退见
[`../docs/gem5-spr-tso-store-buffer-design-20260917.md`](../docs/gem5-spr-tso-store-buffer-design-20260917.md)。

## 维护者：从生产树重新导出

见设计文档
[`../docs/gem5-collection-overlay-design-20260922.md`](../docs/gem5-collection-overlay-design-20260922.md)
第 6 节。要点：overlay 收 untracked 且非 `.orig/build/resources`，patch 收
`git diff c8222cc` 的 tracked-modified，`.orig`/`resources/`/`build/` 排除。
</content>
