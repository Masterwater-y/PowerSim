# Standalone Branch Predictor Replay 设计

## 1. 文档状态

- 状态：当前方案，待实现
- 适用范围：TCSim v29 及后续部署推理
- 核心目标：在不依赖 gem5 的部署环境中，仅使用当前 TCSim 训练数据合同中的
  predictor-independent functional trace，根据外部 branch predictor 配置重放方向预测、
  BTB、RAS 和 indirect predictor，输出逐分支 miss 与 ROI count/rate。
- 默认近似：只重放 committed functional path；不恢复 trace 中不存在的 wrong path。

## 2. 方案结论

Branch PMU 的默认部署方案从纯 neural head 或固定 gshare baseline，收敛为：

> **独立、配置驱动的 correct-path branch predictor emulator。**

它必须满足：

1. 部署时不安装、不链接、不调用 gem5；
2. 输入只包含当前训练数据合同允许的 functional trace 字段；
3. 不读取 `mispredicted`、BTB hit、预测方向、投机 GHR、squash、commit timing 等
   predictor/timing oracle；
4. predictor 类型、表大小、counter bits、BTB 容量/关联度、RAS 容量和 indirect
   参数直接构造 replay 状态，而不是作为 neural embedding；
5. 配置改变后重新 replay，不重新训练；
6. wrong path、真实流水线重叠和 ROI 前 predictor 状态无法恢复时，采用显式近似并报告
   能力边界，禁止使用未来事件或 oracle 补齐。

Neural branch head 保留为研究对照，不默认叠加到 replay count。没有跨 predictor
配置数据时，neural residual 会绑定已有配置，不能作为配置泛化的主方案。

## 3. 目标与非目标

### 3.1 目标

- 对 `TournamentBP` 和标准 `TAGE` 两种 conditional predictor，以及当前
  `SimpleBTB + ReturnAddrStack + SimpleIndirectPredictor` 实现独立 replay；
- 在同一 predictor family 内，修改任意已支持参数后无需代码修改或训练；
- 对每条 functional branch 输出方向、target provider、miss 原因和完整 miss；
- 对每个 core/trace 输出 branch count、miss count、miss rate；
- 保持逐事件因果性：事件 `i` 的预测只能使用 `i` 之前的 functional 事件和 replay
  内部状态；
- 用开发期 golden vectors 验证 standalone 实现与 gem5 组件语义一致，但部署不依赖
  gem5。

### 3.2 非目标

- 不从 committed trace 恢复真实 wrong-path instruction stream；
- 不精确复刻 fetch/decode/execute/commit 的重叠时序；
- 不使用 gem5 predictor state snapshot 作为部署输入；
- 不保证一个尚未实现的 predictor family 仅靠配置名称即可工作；
- 不用下一条 architectural macro PC 伪造微码 control-UOP target；
- 本阶段不适配 DR trace；输入先固定为当前 TCSim raw/aligned functional trace，DR
  适配如有需要另立输入合同；
- 首版 `TAGE` 指 gem5 标准 `TAGE + TAGEBase`。`LTAGE`、`TAGE_SC_L_8KB/64KB`
  和 Multiperspective Perceptron TAGE 具有额外 loop/statistical-corrector 结构，不得
  静默按标准 TAGE 处理，未实现时必须 hard fail。

## 4. 当前状态与主要缺口

当前 `tcsim/v29/inference.py::replay_branch_baseline` 是 4096-entry、direction-only
gshare：

- 使用 `(macro_pc >> 2) XOR history` 索引；
- 每个 core 从统一的弱 not-taken counter 状态启动；
- 每条退休分支后立即使用实际方向更新 counter/GHR；
- 不实现当前 gem5 的 Tournament local/global/choice；
- 不实现 TAGE 的 tagged history tables、provider/alternate provider、useful bits、
  allocation 和 history folding；
- 不实现 BTB、RAS、indirect target；
- 不读取精确 target；
- 只输出汇总 direction miss。

该 baseline 在当前报告中的 heldout branch count relative error 约为 13.5%，rate
absolute error 约为 0.84 pp，明显优于当前 neural branch head，但它不是 gem5 predictor
clone，也不能跟随真实 predictor 配置变化。

