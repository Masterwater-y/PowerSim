# SPR 线程状态 hook 的 ROM 来源修复

Zstd C4 关闭硬件预取采集的第一次有效启动，在 core 1、ordinal 2656920、
PC `0x428640` 触发 `thread-state runtime marker is not user-side`。
该 PC 是 `fastsim_wait_marker` 的 `nop; ret`；截断 FST 的末尾在相同 PC
出现连续内核入口微操作。PC 本身不足以证明真实执行了该标记。

`isThreadStateHook` 现在同时检查 PC 与 micro-PC，排除 ROM 微操作。
线程标记、调度 hook、task-clock hook 及未保留 hook 计数使用同一个谓词。
ROM 记录和原有 owner 初始化保持；真实标记的 CPL/ABI/任务身份检查保留。
未修改 CPU、cache、预取、负载及采集窗口参数。

本次新增差异见 `patches/spr-gem5-thread-hook-rom-20261109.patch`。
已有的子仓库未提交改动未被重置，也未包含到此增量 patch。

简单验证命令：

```bash
/root/.pyenv/versions/3.11.14/bin/python3 -m unittest \
  tests.test_spr_thread_state_hook_origin tests.test_native_task_clock_producer
```

4 项小测试通过；新回归在旧谓词上先复现失败，再在修复后通过。
编译/二进制保护证据保存在 `tmp/spr-thread-hook-rom-fix-20261109/`。
原批次的 gem5 和配置使用独立冻结副本，不覆盖、不重启其余四案。
Zstd 修复后新采集的状态以新目录中的 receipt、准备及审计结果为准，
小测试通过不代表完整采集或硬件 PMU 资格已通过。

构建使用 `TAOGEN_SHARED=$PWD/taogen/shared`、Python 3.11、GCC 11 和
`scons --linker=gold build/X86/gem5.opt -j128`，返回 0；新二进制的
`--build-info` 启动检查通过。旧冻结二进制/配置的 5 个条目均与原始 hash 一致。
新二进制 SHA-256：`7adee6210b02bdfa5a5f9d81c98c260bc91b59b9f1821b542673c45779a46402`。
Zstd 已在 `tmp/spr-no-prefetch-c4-20261109/zstd-retry2/` 单独重启，
统一当前来源索引为原批次的 `current-sources.json`。完整采集和来源审计尚待完成。
