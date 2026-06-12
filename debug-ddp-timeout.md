# [OPEN] debug-ddp-timeout

## Symptom
- 8 卡 DDP 训练可以启动，能跑到约 `step 140`
- `rank_0.log` 显示收到来自 `rank 5` 的 NCCL collective timeout dump signal
- 最终所有 rank 被 `ProcessGroupNCCL` 终止

## Scope
- Project: `LLMSim`
- Entry: `scripts/train_current6_ddp8.sh` -> `scripts/launch_ddp8.sh` -> `train/train_lora.py`
- Dataset: `data/windows_train6_w512/windows.jsonl`

## Hypotheses
1. `rank 5` 对应 GPU/驱动存在瞬时异常，导致某一轮 collective 未参与，进而触发 NCCL timeout。
2. 某个 rank 在评估或保存 checkpoint 阶段耗时异常，造成各 rank collective 次序不一致。
3. DataLoader 或样本长度分布在某一批次触发单 rank 明显慢于其他 rank，最终被判定为 collective timeout。
4. 当前 DDP 进程环境变量或 NCCL 默认参数过于激进，未给长序列 + eval/save 留出足够 timeout。
5. 训练脚本中存在某个只在部分 rank 执行的路径，导致 collective 调用数量不一致。

## Evidence To Collect
- 各 rank 最后 100 行日志，确认最先异常的 rank 和时间点
- 训练脚本在 eval/save 前后的执行路径是否所有 rank 一致
- GPU 健康状态和该轮训练时的显存/掉卡情况
- `rank 5` 是否在超时前有 OOM、CUDA error、文件写入阻塞、checkpoint 阻塞

## Status
- Waiting for runtime evidence collection