当前 raw/aligned functional schema 已含 exact `macro_pc`、`micro_pc`、branch 类型、
`branch_taken`、`branch_target` 和 `branch_next_pc`。v29 packed-3 训练 cache 只保留精确
`macro_pc`、branch mask 和桶化 branch feature，没有保留 exact target。因此 standalone
replay 应采用以下二者之一：

1. 直接消费当前 aligned functional trace；
2. dataset build 时生成 replay-only 数组，原样投影已有 functional 字段。

第二种方式不增加新的语义输入，也不把 exact target 暴露给 neural 模型，只是避免 replay
重复解析 parquet。

## 5. 输入合同

### 5.1 Replay 使用的 functional 字段

每条动态指令沿用当前训练数据的 functional schema。Replay 实际读取的最小投影为：

| 字段 | 用途 |
|---|---|
| `core_id` | 选择 per-core predictor 状态 |
| `thread_id` | 选择 per-thread history/RAS；单线程 trace 固定为 0 |
| `micro_seq` 或文件顺序 | 保证 committed functional order |
| `macro_pc` | predictor/BTB 主 PC key |
| `micro_pc` | 标记微码位置和审计粒度；当前 target 仅有 instAddr 时不能完全恢复 PCState |
| `is_branch` | branch opportunity |
| `is_branch_cond` | 是否查询 conditional predictor |
| `is_branch_indirect` | 是否查询 indirect predictor |
| `is_call` | RAS push 语义 |
| `is_return` | RAS pop 语义 |
| `branch_taken` | 实际 committed 方向，用于 resolve/commit |
| `branch_target` | taken 时的实际 committed target |
| `branch_next_pc` | 实际 committed successor |
| `branch_history` | 仅用于输入审计；主 replay 自己维护 history，不用它覆盖内部状态 |
| `is_microop` | 输出 branch 粒度说明与审计 |

`branch_target`、`branch_next_pc` 和实际方向属于程序功能路径，不依赖 predictor 配置，
允许进入 replay。完整 target 不作为 neural embedding。

### 5.2 严禁读取的字段

Replay 输入和状态更新不得读取：

- `mispredicted`；
- `commit_tick`、fetch/issue/complete tick；
- gem5 `predTaken`、predicted target、target provider；
- BTB/RAS/indirect hit/miss；
- speculative GHR、counter、provider confidence；
- squash/redirect/wrong-path event；
- 由上述 oracle 派生的 history 或 warm state。

这些字段只可在离线评估或 golden-vector 测试中作为 label/expected output。

## 6. Predictor 配置合同

### 6.1 配置来源

部署输入是独立的 `predictor_config.json`。在 gem5 数据验证环境中，它可以由
`uarch_profile.branch_predictor` 或 `config.ini` 规范化生成；在无 gem5 的部署环境中，
由用户或目标微架构配置直接提供。

配置不是模型 feature，而是 replay 构造参数。配置 hash 必须包含：

- replay schema/implementation version；
- predictor family 和全部支持参数；
- BTB/RAS/indirect 子组件配置；
- approximation policy；
- initial-state policy；
- trace schema version 和现有 retired control-UOP branch contract。

任何一项改变后都必须重新 replay。未知 predictor 类型、未知但可能影响语义的参数、非法
表大小或不支持的 indexing/replacement policy 必须 hard fail，禁止静默回退到 gshare。

### 6.2 当前 Tournament 配置示例

```json
{
  "schema": "standalone-bpred-replay-v1",
  "direction": {
    "type": "TournamentBP",
    "local_history_table_size": 2048,
    "local_predictor_size": 2048,
    "local_counter_bits": 2,
    "global_predictor_size": 8192,
    "global_counter_bits": 2,
    "choice_predictor_size": 8192,
    "choice_counter_bits": 2,
    "inst_shift_amt": 0
  },
  "btb": {
    "type": "SimpleBTB",
    "num_entries": 4096,
    "associativity": 1,
    "tag_bits": 16,
    "indexing": "set_associative",
    "replacement": "lru"
  },
  "ras": {
    "type": "ReturnAddrStack",
    "num_entries": 16
  },
  "indirect": {
    "type": "SimpleIndirectPredictor",
    "num_sets": 256,
    "num_ways": 2,
    "tag_bits": 16,
    "ghr_bits": 13,
    "path_length": 3,
    "speculative_path_length": 256,
    "hash_ghr": true,
    "hash_targets": true
  },
  "root": {
    "num_threads": 1,
    "inst_shift_amt": 0,
    "requires_btb_hit": false,
    "speculative_hist_update": true,
    "taken_only_history": false,
    "update_btb_at_squash": true
  },
  "approximation": {
    "path_mode": "committed_path_serial",
    "wrong_path": "ignored",
    "initial_state": "cold",
    "ras_unknown_return": "causal_learn"
  }
}
```

