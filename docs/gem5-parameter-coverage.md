# gem5 microarchitecture parameter coverage

This document maps the parameters of the actual gem5 x86 `BaseO3CPU` +
`MESI_Three_Level` baseline to FastSim. “Active” means changing the FastSim
key changes modeled timing or state; it does not imply cycle-by-cycle gem5
equivalence. The governing rule is identifiability from a functional trace:
FastSim exposes a parameter only when the trace can drive a defensible model
for it.

> **Source-audit status (2026-08-02):** this page records configuration/API
> coverage, not semantic equivalence. A source-level audit found unresolved
> mismatches in SQ/TSO drain, transient Ruby state, and response-driven ROB
> feedback. The first implementation checkpoint has separated Sequencer and
> TBE capacity, aligned x86 walker concurrency/coalescing, and added a
> response-driven memory-IQ layer; none of those changes alone establishes
> full gem5 equivalence. Accuracy claims and the correction plan are superseded by
> [the gem5 source-aligned P99 plan](gem5-source-aligned-p99-plan.md).

## O3 pipeline and queues

| gem5 parameter | FastSim key | Status and semantics |
|---|---|---|
| `fetchWidth` | `core.fetch_width` | Active interval-stage bandwidth |
| `decodeWidth` | `core.decode_width` | Active interval-stage bandwidth |
| `renameWidth` | `core.rename_width` | Active interval-stage bandwidth |
| `dispatchWidth` | `core.dispatch_width` | Active interval-stage bandwidth |
| `issueWidth` | `core.issue_width` | Active global issue bandwidth |
| `wbWidth` | `core.writeback_width` | Active completion/writeback bandwidth |
| `commitWidth` | `core.commit_width` | Active in-order retirement bandwidth |
| `fetchQueueSize` | `core.fetch_queue_entries` | Active bound on UOPs ahead of dispatch |
| `numROBEntries` | `core.rob_entries` | Active; dispatch waits for retirement |
| `instQueues[*].numEntries` | `core.iq_entries`, `core.response_queue_feedback` | Base interval capacity is active. With feedback enabled, non-memory UOPs release at issue, regular stores at core completion, and loads/atomics at response; this is a checkpoint-level occupancy calendar, not yet a full event-driven IQ/ROB replay |
| `LQEntries`, `SQEntries` | `core.lq_entries`, `core.sq_entries` | Capacities are active, but SQ lifetime/TSO drain is not aligned: gem5 retains committed stores until memory completion |
| `iewToRenameDelay`, `commitToRenameDelay` | `core.iew_to_rename`, `core.commit_to_rename` | Implemented source-alignment ablation, default off; the 92-case gate showed that adding these delays on top of interval occupancy double-counts stalls at low core counts |
| `fetchToDecodeDelay` | `core.fetch_to_decode` | Active fixed stage delay |
| `decodeToRenameDelay` | `core.decode_to_rename` | Active fixed stage delay |
| `renameToIEWDelay` | `core.rename_to_dispatch` | Active fixed stage delay |
| IEW dispatch-to-issue bound | `core.dispatch_to_issue` | Active fixed lower bound |
| `issueToExecuteDelay` | `core.issue_to_execute` | Active optional extra delay; baseline 0 because `opLat` already defines producer-ready time |
| `iewToCommitDelay` | `core.execute_to_commit` | Active optional retirement delay; baseline 0 in the interval abstraction |
| `cacheLoadPorts`, `cacheStorePorts` | `core.cache_load_ports`, `core.cache_store_ports` | Active per-cycle memory-port limits |

The two backward free-entry delays are available only as default-off
experiments; the broader `backComSize`, `forwardComSize`, `squashWidth`,
trap/interrupt entry, and decoupled front-end/FTQ controls are not modeled.
`fetchBufferSize` has a functional-PC block model; explicit nonblocking
syscalls now have drain/system-FU/service/restart timing, but blocking and
kernel execution do not. A retired functional trace omits wrong-path fetch
traffic, so several remaining controls cannot be reconstructed exactly.

## Functional units

gem5's `FUPool` makes FU count, operation latency, and pipelining configurable
per operation class. FastSim actively models the same three dimensions, but
groups operation classes that share a gem5 FU pool:

| FU property | FastSim keys |
|---|---|
| Unit counts | `core.integer_alu_units`, `core.integer_multiply_units`, `core.float_simple_units`, `core.float_complex_units`, `core.simd_units`, `core.predicate_units`, `core.memory_units`, `core.system_units` |
| Integer latency | `core.integer_alu_latency`, `core.integer_multiply_latency`, `core.integer_divide_latency` |
| FP latency | `core.float_simple_latency`, `core.float_multiply_latency`, `core.float_multiply_accumulate_latency`, `core.float_misc_latency`, `core.float_divide_latency`, `core.float_sqrt_latency` |
| Other latency | `core.simd_latency`, `core.predicate_latency`, `core.system_latency` |
| Pipelining | corresponding `*_pipelined` keys for integer and FP pools |

