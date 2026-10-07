# Buffer TSO：仅补队头 demand（2026-09-23）

按用户要求缩小 [上一版候选](buffer-tso-head-demand-20260923.md)：取消本次新增的
Sequencer 调度和大范围回退，仅保留队头 demand 的真实 cache 服务与 SQ 响应边。
`core.buffer_tso_prepare_lifecycle` 仍默认关闭，没有新增旋钮或调整延迟参数。

## 此次撤销与保留

- 删除 store demand 的 Sequencer proposal、混合 load/store 重新准入、实际占用
  区间认证和释放堆重建。恢复最初快照的请求容量记账；新增 demand 不另预订槽位。
- 删除新增的同组 replacement 顺序检查及其整 epoch 串行回退。不会因为这项检查
  把整批已接受的 prepare 重新变成 FIFO 完整层级服务。
- 保留原有资格、跨核、page-fill、atomic/barrier、split、权限丢失保护；原有回退
  不属于此次要求删除的新增回退。
- prepare 已完成的 follower 到达 FIFO 头时，以普通 write 再调用真实访存 replay，
  用实际本地响应释放 SQ；prepare 在途时接管现有服务；immediate head 只用原请求。
- 保留请求人口计数，以及 timing trial 的缓存/计数事务恢复。该恢复只丢弃试算，
  防止多轮时序求解重复提交 cache 请求，不会关闭 prepare 或改选串行路径。

## 明确的近似边界

本轮有意沿用原有两阶段 canonical 缓存状态和资源记账；没有实现按实际时间重新
排序的完整 cache 事件回放。因此额外 demand 的 LRU touch 可能相对同组 canonical
访问发生次序偏移；prepare/mandatory 也没有独立的精确资源账本。新增回退已删除，
不把“没有回退”称为缓存访问时间顺序已获认证。

实际服务来自现有 cache replay，不手工叠加 publish 常数。现有 L1 延迟仍是原配置
的统一近似，未完成与本次 gem5 采集配置的 store 专属响应边独立映射；不能仅因为
调用了 cache 函数就宣布协议时延已精确认证。本轮用于隔离 demand 服务的效果。

## 验证与来源

新工作目录：`tmp/buffer-tso-head-demand-only-20260923/`。上版源码快照在 `before/`，
相对上版的撤销见 `revision.diff`，相对最初实现前快照的保留改动见
`demand-only.diff`。旧版报告与实验输出保持原样，不将其数据混称为当前版本。

小集沿用原来的 8 个 common-end native-FS 案例（共 76 个逐核 trace）：LBM
C4/C8/C16/C32，以及 Stockfish、TeaLeaf、Graph500、NAMD C4。源为
`tmp/tso-buffer114-common-end-baseline40-20260918/formal/source/` 中对应案例。
这是 diagnostic pilot，不是 held-out 或全矩阵验收；不重新采集 gem5，不截短 trace。
TSO buffer=114，store forwarding/store-wait relief 关闭；dual 同时开启 residence
certificate 与 lifecycle，off 同时关闭。warmup、测量窗口与宏指令分母沿用原输入。

通过维护入口 `tools/run_native_kernel_fastsim_validation.py` 重跑 dual/off，
每组 `--expected-cases 8 --jobs 4`；独立身份检查结果在 `identity.json`。
CPI 仅取各 `validation.json` 的 `cpi` 字段，范围为 user-plus-kernel，使用各模拟器
自己的宏指令分母。各轮原始 `summary.json/csv/md`、逐例 validation、输入与二进制
指纹保存在实验目录。MAPE/MAE/百分位按 case 等权，WAPE/bias 按匹配宏指令人口加权。
并发重跑不作吞吐比较，native PMU 仍仅为诊断，源门通过不等于精度通过。


## 最终验证结果

- `cmake --build build -- -j16` 成功，`./build/fastsim_tests` 输出
  `all FastSim tests passed`。构建仅出现既有串行 LTO 提示。
- 新反例保留两个能共存的同组缓存行：后续 load 不应使整批 prepare 失效。
  修改前该断言失败（`red-tests.log`），撤销新增回退后通过。仍覆盖 FIFO 服务下界、
  immediate head、在途接管、SQ 容量、跨 epoch、warmup 和原有受保护情形。
- 原有 Sequencer 容量测试继续通过，但它验证的是保留的旧资源记账，不把它表述为
  新 demand/optional 资源模型已经认证。只读代码复查未发现撤销范围的残留。
- dual/off 两组均为 8/8 discovered、passed、native PMU scored，failed/pending=0；
  独立身份检查 8/8 valid。ON 的逐核宏指令、UOP、访存及分支人口守恒。
- OFF 8/8 精确一致：scope（排除宿主 throughput）、完整 threads、所有既有逐核
  字段均与最初实现前二进制一致；三个新增请求人口计数为零。
- 当前冻结二进制为 `fastsim-demand-only`，SHA256：
  `791158d75820236110039f107d7c80a3e44b2145ccc6b3956a1c1afd6c33d12c`。
  源码及配置散列在 `fingerprints.json`，验证后再次核对无变化。

复现开启态：

```bash
python3 tools/run_native_kernel_fastsim_validation.py \
  --source-root tmp/buffer-tso-head-demand-only-20260923/source \
  --fastsim tmp/buffer-tso-head-demand-only-20260923/fastsim-demand-only \
  --config tmp/buffer-tso-head-demand-only-20260923/dual.cfg \
  --output-dir tmp/buffer-tso-head-demand-only-20260923/repeat-dual \
  --expected-cases 8 --jobs 4
```