### 6.3 标准 TAGE 配置示例

当 `direction.type=TAGE` 时，`direction.tage` 直接构造标准 gem5 `TAGEBase`：

```json
{
  "direction": {
    "type": "TAGE",
    "inst_shift_amt": 0,
    "tage": {
      "num_threads": 1,
      "n_history_tables": 7,
      "min_history": 5,
      "max_history": 130,
      "tag_table_tag_widths": [0, 9, 9, 10, 10, 11, 11, 12],
      "log_tag_table_sizes": [13, 9, 9, 9, 9, 9, 9, 9],
      "log_ratio_bimodal_hysteresis_entries": 2,
      "tag_table_counter_bits": 3,
      "tag_table_u_bits": 2,
      "history_buffer_size": 2097152,
      "path_history_bits": 16,
      "log_u_reset_period": 18,
      "num_use_alt_on_na": 1,
      "initial_t_counter_value": 131072,
      "use_alt_on_na_bits": 4,
      "max_num_alloc": 1,
      "enabled_tables": [],
      "speculative_history_update": true,
      "taken_only_history": false
    },
    "allocation_rng": {
      "algorithm": "mt19937_64",
      "seed": 5489
    }
  }
}
```

`enabled_tables=[]` 表示全部 history tables 启用。`tag_table_tag_widths` 和
`log_tag_table_sizes` 的长度必须都是 `n_history_tables + 1`，第 0 项对应 untagged
bimodal base table，且第 0 个 tag width 必须为 0。还必须验证 `tag_table_u_bits` 为
gem5 标准 TAGE 支持的 1 或 2、`path_history_bits <= 32`、
`history_buffer_size > 3 * max_history`、`log_u_reset_period > 0`。

标准 TAGE 在错误预测后的 entry allocation 中使用随机数选择候选长历史表。Standalone
replay 必须使用配置中显式给出的 RNG 算法和 seed，并将二者计入 config hash。否则同一
functional trace 和相同表参数也不能保证确定性。该 RNG 只由 TAGE allocation 消费，不能
与其他组件共享隐式全局随机流。

### 6.4 参数必须改变实际状态结构

配置参数不能只记录在 metadata 中，必须直接控制 replay：

| 配置变化 | Replay 必须发生的变化 |
|---|---|
| BTB `num_entries` | 重新分配 entry/set，改变 index 和冲突 |
| BTB `associativity` | 改变 ways、victim 选择和 replacement state |
| BTB `tag_bits` | 改变 tag 截断和 false alias |
| Tournament table size | 改变 table 容量、mask 和 alias |
| Counter bits | 改变饱和值、threshold 和初始值 |
| History bits/table size | 改变 GHR/local-history 截断和索引 |
| TAGE history/table/tag 向量 | 改变几何 history、folded index/tag 和 table capacity |
| TAGE counter/useful bits | 改变 provider confidence、allocation 和 usefulness aging |
| TAGE alt/allocation 参数 | 改变 alternate-provider 选择、entry allocation 和 u reset |
| TAGE allocation RNG/seed | 改变可重复的 allocation table 选择 |
| RAS entries | 改变 stack 容量、overflow/underflow 行为 |
| Indirect sets/ways | 改变 target cache 容量和冲突 |
| Indirect hash/path 参数 | 改变 set/tag 和 path-history 演化 |
| `requires_btb_hit` | 改变 RAS/indirect 是否可被调用以及 branch detection |
| `update_btb_at_squash` | 改变 taken branch 的 BTB 更新阶段 |
| `speculative_hist_update` | 改变 history 的 predict/repair/commit 顺序 |

## 7. 独立实现架构

