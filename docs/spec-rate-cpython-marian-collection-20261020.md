# CPython / Marian 标准 rate 采集（2026-10-20）

用户要求扩展现有 SQLite/Zstd 流程，新增 CPython 和 Marian，并限制 warmup 时间。本轮沿用四核、四个标准 rate 副本、8 通道、buffer TSO114、实际 DRAM 参数映射和 first-core-target common-end，不修改 FastSim 模型。

## 已确定的输入和 ROI

- **714.cpython_r：** 官方 refrate 第二条 `-I -B coreml_pb.py -i 5 -a -c -m MobileNetV2.mlmodel -d 20`。保留完整模型、5 次迭代和全部分析选项；选择第二条输入是四副本内存预算的前置选择，不将模型或算法缩小。初始 ROI 在 `do_bmk()` 完成模型读入/反序列化后、`analyze_msg()` 前；解释器启动、模块导入和首次解析留在 KVM。标记版给 `sys` 增加仅用于采集的 C 入口，复用既有 rate gate 和 runtime/OS 身份 ABI。
- **772.marian_r：** 官方 refrate 第一条 TildeMODEL 命令及完整输入；保留 `--cpu-threads 1`、`--mini-batch 32`、`--maxi-batch 100`、`-w 512`、`intgemm8` 和标准 rate 编译宏。初始 ROI 在首个 batch 的 decoder `t=1`，`t=0` 的首次 forward/权重准备在 KVM 完成，随后采集实际解码阶段。它是原生 std::thread 工作流，不能强制改成 OpenMP，也不能因 `/proc` 有多个线程就否定标准 rate 语义。

这是官方输入的指定计算阶段研究窗口，不代表整套 SPEC 成绩。先进行完整宿主正确性、四副本内存/线程检查，再真实恢复短窗口检查，才能升级为正式采集来源。静态位置合理不替代执行阶段取证。

## 采集和空间门禁

1. 只读使用 `/data00/zhonghaocheng/benchmark/spec_cpu_2026`。独立构建目录为 `tmp/spec-rate-cpython-marian-20261020/spec`，只构建这两个负载，发行文件先按 MANIFEST 验证；其他 benchmark 仅供 harness 发现的链接不用于运行。
2. 新增目录输入的 tar staging，归档内容哈希与 readfile 哈希进入 checkpoint 身份；拒绝逃逸路径、链接及覆盖 workload/launcher 的成员。各副本使用独立输出目录，共享输入链接，避免 Marian 输出相互覆盖。
3. 使用当前 gem5 创建新的真实 pre-fork staged KVM checkpoint，绑定 binary/runner/输入/硬件/kernel/disk；恢复后 KVM 完成初始化，四副本计算入口全部 ready 后切 O3+Ruby。
4. 初始沿用 1,000,000 instruction-stop warmup、KVM quantum 10µs；宿主 warmup 上限 180 秒。超限记录失败，不延长上限或强制开 stat；先诊断实际阶段/预算是否正确。短恢复检查也保留相同预热预算。
5. 正式目标为最快参与核 10,000,000 measured user UOP，共同关闭，保留真实宏/UOP和线程归属。先通过恢复/人口门禁，再正式采集，最后使用维护的 native validator 做回放核验。
6. 全局 FST ≤100,000,000,000 bytes，99GB 提前停止，剩余空间保留20GB；逐案≤20GB，顺序采集。有效旧回归源保留，失败/过期新 FST 如清理则记录逐文件身份收据。当前不因新增任务而删除有效 SQLite/Zstd 来源。

## 验证状态

实施中。初始目录输入测试 RED4 → GREEN4；每副本独立输出测试先因 launcher 不认识选项失败，再通过实际四进程输出核对。相关 rate 测试共10项通过。源码构建、完整宿主输出、真实恢复及正式来源的完成状态随后以实际收据更新，不把测试通过作为采集成功。

构建环境首次缺少隔离安装的 flags DTD 和其他 benchmark 发现元数据，尚未进入目标编译即失败；补齐 Docs 与只读发现链接后重新运行，失败日志保留。原版 CPython 已由 harness 构建，Marian 原版正在构建；尚未声称标记版或 FS 采集完成。
