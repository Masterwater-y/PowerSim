# 四个 SPECspeed 负载的 SPR C4 重采设计

用户要求立即并行采集 Graph500、XZ、FLAC、Marian 四负载的 C4 FST，并以
FastSim 验证，充分使用可用并行资源。使用独立 gem5-spr/yinhaolang 当前采集器，
保留 SPR 原生 CPU/cache/DDR5 行为；父仓库 fastsim-spr。当前退休流结束规则为
retired-stream-completion-v1。只提供退休指令与功能信息，不把原生时长输入模型。

复用已保存的官方 speed 研究二进制、输入、原生线程数/启动器及 ROI，不复用旧
gem5-fs checkpoint。四核分别绑定原生工作线程；XZ 为四压缩 worker 加一生产者。
采集首核达 10M 用户 UOP 后共同截止，全部已保留人口回放；身份齐备后预热
500K 指令。XZ 使用当前 32GiB 正式矩阵的 refspeed 输入与命令；2026-10-20 的 train 探针不作为本批输入。

当前入口固定 2GiB，Marian/XZ 容量不足。新增显式 32GiB 大内存配置，默认
2GiB 不变。复用已有 split-range/E820 算法，用配置局部 DDR5 子类保持
DDR5_4400_4x8；每 RAM 段 16 通道、两段共 32 控制器，不能将其冒称总16通道。
不修改任何非 probe src；不调用旧 configure_o3，不覆盖原生 SPR 参数。

四个二进制均缺 fastsim_sync_marker；Graph500 使用普通 libgomp。保留自然
同步和原生 scheduler/thread-state/task-clock 采集，不补空标记，不冒称高层
OpenMP barrier 因果完整。是否可评分由实际原生计时/源/人口审计决定；缺项保留。

四路并行：每路分配24物理核及对应24 SMT逻辑CPU，覆盖全部192逻辑CPU；单案
gem5详细阶段的单线程性质不因CPU掩码加宽而改变。每案完成后立即 prepare、按
实际 config.ini 映射、回放、维护 validator，其他采集继续。独立身份门禁与
native来源/守恒/逐核人口/CPI资格分别报告，不能把passed当精度阈值验收。

现有全局FST约295GB、实际空闲93GB。逐案FST预算 Marian8GB、Graph5008GB、
XZ10GB、FLAC14GB，共40GB；预留14GB旁表空间。沿用全局400GB/395GB停止及
30GB空闲底线，额外35GB空闲哨兵仅终止本批所拥有的进程。保留失败现场，
不删除旧源、不因不足预算把截断样本评分。旧checkpoint不迁入新架构。

所有命令、二进制/配置/输入身份、PID、计时阶段、容量及逐案失败原因写入
tmp/spr-speed-c4-20261104/。维护入口 prepare_spr_capture.py 及
run_native_kernel_fastsim_validation.py 负责审计/评分，禁止临时另选参考。
并行运行只用于缩短任务墙钟，不据此报告单案吞吐精度。