建议实现为无 gem5 依赖的 C++17 library/CLI，Python 仅提供配置生成和 TCSim 集成层：

```text
standalone/branch_replay/
  include/
    branch_event.hh
    predictor_config.hh
    direction_predictor.hh
    target_predictor.hh
    replay_engine.hh
  src/
    tournament_bp.cc
    tage_base.cc
    tage.cc
    simple_btb.cc
    ras.cc
    simple_indirect.cc
    replay_engine.cc
    trace_reader.cc
    main.cc
  tests/
    unit/
    golden/
```

部署接口示例：

```bash
bpred-replay \
  --trace functional.aligned.parquet \
  --config predictor_config.json \
  --output branch_replay.json
```

也可输出逐事件紧凑数组供 TCSim rollout 使用：

```text
branch_replay_miss.npy
branch_replay_reason.npy
branch_replay_provider.npy
```

实现可以参考相同版本 gem5 的语义和单元测试，但最终二进制不得 import、dlopen 或链接
gem5。若直接复用 gem5 源码片段，必须保留其许可证声明。

### 7.1 核心接口

```cpp
struct BranchEvent {
    uint32_t core_id;
    uint32_t thread_id;
    uint64_t sequence;
    uint64_t macro_pc;
    uint32_t micro_pc;
    bool conditional;
    bool indirect;
    bool call;
    bool ret;
    bool actual_taken;
    uint64_t actual_target;
    uint64_t actual_next_pc;
};

class DirectionPredictor {
  public:
    virtual DirectionPrediction predict(const BranchEvent&) = 0;
    virtual void repair(const BranchEvent&, bool actual_taken) = 0;
    virtual void commit(const BranchEvent&, bool actual_taken) = 0;
};

class TargetPredictor {
  public:
    virtual TargetPrediction lookup(const BranchEvent&) = 0;
    virtual void repair(const BranchEvent&) = 0;
    virtual void commit(const BranchEvent&) = 0;
};
```

`TournamentBP`、`TAGE/TAGEBase`、`SimpleBTB`、RAS 和
`SimpleIndirectPredictor` 分别实现独立组件，`CorrectPathBPredUnit` 负责 gem5 相同的
provider 顺序和阶段调用。Direction predictor factory 根据 `direction.type` 实例化
Tournament 或 TAGE；两者不得共享不符合各自算法的简化 history state。

## 8. Correct-path replay 语义

### 8.1 预测顺序

对每条 `is_branch=1` 的 committed functional event：

1. unconditional branch 的初始方向为 taken；conditional branch 查询方向预测器；
2. 总是执行 BTB lookup；
3. 根据 `requires_btb_hit` 决定 branch 是否已被 target side 检测；
4. call/return 按配置执行 RAS push/pop；
5. taken indirect non-return 查询 indirect predictor；
6. target provider 优先级按当前 gem5 语义处理；
7. 没有 target provider 时预测 fallthrough，并将最终 predicted-taken 视为 false；
8. 按配置执行 speculative history/path update；
9. 比较最终预测和 actual functional outcome；
10. miss 时 repair 当前分支状态；随后 commit 当前分支。

默认输出：

\[
miss_i = \mathbf{1}(predicted\ outcome/target_i \ne actual\ outcome/target_i)
\]

具体比较规则：

- 最终 predicted-taken 与 `branch_taken` 不同：direction/full miss；
- 二者均 taken 且 predicted target 与 `branch_target` 不同：target/full miss；
- 二者均 not-taken：按当前 functional 口径视为正确，不要求 trace 提供静态 fallthrough；
- predicted target 不可用且实际 taken：target/full miss。

### 8.2 Repair 与 commit

默认 `committed_path_serial` 表示每条 branch 在下一条 functional branch 到来前完成
resolve 和 commit，但仍保留当前 branch 自身的投机更新语义：

```text
predict
  -> optional speculative history/path update
  -> compare with actual
  -> if miss: restore/repair history and target state
  -> apply updateBTBAtSquash when configured
  -> commit direction counters, RAS and indirect state
  -> if BTB is commit-updated: update BTB
```

这种模式等价于“同一时刻最多一个未决 branch”的 predictor emulator。它有意忽略多个
未决分支导致的 counter update latency，但最大化因果确定性和跨配置稳定性。

