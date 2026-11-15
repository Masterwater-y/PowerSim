# 一键配置与小 FS 验证

两个脚本分别在客户端电脑和 Linux 仿真服务器运行。

## 放行客户端沙箱配置

下载或复制 `scripts/configure_trae_kvm_sandbox.py` 到 TRAE 客户端所在电脑，
在该电脑的普通终端运行：

```bash
python3 configure_trae_kvm_sandbox.py
```

Windows 可使用 `python configure_trae_kvm_sandbox.py`。

脚本默认编辑当前用户的 `~/.trae-cn/sandbox.json`。原配置存在时先创建带时间戳的
备份，保留其他设置和网络规则，将 `/dev/kvm` 加入 `filesystem.readWrite`，
移除 `readOnly` 中同一个 `/dev/kvm` 项。重复运行不会重复添加或重复备份。
原 JSON 损坏时直接报错，不覆盖它。

请用 TRAE 的“设置 → 对话流 → 沙箱自定义配置 → 打开配置”核对实际配置文件。
如果该入口打开了其他位置，可以指定它：

```bash
python3 configure_trae_kvm_sandbox.py --config "/实际打开的路径/sandbox.json"
```

配置脚本不会修改设备系统权限，也不能证明当前会话已经加载配置。
此前只修改远端 `/root/.trae-cn/sandbox.json` 未解决实际 FS 启动的沙箱拒绝。
如仍未生效，可重载窗口或重连 Remote SSH，然后用真实 FS 命令验证。

## 运行三个小 FS 负载

在 Linux 仿真服务器上，通过你自己控制的普通 SSH 终端运行：

```bash
bash /data00/yinhaolang/FastSim/scripts/run_spr_retired_cache_smoke_oneclick.sh
```

脚本会自动选择当前已安装的 Python 3.11，校验工具和本地内核/磁盘哈希，
检查 KVM API，然后依次为 mode 0、1、2 保存准备状态、恢复真实 FS 采集，
调用维护的输入准备和独立退休/native 审计。参数为 C4、32GiB、
500K warmup、50K target；沿用既有 FST 和空闲磁盘保护。
明确使用已核对的本地资源，不依赖旧磁盘下载锁。

如需只检查依赖，不启动 FS：

```bash
bash /data00/yinhaolang/FastSim/scripts/run_spr_retired_cache_smoke_oneclick.sh --check-only
```

默认创建新的 `tmp/spr-retired-cache-smoke-manual-时间-进程号/`，
也可以通过 `--output` 指定项目内一个尚不存在的目录。不会覆盖旧结果。
终端会打印回执目录和各阶段日志位置。遇到采集、来源、人口、unknown
或原生守恒门禁失败时停止后续模式；Ctrl+C 会通知采集进程收尾。

结果包括：

- `summary.md`：逐核合格退休 load、三个事件 miss 和 unknown 数量/占比。
- `summary.json`：各模式状态、服务路径计数、正负估计拆分和门禁。
- `mode*-retired-audit.json`、`mode*-native-audit.json`：独立审计结果。
- 逐阶段命令、日志、退出码，以及原始 FS 采集回执与源文件。

退出码 0 仅表示三个小 FS 的 `workload_task` 零 unknown 和来源门禁通过。
服务路径覆盖按实际计数报告；中性、同源回放和四负载最终验收仍需分别完成。
未执行的模式没有退休计数，不能当作零 unknown。硬件等价仍为 `unverified`。

当前已经通过脚本语法、依赖/输入预检查，以及配置备份、合并、重复执行和
损坏 JSON 拒绝检查；没有通过智能体启动真实 FS，本说明不声明验收完成。