The operation-class and producer-distance fields are functional inputs. gem5
issue/complete/commit ticks are forbidden timing oracles.

## Rename, memory dependence, and SMT

gem5 also exposes separate integer/FP/vector/predicate physical-register
counts, SSIT/LFST store-set geometry, dependency-check policy, TSO behavior,
and SMT sharing policies. FST v6 now carries per-UOP Int/Float/Vec/CC
architectural destination counts. Two explicit experiments consume them:

- `core.rename_free_list` is a lower-bound-only per-class free list;
- `core.response_rename_feedback` is an alternative C2 model whose releases
  follow response-corrected ordered retirement.

Both remain disabled in production until the directed business matrix passes;
they are mutually exclusive to prevent double allocation. The remaining
unsupported state is:

- it does not report speculative loads that were squashed and replayed after
  a memory-order violation;
- the current input contract is one committed stream per core, not multiple
  SMT threads sharing one O3 backend.

Adding those knobs without the missing functional facts would create
configuration syntax with no identifiable timing meaning.

## Branch prediction

FastSim actively supports the baseline Tournament or gshare table geometry,
counter widths, PC shift, BTB entries/associativity/tag/index shift, RAS
entries, simple indirect predictor geometry/hash controls, BTB-hit policy,
BTB squash-update policy, and recovery penalty under the `branch.*` keys.
Committed direction and successor PC come from the trace; predictor outcomes
are replayed from FastSim state.

Other gem5 predictors such as TAGE, TAGE-SC-L, perceptron, and loop predictors
are not implemented. Selecting one requires its actual state machine, not a
generic accuracy scalar.

## Address translation

| gem5 parameter/state | FastSim key | Status |
|---|---|---|
| x86 DTLB `size` | `dtlb.entries` | Active, fully-associative LRU |
| TLB lookup latency | `dtlb.hit_latency` | Active |
| walker service | `dtlb.page_walk_latency` | Active fixed functional approximation; page-table memory requests are absent |
| concurrent walks | `dtlb.page_walkers` | Active; the captured x86 baseline uses one active timing walk |
| same-page in-flight behavior | `dtlb.coalesce_misses` | Active; disabled for the captured x86 baseline because its timing walker queues followers and does not implement coalescing |

The actual gem5 x86 TLB source selects the LRU victim across all entries; it
does not expose an associativity parameter. The older `uarch_profile.json`
`dtlb.assoc=8` describes TaoTrace's auxiliary view, not the simulated gem5
x86 TLB, and is therefore not copied into FastSim.

FastSim v6 records keep an opaque virtual-page token beside the physical data
address. The token drives DTLB state; the physical address drives every cache,
coherence, CHA, and DRAM decision. Page-table memory addresses are absent, so
walks use explicit service time/concurrency rather than fabricated cache
accesses. ITLB and instruction-cache timing remain unsupported.

## Syscalls in gem5 SE

| Functional fact / target cost | FastSim representation | Status |
|---|---|---|
| committed syscall identity | JSONL `is_syscall`; v5 reserved op-class marker | Active |
| older OoO work drained | serialize-before edge to prior ordered retirement | Active |
| syscall instruction execution | `core.system_latency` and `core.system_units` | Active |
| additional known SE service delay | `syscall.service_latency` | Active, default 0 |
| frontend restart | `syscall.restart_latency` | Active, default 1 |
| syscall number/arguments, blocking and wakeup | planned functional event sidecar | Not yet active |
| time slices, migration, context-switch cost | planned scheduler layer | Not yet active |

The default does not calibrate a fixed Linux syscall penalty: gem5 SE executes
no guest-kernel instruction stream, and a committed functional trace cannot
identify kernel cache/TLB traffic. A syscall record therefore contributes
only the observable serializing system-UOP path unless the baseline supplies
an explicit simulated service delay. Blocking time must later be represented
as a thread state transition anchored to functional order, not as extra core
cycles charged to every syscall.

`dtlb.misses` counts allocated FastSim walks and `dtlb.merged_misses` counts
younger accesses merged only when `dtlb.coalesce_misses=true`. The captured
profile keeps it false. DTLB **access** PMU is
diagnostic for a committed functional trace: gem5 also translates wrong-path loads. For
example, the captured C4 `int_div_serial` core 0 dispatched 33,106 loads and
squashed 16,712 of them, while the committed trace contains only 16,388 loads.
Those speculative virtual addresses cannot be reconstructed from the retired
stream, so FastSim neither invents them nor tunes an access multiplier.