### 8.3 Wrong-path 策略

默认：

```text
wrong_path = ignored
```

不生成或猜测错误路径指令，不模拟其 BTB/indirect 污染。仍对当前误预测 branch 执行配置
规定的 history repair、RAS repair 和 BTB-at-squash 更新。

预期保留较好的部分：

- direction table 的长期训练主要来自 committed branch；
- global/local speculative history 在 miss 时能对当前 branch 修复；
- BTB 容量、关联度、tag、replacement 和正确路径 alias；
- RAS 容量和正确路径调用深度；
- indirect target 的正确路径重用与冲突。

不可恢复的部分：

- wrong-path branch 的持久 BTB/indirect 污染；
- 真实多个未决 branch 的 history/counter 可见时序；
- decode early resteer；
- fetch width、ROB、执行延迟导致的投机深度变化。

这些是允许的近似误差，不应由默认 neural residual 隐式修正。

## 9. 各组件实现要求

### 9.1 TournamentBP

必须复现：

- local-history PC index；
- local-history 到 local-counter index 的映射；
- global-history mask；
- choice table 对 local/global 的选择方向；
- counter threshold、饱和更新和初始值；
- unconditional branch 对 global history 的影响；
- conditional branch 对 local history 的影响；
- speculative update、mispredict restore/repair 和 commit counter update 顺序；
- `inst_shift_amt` 和 per-thread history。

禁止继续使用固定 `(PC >> 2) XOR GHR` gshare 代替 TournamentBP。

### 9.2 TAGE/TAGEBase

首版 TAGE 范围是 gem5 标准 `TAGE` wrapper 和 `TAGEBase`，必须复现：

- bimodal base prediction bit 和共享 hysteresis bit；
- `min_history`、`max_history`、`n_history_tables` 生成的几何 history lengths；
- 每个 tagged table 的独立 size、tag width、prediction counter 和 useful counter；
- global history buffer、path history，以及 index/tag 所需的 folded histories；
- PC、folded global history 和 path history 的 index hash；
- PC 与两组 folded history 的 partial-tag hash；
- longest matching provider 和 alternate matching provider 搜索；
- pseudo-newly-allocated 判断以及 `useAltOnNa` counter；
- provider/alternate provider 的选择和更新；
- mispredict 时只在 provider 更长的 history tables 中 allocation；
- `max_num_alloc`、useful-bit 限制、候选 table 随机选择；
- `tCounter` 和 periodic useful-bit reset；
- speculative history record/restore/repair；
- `taken_only_history` 模式下由 taken branch PC/target hash 注入两位 history；
- unconditional branch、per-thread history 和 `inst_shift_amt`；
- gem5 相同的 counter 初值、符号、threshold、饱和边界和更新顺序。

Trace 中现有 16-bit `branch_history` 不能满足 TAGE 的 130-bit 或更长 history，但这不构成
输入缺口：Replay 必须从 trace 起点开始，按所有之前的 functional branch event 自己维护
完整 global/path history；`branch_history` 只用于前 16 位一致性审计，不能作为 TAGE
history 的来源或长度上限。

`committed_path_serial` 不存在更年轻的在途 branch，但当前 branch 仍要执行 TAGE 的
predict-time history update；mispredict 时恢复 predict 前 history，再用 actual direction/target
repair，最后更新 provider counters、useful bits 和 allocation state。不能因为 serial 模式
就把 TAGE 简化为“用实际结果直接移入 GHR 后再查表”。

TAGE allocation 的随机选择必须是确定、可配置、可审计的。Golden test 除了检查最终
miss，还必须逐 branch 检查 provider bank、alternate bank、table index/tag、allocation
bank、counter/u-bit 和 folded-history state。

以下 family 不属于标准 TAGE 首版：

- `LTAGE`：额外包含 loop predictor；
- `TAGE_SC_L_8KB/64KB`：额外包含特化 TAGE、loop predictor 和 statistical corrector；
- `MultiperspectivePerceptronTAGE*`：额外包含 multiperspective perceptron 和 SC。

配置出现上述类型但插件尚未实现时必须 hard fail，不能只运行其内部 TAGE 子结构后仍把
结果标记为完整 predictor replay。

