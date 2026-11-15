# 五负载 C4 关闭预取和 early store prepare 采集

用户于2026-11-10授权退役已列出的历史/失败FST，并并行采集新一组
zstd、xz、Graph500、FLAC、Marian。采集目录为
`tmp/spr-no-prefetch-no-early-prepare-c4-20261110/`。

## 采集配置与来源

- 五案并行，4个详细参与核、32GiB split-range x86 RAM；500K预热、
  首核10M用户UOP共同终点，其余参与核保留到同一终点。
- 四个L1D及四个private-L2的`enable_prefetch=false`；四个详细核的
  `sprEarlyStorePrepare=false`。软件预取ISA指令保留在功能输入中。
  关闭的是退休前early prepare；普通store权限获取及退休后prepare遵循原协议。
- 使用原成功采集的命令、工作负载、输入和冻结Python配置；zstd使用此前成功
  retry2命令，保持4个rate copies及已修复的launcher参数。
- 五案统一使用此前成功的ROM thread-hook修复二进制，复制至本批
  `frozen/gem5.opt`，SHA256应为
  `7adee6210b02bdfa5a5f9d81c98c260bc91b59b9f1821b542673c45779a46402`。
  本次不修改或重编译gem5/FastSim模型。配置覆盖仅在本批wrapper生效。
- CPU mask覆盖当前可用CPU并分成五组，避开已有实验使用的128–139；
  不中断已有FastSim回放。本次并发采集不是吞吐测量。

## 空间与退役证据

执行显式清单`tmp/fst-retention-candidates-20261110.json`中的六组：
zstd失败retry1、common-end首次中止、11月8日C4、11月4日C4、
旧static-speed-c4及static-speed-c4-v2。保留当前关闭预取五案、逐load诊断源、
最新开启预取对照和C4/C8/C16正式矩阵。

`tmp/fst-retirement-20261110/receipt.json`确认删除84个FST及676个专属旁表，
总计61.68GB；其他7,140个FST属性未变。所有删除项先记录SHA256/size/inode/mtime，
复查活动文件引用和文件属性，再逐项unlink；没有整目录删除。

新案FST预算分别为Marian6GB、Graph5006GB、XZ7GB、FLAC7GB、zstd6GB，
总预留32GB，通过共享`native-capture-reservations.json`入场。
入场全项目FST335.06GB，预算后367.06GB，小于395GB停止线；实际空闲
154.42GB，另留20GB旁表余量和35GB启动保护。采集guard仍执行全局400GB、
395GB停止线及至少30GB磁盘空闲要求。

## 自动检查和当前状态入口

`launch.py`复用维护版`Campaign`和存储guard。每案完成后自动执行
`prepare_spr_capture.py`、独立target identity和退休load-miss来源审计；
这些是来源完整性门禁，不是模型准确性或硬件PMU等价认证。

`watch.py`读取实际`config.ini`，要求每案8个预取开关和4个early prepare开关
全为false；发现开启则停止本批采集。完成后检查各核
`storePrepareSpeculativeIssued`均为0。当前采集尚在进行，不能称五案采集完成。

- `plan.json`：逐案命令、CPU mask、预算；`freeze.json`：冻结文件哈希。
- `campaign-status.json`：五案阶段和子进程；`completion.json`：最终来源门禁。
- `startup-verification.json`：实际配置核对；`ablation-health.json`：监控状态。
- `runtime-verification.json`：完成案的early prepare运行计数核对。
- `receipts/<case>/`及`sources/<case>/capture-receipt.json`：日志、退出和guard。

本次没有启动新来源的FastSim精度比较；新FST需先完成自身来源检查。

启动复核已确认五个冻结gem5进程均在运行，五案实际配置共40个硬件预取
开关和20个early prepare开关均为false，未发现失败。
启动收据为`startup-summary.json`；该收据是启动状态快照，不是采集完成证明。
