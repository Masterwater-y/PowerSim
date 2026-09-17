# QEMU-FST v11 跨仓 patch（producer 侧）

本目录是 QEMU-FST v11 转换体系中 **QEMU producer 两仓** 的可分发 patch，供其他作者
在官方基线上重建跨仓联动。它与顶层 `patches/`（gem5/TaoTrace 线，plain `-p1`
overlay）互不混用；QEMU-FST 线的 gem5 侧改动仍以 `integrations/gem5/qemu_fst/`
源码 overlay 形式提供，不在此目录。

对应官方基线见 `docs/qemu_fst/mainline.md §1` 与 `tools/qemu_fst/build.py`
（`QEMU_UPSTREAM_COMMIT` / `TRACER_UPSTREAM_COMMIT`）。patch 从各仓官方基线到
当前已验收工作树生成，不要求外部仓先创建 integration commit。

## Patch 清单

每仓一份平铺 `-p1` 统一 diff，完整表达各仓官方基线到当前工作树的差异。

| patch | 目标仓库 | 上游基线 | 规模 |
|---|---|---|---|
| `qemu-fst-observation-v11.patch` | `qemu` | `v11.0.0` @ `98b060da` | 24 文件 +830/−48 |
| `qemu_tracer-native-kernel-fst.patch` | `qemu_tracer` | `origin/master` @ `abd8dc2b` | 21 文件 +1983/−160 |

- `qemu-fst-observation-v11.patch`：QEMU-FST 结构化全系统 observation API。唯一新增
  文件 `include/plugins/qemu-fst-observation.h`（私有只读 vtable
  `qemu_fst_query_observation_api`）；`include/plugins/qemu-plugin.h` 与 v11.0.0
  逐字节一致。其余为 `accel/tcg`、generic `cpu.h`、`plugins/*`、`target/i386/*`、
  build/symbol 脚本的修改。
- `qemu_tracer-native-kernel-fst.patch`：native-kernel 采集 frontend，消费上面的
  observation ABI v1。新增 `common/trace_entry_extensions.h` 与独立的
  `frontend/native_fst_session.{h,cc}`；后者拥有 Frame 生命周期、state delta、
  RAM/PIO/MMIO 事实和 raw 写出，`sender.h` 不再承载 Native 主数据流。

sha256：
```
6e96e1d1f4b75bd016f4094c2f310454c5a201d67e148e60bf85cb604cee0347  qemu-fst-observation-v11.patch
2070bb8b128fb7c2b4873c59636c5804fd7e38400eac4ee4a196d2dcad1905be  qemu_tracer-native-kernel-fst.patch
```

## 应用（两仓独立，无先后依赖）

在各自仓库根、干净的官方基线 checkout 上以 `-p1` 应用：

```sh
# qemu（基线 v11.0.0 @ 98b060da）
cd <qemu-checkout>
git checkout 98b060da3a4f92b2a994ead5b16a87e783baf77c
git apply -p1 /path/to/minesim/patches/qemu_fst/qemu-fst-observation-v11.patch

# qemu_tracer（基线 origin/master @ abd8dc2b）
cd <qemu_tracer-checkout>
git checkout abd8dc2b2a4d09ceb93caf34b3932589f150bfd2
git apply -p1 /path/to/minesim/patches/qemu_fst/qemu_tracer-native-kernel-fst.patch
```

应用后各仓工作树包含当前 producer 实现；`tools/qemu_fst/build.py` 用
`git merge-base --is-ancestor` 校验 HEAD 派生自对应官方基线，并允许应用 patch
后的未提交工作树。gem5 仍需 pin 到 `v25.1.0.1 @ c8222cc6` 并挂
`integrations/gem5/qemu_fst/` overlay，FastSim 基线见 `mainline.md §1`。

## 验证

- dry-run：`git apply --check -p1 <patch>`（在对应干净基线 worktree 上）。
- 干跑通过日期：2026-09-17，两 patch 均在新鲜 `v11.0.0` / `abd8dc2b` worktree 上
  `git apply --check` 干净。
- patch 内容由 `git diff --binary <official-base>` 生成；qemu_tracer 使用临时 index
  同时纳入 tracked 修改与 `NativeFstSession` 新文件。当前规模为
  24 文件 +830/−48 与 21 文件 +1983/−160。