### 9.3 SimpleBTB

必须复现：

- `num_entries / associativity` 个 set；
- PC shift、set index 和 tag 提取；
- thread key；
- configurable tag bits；
- hit 时 replacement-state touch；
- miss victim 选择和 insert；
- `requires_btb_hit`；
- `update_btb_at_squash` 与 commit-update 路径；
- return/indirect 在 `requires_btb_hit=false` 下的当前 gem5 安装规则。

首版只支持明确实现并验证过的 set-associative + LRU。其他 indexing/replacement
类型必须 hard fail。

### 9.4 RAS

当前 trace 没有 taken call 的静态 fallthrough/return PC。为了保持输入合同不扩展且不使用
未来信息，默认采用因果近似：

1. call 时若当前 `call_pc -> return_pc` 映射已由历史 return 学到，则 push 已知 return；
2. 首次调用或映射未知时 push `unknown`；
3. return 时 pop；top 为 exact target 才算 RAS target hit；top 为 `unknown` 或错误 target
   均算 target miss；
4. return resolve 后，用实际 `branch_target` 更新对应 call frame/call-PC 的映射，供未来
   动态调用使用；
5. stack 容量、overflow、underflow 必须服从 `num_entries`。

禁止预读未来 return target 后回填早先 call；那会人为提高 replay 精度并违反部署因果性。

该近似会在首次动态调用、递归、多调用点共享 return 行为和异常控制流上产生额外误差，
但 RAS 容量变化仍会反映为真实 stack 行为变化。

### 9.5 SimpleIndirectPredictor

必须复现已支持配置下的：

- set/way/tag；
- GHR bits；
- branch PC、GHR 和 target-path hash；
- path length 和 speculative-path buffer；
- lookup、repair、commit、replacement 顺序；
- return 不进入 indirect predictor 的 provider 规则。

实际 target 可在当前 branch resolve 后用于训练，不能在该 branch lookup 前使用。因此首次
出现的 indirect target 应自然产生 cold miss，后续行为由历史 functional targets 和配置
决定。

## 10. 初始状态与 warmup

默认：

```text
initial_state = cold
```

所有 table、counter、BTB、RAS 和 indirect state 按 standalone 配置中复现的 gem5 初始
规则构造。Replay 不读取 gem5 ROI 起点 predictor snapshot，因为 snapshot 与具体配置绑定，
换 BTB/表大小后不能复用。

若 functional trace 包含 ROI 前 predictor-independent branch prefix，可以先 replay warmup
但不计入 ROI 输出。这仍满足 functional-only 合同。若只有 ROI trace，则接受冷启动误差，
并至少同时报告：

- 全 trace error；
- 跳过最初 1K branches 后的 error；
- 跳过最初 10K branches 后的 error。

该切片仅用于分析冷启动误差，正式 ROI count 不能丢弃前缀 branch。

## 11. 输出合同

### 11.1 Per-branch 输出

```json
{
  "sequence": 123,
  "core_id": 0,
  "thread_id": 0,
  "pc": 4198400,
  "actual_taken": true,
  "predicted_taken": true,
  "direction_provider": "TAGE_LONGEST:bank=5",
  "actual_target": 4202496,
  "predicted_target": 4202496,
  "target_provider": "BTB",
  "direction_miss": false,
  "target_miss": false,
  "full_miss": false,
  "reason": "correct"
}
```

`predicted_*` 是 standalone replay 自己产生的结果，不是输入 oracle。

建议 reason 枚举：

```text
correct
direction_wrong
btb_miss
btb_wrong_target
ras_empty
ras_unknown
ras_wrong_target
indirect_miss
indirect_wrong_target
no_target_provider
unsupported_pcstate_detail
```

### 11.2 Aggregate 输出

每个 core 和全 trace 输出：

```text
branch_opportunities
predicted_branch_misses
predicted_branch_miss_rate
direction_misses
direction_provider_counts
target_misses
btb_misses
ras_misses
indirect_misses
predictor_config_hash
replay_version
trace_branch_granularity
approximation_flags
```

Branch 分母由 functional trace 精确统计：

\[
B=\sum_i \mathbf{1}(is\_branch_i),\qquad
M=\sum_i full\_miss_i,\qquad
R=M/B
\]

