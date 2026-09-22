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
| `qemu-fst-observation-v11.patch` | `qemu` | `v11.0.0` @ `98b060da` | 27 文件 +1135/−48 |
| `qemu_tracer-native-kernel-fst.patch` | `qemu_tracer` | `origin/master` @ `abd8dc2b` | 24 文件 +1999/−170 |

- `qemu-fst-observation-v11.patch`：QEMU-FST 结构化全系统 observation API。唯一新增
  文件 `include/plugins/qemu-fst-observation.h`（私有只读 vtable
  `qemu_fst_query_observation_api`）；`include/plugins/qemu-plugin.h` 与 v11.0.0
  逐字节一致。其余为 `accel/tcg`、generic `cpu.h`、`plugins/*`、`target/i386/*`、
  build/symbol 脚本的修改。QEMU 单向发布 REP/PAUSE 分类、REP 完整宏 commit 与
  `RDTSC/RDTSCP` 实际结果，不拥有 raw 或 common-end 状态。
- `qemu_tracer-native-kernel-fst.patch`：native-kernel 采集 frontend，消费上面的
  observation API。新增共享 `common/qemu_fst_record.h` 与独立的
  `frontend/native_fst_session.{h,cc}`；后者是 pending macro/event、state delta、
  RAM/PIO/MMIO 事实、raw commit 与首核 CPL3 macro common-end 的唯一 owner。

sha256：
```
75feb97709998d85b25ec8f9b9043826376f0f1dbf064d81ee1cf4152fb9d577  qemu-fst-observation-v11.patch
d61120081b146e84fce749d9fe23fa8b811d4583636078f5ba86b0bbb9461ecd  qemu_tracer-native-kernel-fst.patch
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
- 干跑通过日期：2026-09-22，两 patch 均在新鲜 `v11.0.0` / `abd8dc2b` worktree 上
  `git apply --check` 干净。
- patch 内容由本地最终提交相对 official base 的
  `git diff --binary <official-base>..<integration-commit>` 生成。当前规模为
  27 文件 +1135/−48 与 24 文件 +1999/−170。
