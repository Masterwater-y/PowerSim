# Store 链路接手结果（2026-10-29）

后续实施已完成实际 demand 观测及有证书的单次 FR-FCFS 路径，见 [Store demand 服务修复结果](store-demand-service-results-20261029.md)。下文保留首次接手时的历史状态；不作为后续实现的最新状态。

本轮接续 [实施交接](store-chain-implementation-handoff-20261029.md)。完成 S1 的功能邻接审计及 V4 的两个 gem5 机制用例，发现原 V1 比较的事件域不同，不能把比例差异直接解释为 NoAccess 扫描屏障错误。**V1 仍未验收；S2/S3、V3/V5/V6 尚未实施。** 本轮没有修改时序模型、没有新 CPI 或吞吐结果，没有 commit。

## 1. Marian 的矛盾：CPU demand 已发送，缓存仍可先看到年轻 prepare

使用采集六项来源时的同一个 frozen gem5：
`tmp/spec-speed-32g-c4-c8-c16-20261027-final/frozen/gem5.opt`。
新增 [冷流程序](../tests/workloads/store_chain_cold.S) 和
[可重复回归](../tests/test_gem5_store_chain.py)，通过 SE O3 + MESI_Three_Level 运行。
这只是机制反例，SE 参数未逐项复刻 FS 目标，不作为 FS 精度或性能验收。

冷流包含 256 条 MOVNTDQ，访问连续 4096 bytes，共 64 条冷 line。gem5 将每条展开为 `cda; stfp; stfp`。
第一条 line 的逐拍记录：

| tick | 层次 | 事件 |
|---:|---|---|
| 81000 | CPU SQ | CDA（sn=6）完成 |
| 81000 | CPU SQ | 第一条真实 store（sn=7，地址 0x2000）发送 WriteReq |
| 81500 | L0 | Store_Prepare，`I → SP_IE`，line 0x2000 |
| 81500 | L0 | Store，`SP_IE → IM`，同一 line |

第二条 stfp 尚未 committed，可在扫描中发 prepare。optional queue 的 rank=2，mandatory queue 的 rank=0，均有一拍入队边；生成的 controller wakeup 先检查 optional queue。于是年轻同 line 的 prepare 可以在**队头 demand 已发送之后**，于同一个 Ruby tick 先占据 I 状态，随后 demand 接管它。

源码链路：

- `gem5-fs/src/cpu/o3/lsq_unit.cc`：`writebackStores()` 在函数末调用 `issueStorePrepareRequests()`；跳过已 committed 的普通 store，但仍可为年轻同 line store 发 prepare。
- `gem5-fs/src/mem/ruby/system/Sequencer.cc`：prepare 直接 `enqueuePrefetch()`，demand 进入 mandatory queue。
- `gem5-fs/src/mem/ruby/protocol/MESI_Three_Level-L0cache.sm`：optional/mandatory queue 的优先级、入队边及 `I.Store_Prepare` / `SP_IE.Store` 转移。

观察到 64 次 `I.Store_Prepare`、64 次 `SP_IE.Store`，即缓存转移比值均为 1；这不能证明 64 个队头 demand 都曾被自己的 prepare 提前覆盖。空 SQ 单条 scalar store 对照则观察到 `I → IM`，没有冷 prepare 或 takeover 转移。回归检查了成功退出、冷 line 数、同 tick 同 line 转移及 CPU demand 先发出的次序。

因此原表的

$$\rho^{fs}=\frac{m_{TO}+m_{PUB}+m_{RE}}{\sum_r m_r},\qquad
\rho^{g5}=\frac{I.Store\_Prepare}{I.Store\_Prepare+I.Store}$$

分别统计 **CPU store 角色** 和 **cache line 的状态转移**。年轻同 line prepare 使两者无法直接一一对应；$\tau$ 也受相同影响。比例差异是真实观察，原先从差异推导“屏障错误地串行支付完整 miss”的因果结论尚不成立。这个反例不证明 FastSim 时序正确，也不解释 Marian 的全部误差。

## 2. 功能输入邻接审计

新增 [audit_store_chain.py](../tools/audit_store_chain.py)，按 manifest 的明确 warmup/take **record** 边界审计 `.mreq`，核对 header/core/人口、行序、测量期角色行的 hot-record hash、纯 store/load 资格。对 NoAccess 检查后两条是否同 PC、8-byte 真实 store、地址依次为 CDA−8/CDA；不将相同 PC 本身作为完整宏边界认证。

