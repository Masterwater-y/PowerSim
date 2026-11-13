# SPR-M2：Retired Prepare + Rename Elimination 默认启用

2026-11-12，用户明确要求默认开启已验证的 MOV / zero-idiom 候选。当前 SPR 默认入口为 `configs/spr-evaluation-default.cfg`，选择不可变的 `configs/spr-m2-rename-elimination.cfg`；`configs/gem5-spr-retired.cfg` 使用相同入口。

`build/fastsim` 安装与原候选逐字节相同的已验证执行文件，SHA-256 为 `0b5aacd527e837771f2693ecfd8225ad2839950dfc33615944e861ea9da822b2`。冻结副本在 `tmp/spr-m2-rename-elimination-20261112/fastsim`。221 个主工作区构建输入与候选一致，本次未修改模型源码或重新测量精度、吞吐。

SPR-M2 继承 SPR-M1 的全部有效配置，只增加：

```ini
core.spr_rename_elimination = true
core.spr_rename_policy_sha256 = 8d6243eaa281ab5dbf0942def5823ec6ad1a82757284e8ae3a3ed7a0af1ba15b
```

退休 store prepare 和 `v2-wait` 保持启用。各来源仍须覆盖自身 geometry/timings 并绑定自身 `thread-state.plan`；仍保留退休软件预取指令。已知不适用、资格缺失、形状不符或不支持的 PRF 模式继续走原执行路径。

对默认入口、维护 SPR 入口、冻结 M1 和显式 OFF 配置的实际 C++ 配置加载检查均通过；候选完整 `fastsim_tests` 和独立消除测试通过。通用非 SPR 的库构造默认仍为 OFF，SPR 默认通过版本化配置启用。

已有五负载 CPI-only 验证的 MAE 为 **0.179390 cycles/macro**、MAPE 为 **10.89%**；冻结 M1 为 **0.191599 cycles/macro**、**12.99%**。只有 XZ 的整体 CPI 绝对误差小幅增加，其他四负载缩小。Zstd 两组配对的进程吞吐 ON/OFF 为 −7.12%，simulator 吞吐 ON/OFF 为 −7.93%。默认启用是用户的推广决定；30 组稳定吞吐验收和完整 native 主缓存 PMU 认证仍未完成，已停止的后台测量和审计不会自动重启。

关闭消除路径时，在来源绑定的 SPR 配置之后增加：

```ini
core.spr_rename_elimination = false
```

恢复真正冻结的 M1 对照时，使用 `tmp/spr-m1-20261111/fastsim` 与 `configs/spr-m1-retired-prepare.cfg`，并绑定原来源配置。M1 执行文件 SHA-256 仍为 `dad62e0d67bea89cd9c69a946a2bd5e1eac89b7e9f3edf54704c0f81f13dc905`，原 M1 配置和里程碑记录保持不变。

- [SPR-M2 执行文件与有效配置身份](../configs/spr-m2-rename-elimination.json)
- [实现、资格近似与回退合同](spr-rename-elimination-20261112.md)
- [五负载精度与已完成吞吐配对](spr-rename-throughput-regression-20261112.md)
- [实际配置加载检查](../tmp/spr-rename-elimination-20261112/check-default.log)
- [激活记录](../tmp/spr-m2-rename-elimination-20261112/activation.json)
