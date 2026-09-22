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
| `variants/tso-buffer.patch` | buffer TSO variant（reference-only，见下） | 通常**不要**再 apply |
| `CHANGELOG.md` | 修改历史与补丁分组来源 | — |

**核心规则：overlay 与 patch 不可混。** 新增文件（上游 `/dev/null`）无法用
patch 表达，必须走 overlay；改上游文件走 patch。混用会导致协作者无法重建——这
是本仓库旧 `patches/` 无法 bootstrap 的根因。

## 前置条件

- 上游 gem5 `v25.1.0.1` / commit `c8222cc`（唯一 baseline，patch offset 依赖它）
- Ruby 协议 `MESI_Three_Level`，构建目标 `build/X86_MESI_Three_Level/gem5.opt`
- Python **3.11** + SCons **4.9**（系统 Python 2/3.7、旧 SCons 3.0 不兼容）
- `TAOGEN_SHARED` 指向本机 taogen shared 路径

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

# 3) 构建
cd "$GEM5"
export TAOGEN_SHARED=<本机 taogen shared 路径>
python3.11 "$(which scons)" build/X86_MESI_Three_Level/gem5.opt -j"$(nproc)"

# 4) 验证
sha256sum build/X86_MESI_Three_Level/gem5.opt   # 对照 manifest.artifact(跨机可能不同)
python <FastSim>/tools/validate_fs_oracle_identity.py <run-dir>   # 期望 valid=true(权威)
```

若 patch 报 “No file to patch”，多为 overlay 未铺或 baseline commit 不对。

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