| case | 测量期 NoAccess | 满足后两条同 PC/address 关系 | 同 line | 测量尾截断 |
|---|---:|---:|---:|---:|
| XZ C4 | 294292 | 294292 | 294292 | 0 |
| XZ C8 | 292928 | 292928 | 292928 | 0 |
| Marian C4 | 783344 | 783344 | 783344 | 0 |
| Marian C8 | 1380606 | 1380605 | 1380605 | 1 |
| FLAC C4 | 84 | 84 | 84 | 0 |
| Graph500 C16 | 0 | 0 | 0 | 0 |

Marian 主要 PC `0xab096d/71/76/7b`、`0xab09d1/d6/dc/e2` 在 frozen workload 的反汇编中均为 MOVNTDQ，位于 `__memcpy_sse2_unaligned_erms`。没有发现所检查邻接关系中的错序证据。C8 最后一个 CDA 后的两个成员超出测量边界，保留为 boundary，不能算成功或自动扩窗。

工具明确输出 `full_stream_hash_verified=false`：这里只验证测量期稀疏行绑定，不重新计算整个 FST 的流 hash，不认证完整依赖或宏人口，也不消费 native cache 标签。它不能替代 native validator。结果位于 `tmp/store-chain-implementation-20261029/*-functional-audit.json`。

## 3. 当前门禁与验证

- `cmake --build build -- -j16` 通过；`LD_LIBRARY_PATH=/opt/gcc-11/lib64 ./build/fastsim_tests` 通过。
- 功能审计 6 项测试通过，覆盖 hash 损坏、缺 companion、测量尾、FST 冷 metadata 尾部及后继 NoAccess 排除；发送日志解析另有 1 项测试，排除发送失败和异 line。
- gem5 回归的 MOVNTDQ 冷流和空 SQ 单 store 两个子用例通过。收据包含 gem5/guest/source SHA256、命令和转移数。
- 维护的 `validate_fs_oracle_identity.py` 当前发现 10 项，10 项 `valid=true`，包含冻结六项；这是独立目标身份检查，不是精度、吞吐或硬件 PMU 认证。
- 本轮没有运行新的 native cache 评分，也没有重新发布交接中的 CPI 表；旧 S0 的精度与吞吐限制继续保留。

复现：

```bash
python3 tests/test_store_chain_audit.py
python3 tools/audit_store_chain.py \
  --manifest tmp/spec-speed-32g-c4-c8-c16-20261027-final/source/c04-872.marian_s/tao_trace/manifest.txt \
  --output tmp/store-chain-implementation-20261029/marian-functional-audit.json
GEM5_STORE_CHAIN_BINARY=tmp/spec-speed-32g-c4-c8-c16-20261027-final/frozen/gem5.opt \
GEM5_STORE_CHAIN_OUT=tmp/store-chain-implementation-20261029/regression \
LD_LIBRARY_PATH=/opt/gcc-11/lib64:/root/.pyenv/versions/3.11.14/lib \
  python3 tests/test_gem5_store_chain.py
```

## 4. 后续实施边界

依交接中的停止条件，保留 NoAccess 在 WB 队头完成、其后解除扫描屏障的语义。直接降低 `local_scan_ready_cycle` 会让后续 prepare 越过尚未完成的 CDA，目前没有逐拍证据支持这种修改。

V1 下一步需要同事件域观测：gem5 按 SQ entry 记录 canWB、prepare-issued、demand-send、response 与 local-complete，另将同 line 父事务关联记录为 cache 域；两侧以同一成员/line 人口分别比较。现有 frozen stats 不提供完整的逐 entry 配对数据，不能从 `I.Store_Prepare` 反推这些时间。先在微基准建立这种关联，再决定是否需要通用时序修复。XZ 的 Publish/Takeover、FLAC 相同 CPI 等问题仍待验证。

V1 通过后才能继续 S2/S3 的 SQ 视界、LRU 证书、foreign-write 独立开关，以及完整 V3–V6。当前工作没有把“发现旧指标不充分”改写成“V1 已通过”。

## 5. 独立审核与决策

独立 reviewer 的两项 Important 已经以失败测试复现并修复：后继成员必须在稀疏角色表中为普通 Data；sent-before-prepare 必须关联成功发送的同 line WriteReq。两项回归及 gem5 实跑重新通过。

保留的一项 Minor：64 个 prepare/takeover 转移已计数，但逐拍同 tick/同 line 关联只断言第一对；扩展全部配对留待后续。当前证据仅需一例即可反驳原比例的一一对应假设，不能据此宣称全部 64 对的逐拍关系都已自动认证。

本轮裁决：不删除 NoAccess 屏障；若判断错误，真实的扫描时序偏差会继续保留，需逐 entry 证据裁定。不把反例视为 V1 通过；代价是 S2/S3 与性能验收继续等待同事件域观测。审核未判定的模型正确性、CPI/PMU/吞吐、完整流身份及 SE 的 FS 适用性均维持未认证；跨过这些边界会导致错误验收。
