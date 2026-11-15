# 小负载 unknown 验证回执

2026-11-07：已尝试启动三个真实四线程 FS 小负载，全部在准备状态保存阶段
被沙箱拒绝访问 `/dev/kvm`。尚未进入测量窗口，无法报告逐核 unknown 数量。

| 模式 | 返回码 | 停止原因 | 测量窗口打开 | FST 字节 | 退休 unknown |
|---|---:|---|---|---:|---|
| 0：重复读取、软件预取候选 | 1 | producer-fatal | 否 | 0 | 未测量 |
| 1：转发、跨行候选 | 1 | producer-fatal | 否 | 0 | 未测量 |
| 2：共享读写候选 | 1 | producer-fatal | 否 | 0 | 未测量 |

每次实际 FS 启动约 16.45 秒结束。任务入口采用 C4、SPR DDR5/32GiB、
500K warmup、50K target；未覆盖这些测量预算。模式名称不作为特殊路径覆盖证据。

执行工具明确返回：

```text
TRAE Sandbox Error: hit restricted
Not allow operate files: /dev/kvm
```

gem5 日志对应：

```text
src/cpu/kvm/vm.cc:83: fatal: KVM: Failed to open /dev/kvm
```

最小 `System + KvmVM` 实例化探测曾成功，但完整 FS 命令仍被拒绝。
现有远端 `/root/.trae-cn/sandbox.json` 已包含
`filesystem.readWrite=["/dev/kvm"]`；文件存在没有证明当前执行策略生效。
需要在 TRAE 客户端“设置 → 对话流 → 沙箱自定义配置”中使设备放行对当前会话
生效。当前工具没有刷新客户端执行策略的接口。

此前启动还遇到旧尝试留下的磁盘资源下载锁。已正常停止这些等待，
并为维护的小负载入口增加本地内核、磁盘参数，检查参数转发通过。
重启使用的本地内核、磁盘与原资源 SHA256 一致；原锁与资源没有被修改。

最终流程命令 ID：`96fa38c969a39142c7d451f7238a6743`。
三个 gem5 进程已退出，四负载重采集未启动。零 unknown 验收与 SPR 硬件等价
均未得到新证据。

回执位于 `tmp/spr-retired-cache-v2-20261107-build/`：

- `smoke-session2-local-completion.json`：三个模式的最终启动失败状态。
- `checkpoint-mode{0,1,2}-session2-local-run/capture-receipt.json`：逐次返回码、
  停止原因、窗口状态、FST 字节及存储保护回执。
- 同目录 `capture.log`：逐次 gem5 设备访问失败日志。
- `smoke-mode{0,1,2}-session2-local-checkpoint-command.json`：可复现的完整命令。
