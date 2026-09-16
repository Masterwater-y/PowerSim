# QEMU-FST v11 跨仓 patch（producer 侧）

本目录是 QEMU-FST v11 转换体系中 **QEMU producer 两仓** 的可分发 patch，供其他作者
在官方基线上重建跨仓联动。它与顶层 `patches/`（gem5/TaoTrace 线，plain `-p1`
overlay）互不混用；QEMU-FST 线的 gem5 侧改动仍以 `integrations/gem5/qemu_fst/`
源码 overlay 形式提供，不在此目录。

对应基线与 pin 见 `docs/qemu_fst/mainline.md §1` 与 `tools/qemu_fst/build.py`
（`QEMU_UPSTREAM_COMMIT` / `QEMU_INTEGRATION_COMMIT` /
`TRACER_UPSTREAM_COMMIT` / `TRACER_INTEGRATION_COMMIT`）。三方 SHA 一致。

## Patch 清单

每仓一份 squashed 平铺 `-p1` 统一 diff：把各仓官方基线之上的多次本地提交压成
一个干净提交后的完整 diff，内容与压缩前逐位等价。

| patch | 目标仓库 | 上游基线 | squash 后 integration commit | 规模 |
|---|---|---|---|---|
| `qemu-fst-observation-v11.patch` | `qemu` | `v11.0.0` @ `98b060da` | `67be5c84` | 24 文件 +830/−48 |
| `qemu_tracer-native-kernel-fst.patch` | `qemu_tracer` | `origin/master` @ `abd8dc2b` | `bcd8c779` | 19 文件 +1946/−275 |

- `qemu-fst-observation-v11.patch`：QEMU-FST 结构化全系统 observation API。唯一新增
  文件 `include/plugins/qemu-fst-observation.h`（私有只读 vtable
  `qemu_fst_query_observation_api`）；`include/plugins/qemu-plugin.h` 与 v11.0.0
  逐字节一致。其余为 `accel/tcg`、generic `cpu.h`、`plugins/*`、`target/i386/*`、
  build/symbol 脚本的修改。
- `qemu_tracer-native-kernel-fst.patch`：native-kernel 采集 frontend，消费上面的
  observation ABI v1。唯一新增文件 `common/trace_entry_extensions.h`；bulk 在
  `frontend/`（`sender.h`/`frontend.cc`/`sampler.h`/`arch_state.h`），另含
  `backend/dumper/*` 与 `lib/drmemtrace/*` 修改。

sha256：
```
6e96e1d1f4b75bd016f4094c2f310454c5a201d67e148e60bf85cb604cee0347  qemu-fst-observation-v11.patch
1ed924f8625c9e8e3c9b62713f334ac7ad95260bf8e3900579554948f3dcf68f  qemu_tracer-native-kernel-fst.patch
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

应用后各仓工作树内容等价于 `build.py` 所 pin 的 `67be5c84` / `bcd8c779`；
`tools/qemu_fst/build.py` 用 `git merge-base --is-ancestor` 校验 HEAD 派生自这两个
integration commit（提交后满足）。gem5 仍需 pin 到 `v25.1.0.1 @ c8222cc6` 并挂
`integrations/gem5/qemu_fst/` overlay，FastSim 基线见 `mainline.md §1`。

## 验证

- dry-run：`git apply --check -p1 <patch>`（在对应干净基线 worktree 上）。
- 干跑通过日期：2026-09-16，两 patch 均在新鲜 `v11.0.0` / `abd8dc2b` worktree 上
  `git apply --check` 干净。
- squash 前后内容一致性：`git diff <base>..<integration> --stat` 与 patch `--stat`
  逐项相等（24 文件 +830/−48；19 文件 +1946/−275）。