同一 trace 上 count relative error 与 rate relative error共享同一分母，应统一报告一个相对
误差；同时保留 miss-rate absolute percentage-point error。

## 12. 配置泛化边界

| 变化 | 预期能力 | 条件/限制 |
|---|---|---|
| BTB entries/assoc/tag | 强 | indexing/replacement 已实现；exact functional target 可用 |
| Tournament sizes/counter bits | 强 | 算法与初始化逐项对齐 |
| 标准 TAGE history/tables/tags | 强 | TAGE/TAGEBase、folded history 和 RNG 已实现并验证 |
| 标准 TAGE allocation/RNG seed | 强 | seed 显式配置且使用独立确定性随机流 |
| RAS entries | 中强 | 容量行为准确，首次 return target 受当前 trace 字段限制 |
| Indirect sets/ways/hash/path | 中强 | correct-path target 可用，缺 wrong-path pollution |
| root predictor flags | 强 | 状态机实现对应分支 |
| 同 family 新参数组合 | 强 | 参数合法且没有未支持字段 |
| 新 predictor family | 不自动支持 | 必须新增插件与 golden tests |
| CPU pipeline timing 变化 | 近似 | serial replay 不模拟真实重叠 |
| wrong-path 深度变化 | 近似 | trace 中不可辨识，默认忽略 |

这里的“强泛化”指机制泛化：配置直接改变 predictor state machine，不依赖训练分布。它不
表示 functional-only replay 能精确恢复未观测的动态投机事件。

## 13. 验证方案

### 13.1 组件单元测试

- 饱和 counter 全状态转移；
- Tournament local/global/choice 选择和更新；
- TAGE 几何 history length、folded index/tag 和 bimodal base；
- TAGE provider/alternate-provider、useAltOnNa、allocation 和 useful-bit reset；
- TAGE speculative history restore/repair 和确定性 RNG；
- history mask、restore、repair；
- BTB set/tag/way、LRU victim 和 false alias；
- RAS overflow/underflow/unknown target；
- indirect hash、lookup、replacement 和 path repair；
- 配置非法值和 unsupported policy hard fail。

### 13.2 开发期 gem5 golden vectors

部署不依赖 gem5，但开发阶段可以使用当前 gem5 对固定 synthetic branch stream 导出一次性
golden vectors：

```text
predicted direction/target
provider
GHR/local history
direction counter index/value
TAGE provider/alternate bank/index/tag/counter/u/allocation
BTB set/tag/hit/victim
RAS depth/top
indirect set/tag/hit
full miss
```

Standalone 实现必须逐 branch 比对。Golden 文件随测试代码保存；部署二进制不 import、
链接或执行 gem5。

需要注意：serial correct-path standalone 与完整 O3 gem5 的 wrong-path/timing 本来就不同。
Golden 测试应分为：

1. **组件语义 golden**：输入同一确定事件序列，要求状态逐项相同；
2. **完整 O3 统计评估**：允许近似误差，评估 count/rate。

### 13.3 跨配置矩阵

同一批 functional traces 至少测试：

| 组件 | 配置矩阵 |
|---|---|
| BTB entries | 1K / 2K / 4K / 8K |
| BTB associativity | 1 / 2 / 4 |
| BTB tag bits | 12 / 16 / 24 |
| RAS entries | 8 / 16 / 32 |
| Tournament local | 1K / 2K / 4K |
| Tournament global/choice | 4K / 8K / 16K |
| Counter bits | 2 / 3（gem5 配置合法时） |
| TAGE history tables | 4 / 7 / 10 |
| TAGE min/max history | 4–64 / 5–130 / 8–256 |
| TAGE tagged table size | 每表 256 / 512 / 1K entries |
| TAGE tag width | 7–10 / 9–12 / 10–14 bits |
| TAGE allocation seed | 至少 3 个固定 seed |
| Indirect sets/ways | 128×2 / 256×2 / 256×4 |

每个配置重新采 gem5 label **仅用于验证**，standalone replay 不训练。验收同时看：

- trace-equal mean branch count relative error；
- heldout workload mean；
- miss-rate absolute pp；
- pooled signed bias；
- per-component miss reason；
- 配置改变时 replay 与 gem5 的 miss 增减方向是否一致；
- 冷启动前缀与 steady-state 切片。