“实现前”是原报告的 `before-dual`；“已撤销组合版”是原报告的 `final-dual`；
“仅 demand”是本报告的新冻结二进制。没有把关闭 residence certificate 的 off
作为开启态的精度基线。逐例与汇总如下。

| 案例 | gem5 CPI | 实现前 CPI | 旧相对误差 | 旧 CPI 绝对误差 | 仅 demand CPI | 新相对误差 | 新 CPI 绝对误差 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| c04-782.lbm_r | 0.817326 | 0.773531 | -5.36% | 0.043795 | 0.775517 | -5.12% | 0.041809 |
| c08-782.lbm_r | 0.822077 | 0.758934 | -7.68% | 0.063144 | 0.755658 | -8.08% | 0.066419 |
| c16-782.lbm_r | 0.951678 | 1.045025 | +9.81% | 0.093347 | 1.051002 | +10.44% | 0.099324 |
| c32-782.lbm_r | 1.502989 | 1.755288 | +16.79% | 0.252298 | 1.760671 | +17.14% | 0.257681 |
| c04-706.stockfish_r | 0.668626 | 0.660548 | -1.21% | 0.008078 | 0.662194 | -0.96% | 0.006432 |
| c04-811.tealeaf_s | 1.004183 | 0.862259 | -14.13% | 0.141924 | 0.860937 | -14.26% | 0.143247 |
| c04-854.graph500_s | 1.305529 | 1.163890 | -10.85% | 0.141639 | 1.180965 | -9.54% | 0.124564 |
| c04-857.namd_s | 0.479341 | 0.383074 | -20.08% | 0.096267 | 0.389498 | -18.74% | 0.089843 |

CPI 绝对误差与 MAE 的单位为 cycles/宏指令；相对误差带符号。

| 范围 / 版本 | MAPE | CPI MAE | P50 APE | P90 APE | P99 APE | 最大 APE | WAPE | signed bias |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 全部 8 案例 / 实现前 dual | 10.74% | 0.105061 | 10.33% | 17.78% | 19.85% | 20.08% | 13.72% | +8.87% |
| 全部 8 案例 / 已撤销组合版 | 86.70% | 0.863711 | 72.73% | 178.20% | 183.46% | 184.04% | 126.91% | +125.64% |
| 全部 8 案例 / 仅 demand | 10.54% | 0.103665 | 9.99% | 17.62% | 18.63% | 18.74% | 13.96% | +9.26% |
| LBM 4 案例 / 实现前 dual | 9.91% | 0.113146 | 8.74% | 14.69% | 16.58% | 16.79% | 14.03% | +12.19% |
| LBM 4 案例 / 已撤销组合版 | 164.93% | 1.630625 | 174.03% | 181.54% | 183.79% | 184.04% | 145.42% | +145.42% |
| LBM 4 案例 / 仅 demand | 10.19% | 0.116308 | 9.26% | 15.13% | 16.94% | 17.14% | 14.42% | +12.53% |

| 案例 | prepare | head demand | 在途接管 | canonical 回退 epoch |
| --- | ---: | ---: | ---: | ---: |
| c04-782.lbm_r | 3781346 | 3806230 | 194890 | 0 |
| c08-782.lbm_r | 7658105 | 7708526 | 373620 | 0 |
| c16-782.lbm_r | 14893110 | 14977656 | 585498 | 0 |
| c32-782.lbm_r | 29706309 | 29892925 | 1083526 | 0 |
| c04-706.stockfish_r | 263591 | 1970488 | 9036 | 0 |
| c04-811.tealeaf_s | 794401 | 1068495 | 23615 | 0 |
| c04-854.graph500_s | 1334456 | 1860988 | 71856 | 0 |
| c04-857.namd_s | 1543765 | 2079224 | 237676 | 0 |


## 结论

用户要求的范围撤销已完成。八个案例的新增 canonical 回退均为零；LBM 的大幅
退化消失，回到最初 dual 基线附近。全部 8 案例 MAPE 为 10.54%，CPI MAE 为
0.103665 cycles/宏指令，原基线为 10.74% / 0.105061；LBM 四例则从
9.91% / 0.113146 略退到 10.19% / 0.116308。

仅 demand 对 NAMD 的改善有限：相对误差从 −20.08% 到 −18.74%，CPI 绝对误差
从 0.096267 到 0.089843 cycles/宏指令。原组合版接近 gem5 的 NAMD 结果不能归功于
单独补 demand。受当前 canonical 状态与资源记账近似限制，这组结果也不等于已
实现精确 gem5 协议；但它不支持此前“补这一条边就能解释主要 CPI 缺口”的强结论。

本轮没有继续增加调度、回退或调参。最初“NAMD 明显收敛且 LBM C16/C32 不恶化”的
精度目标仍未达到；候选保持默认关闭，不扩全矩阵、不声明吞吐通过。

原始证据：
[身份门](../tmp/buffer-tso-head-demand-only-20260923/identity.json)、
[开启态维护报告](../tmp/buffer-tso-head-demand-only-20260923/final-dual/summary.md)、
[关闭态维护报告](../tmp/buffer-tso-head-demand-only-20260923/final-off/summary.md)、
[关闭态逐值对照](../tmp/buffer-tso-head-demand-only-20260923/off-equivalence.json)、
[CPI 汇总数据](../tmp/buffer-tso-head-demand-only-20260923/report.json)。