## Cache, coherence, interconnect, and memory

| gem5/Ruby dimension | FastSim key | Status |
|---|---|---|
| Cache size/associativity/line size | `cache.{l1d,l2,llc}.{size,associativity,line_size}` | Active tag/replacement state |
| Replacement policy | `cache.*.replacement` | Active LRU or TreePLRU |
| Combined hit latency | `cache.*.hit_latency` | Active timing approximation |
| Ruby Sequencer outstanding | `ruby.sequencer_max_outstanding` | Active per-core response-lifetime calendar; captured value 16 |
| controller `number_of_TBEs` | `cache.{l1d,l2,llc}.mshrs` | Active TBE-like miss calendars; captured value 256, with LLC capacity partitioned per CHA/controller |
| LLC inclusion | `cache.llc.inclusive` | Active invalidation behavior |
| Coherence | `uncore.coherence` | Active directory/MESI approximation |
| L3 banks/home slices | `uncore.cha_count`, `uncore.cha_xor_hash` | Active mapping |
| Network/service delay | `uncore.noc_one_way_latency`, `uncore.llc_service_cycles` | Active |
| DRAM capacity/topology | `dram.size`, `dram.channels`, `dram.ranks_per_channel`, `dram.banks_per_channel`, `dram.bank_groups_per_rank`, `dram.row_bytes` | Active |
| DRAM command timing | `dram.t_cl`, `dram.t_rcd`, `dram.t_rp`, `dram.t_ras`, `dram.t_rtp`, `dram.t_rrd`, `dram.t_rrd_l`, `dram.t_xaw`, `dram.activation_limit`, `dram.t_ccd_l`, `dram.t_cs`, `dram.burst_cycles` | Active compact bank/rank/channel calendars; zero disables optional source-derived constraints |
| Controller queue and selection | `dram.read_buffer_size`, `dram.frfcfs_selection_window`, `dram.frfcfs_topology_scaled_window`, `dram.frfcfs_passes`, `dram.frfcfs_arrival_bucket_cycles` | Active causal FR-FCFS repair; selection window is a FastSim ambiguity bound, not a gem5 parameter |
| Controller dirty-write queue | `dram.separate_write_queue`, `dram.write_buffer_size`, `dram.write_high_threshold_percent`, `dram.write_low_threshold_percent`, `dram.min_reads_per_switch`, `dram.min_writes_per_switch` | Active by default; per-channel read priority and bounded write turns are validated. Direction-specific bus timing and a certified write-selection bound remain modeling limitations |
| Adaptive page policy | `dram.frfcfs_full_queue_page_policy`, `dram.frfcfs_row_cap_single_precharge`, `dram.max_accesses_per_row` | Source-alignment experiments implemented and unit-tested, but both corrections remain default-off after the isolated memory gate failed |

Ruby's separate tag/data-array latencies, controller transition bandwidth,
message-buffer capacities, virtual networks, detailed topology, refresh,
write-drain arbitration, and the complete DDR4 command protocol are not yet
reproduced. FastSim's current `mshrs` keys are
only a TBE-like capacity approximation, including an independent LLC pool per
CHA. The separate Sequencer calendar enforces the 16-request CPU-side limit,
but same-line aliasing, transient state, and request-merge rules remain absent.

## Baseline profile and acceptance rule

`configs/gem5-v28_1-time-epoch.cfg` records the effective default O3 values
from the captured gem5 `config.ini`: 8-wide pipeline stages, 192 ROB entries,
64 IQ entries, 32-entry LQ/SQ, the default FU pool, 64-entry x86 DTLB,
one active non-coalescing x86 walker, an independent 16-request Sequencer,
256 TBEs per captured Ruby controller, and the captured cache/DRAM hierarchy.
It enables checkpoint-level response feedback for memory-IQ lifetime; ROB,
LQ/SQ/TSO and transient Ruby closure are still incomplete.

The profile uses a conservative one-cycle fixed page-walk service time. This
is not a claim about gem5's real walk latency: the functional trace contains
no page-table memory addresses. Full-corpus ablations at 8 and 32 cycles made
C4/C8/C16/C32 mean CPI error worse at every core count, even though selected
PyTorch and high-core random-memory cases improved. The complete evidence is
in [the Stage 3 validation report](gem5-o3-dtlb-stage3-validation.md).

A newly exposed parameter is accepted only when:

1. changing it changes an explicit FastSim resource or state machine;
2. the required input is functional rather than a gem5 timing/path label;
3. a unit sensitivity test proves the direction of effect;
4. C4/C8/C16/C32 validation reports CPI, PMU, and throughput deltas.