不能只看 global-pooled error；大 trace 的正负抵消会掩盖 workload 泛化失败。

## 14. 分阶段实施

### P0：输入与配置合同

1. 固化 standalone config schema；
2. 从当前 predictor profile 生成规范化 JSON；
3. 增加 functional aligned trace reader；
4. 增加 predictor/replay/trace hash；
5. 审计任何 oracle 字段均未被读取。

### P1：方向预测

1. 建立 configurable direction-predictor factory；
2. 实现 exact configurable TournamentBP；
3. 输出逐 branch direction prediction；
4. 以 gem5 component golden vectors 验证；
5. 保留旧 gshare 只作为 baseline，不再作为默认 replay。

### P2：标准 TAGE

1. 实现 configurable TAGE/TAGEBase；
2. 实现 bimodal、tagged tables、folded histories 和 provider/alternate provider；
3. 实现 useful bits、allocation、periodic reset 和独立确定性 RNG；
4. 实现 speculative history record/restore/repair；
5. 对默认参数和参数矩阵执行逐事件 golden 验证。

### P3：BTB 与完整 direct target

1. 实现 configurable SimpleBTB；
2. 实现 provider/fallthrough 规则；
3. 支持 BTB-at-squash/commit；
4. 输出 BTB miss/wrong-target/full miss。

### P4：RAS 与 indirect

1. 实现 configurable RAS 和 causal unknown-return policy；
2. 实现 SimpleIndirectPredictor；
3. 输出分组件 target miss；
4. 完成 Tournament 和标准 TAGE 两种 direction family 的完整 correct-path replay。

### P5：跨配置验收与部署集成

1. 跑 BTB/RAS/Tournament/TAGE/indirect 配置矩阵；
2. 固化两种 direction family 的误差边界；
3. 接入 v29 deployment aggregate；
4. 默认 branch PMU 使用 standalone replay；
5. neural branch head 只作为对照输出。

### P6：可选近似增强

只有 serial replay 的配置矩阵验收完成后，才评估：

- correct-path counter commit delay；
- 有限 unresolved-branch queue；
- 基于 functional UOP distance 的 overlap 近似。

这些模式必须使用新的 approximation-policy hash，并始终保留 serial baseline。禁止为了拟合
单一配置而加入隐式常数或 workload-specific correction。

## 15. 验收标准

首版完成需同时满足：

1. 部署二进制在没有 gem5 的环境中运行；
2. 运行时只读取 functional schema 和 predictor JSON；
3. Tournament、标准 TAGE、BTB、RAS 和 indirect 配置全部由参数构造；
4. 配置变化导致 state capacity/index/replacement 实际变化；
5. unsupported family/parameter hard fail；
6. 组件 golden test 逐事件通过；
7. 同一 functional trace 可对多个 predictor 配置直接 replay；
8. 输出逐 branch 原因、per-core count/rate、配置 hash 和近似标记；
9. 报告 trace-equal、heldout、absolute pp 和 signed bias，不只报告 pooled 结果；
10. 输入严格限定为当前 TCSim functional trace contract；本阶段没有 DR adapter 或
    DR-specific fallback。

## 16. 相关实现与文档

- 当前 direction-only baseline：`tcsim/v29/inference.py::replay_branch_baseline`
- v29 branch token/count loss：`tcsim/v29/losses.py`
- 当前 functional/oracle 字段边界：`tcsim/v29/builder.py`
- predictor 配置规范化：`tcsim/chunker/functional_features.py`
- functional branch 合同：`docs/v28_1_functional_feature_and_trace_contract.md`
- v29 branch 设计：`docs/v29_global_time_prefix_progress_design.md`
- 当前 v29 评估：`docs/v29_packed3_checkpoint_evaluation_report.md`
- 参考 gem5 predictor 参数：`gem5/src/cpu/pred/BranchPredictor.py`
- 参考 gem5 BPredUnit 状态机：`gem5/src/cpu/pred/bpred_unit.cc`
- 参考 gem5 标准 TAGE wrapper：`gem5/src/cpu/pred/tage.cc`
- 参考 gem5 TAGEBase：`gem5/src/cpu/pred/tage_base.cc`
