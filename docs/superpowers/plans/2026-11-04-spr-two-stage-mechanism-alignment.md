# SPR Two-stage Mechanism Alignment Plan

> 2026-11-04：用户已明确授权“先将方案整理到文档再实施，并最终验证效果”。现转入执行。
> 使用 subagent-driven-development，独立文件范围并行，核心时序集成统一协调；不恢复旧A3批次。

**Goal:** Cover active gem5-spr mechanisms within the retired-stream contract while preserving two-stage processing and measured high throughput.
**Architecture:** Parallel functional preparation produces immutable facts/dependencies/lower bounds. A persistent second-stage core continuation and causally admitted memory runtime commit target state once, with sparse events and certified bulk paths.
**Tech Stack:** C++17/current FastSim types and pools, existing IntervalCore/cache/DRAM, Python profile/audit tools, probe-only functional schema extensions if needed.
**Spec:** docs/spr-two-stage-mechanism-alignment-design-20261104.md

## Global constraints

- Parent fastsim-spr; child gem5-spr/yinhaolang. 保留旧路径；逐项实现、集成和验证，未完成项不得标为完成。
- No whole-epoch fixed point, seed/commit duplicate hierarchy replay or model-driven removal of input members.
- No native hit/source/ready/issue/response time, wrong-path reconstruction, workload-specific branches or fitted constants.
- All active resource domains have separate units and release events; use actual referenced objects, not unused template or DMA settings.
- Preserve current OFF path and old experiment profiles; reject incompatible combinations explicitly.
- Existing retirement-stop/task-active scope, full common-end population and independent elapsed remain unchanged.
- Suggested candidate throughput target is >=90% of same-source baseline user-UOP/s median, pending acceptance; this is not measured or preapproved.
- Current A3-Marian batch is absent/stopped. 不恢复旧批次。允许本轮构建、机制测试及带独立收据/墙钟限制的同源四案验证；新增采集仅在功能字段确实缺失时明确记录必要性。

## Review focus

1. Final arrivals can change cache/merge/FR-FCFS decisions: no side effects before a certified admission frontier.
2. Same-cycle/cross-chunk ordering: no invented time advance, forgotten callbacks, stale pointers or implicit epoch drain.
3. Shared store credit, request credit, optional hint, fill and TBE are different; include followers and completed-but-not-popped buffer entries.
4. Bounded repair has no general fixed-size causal cone; no successful partial score or silent approximate fallback on work-budget exhaustion.
5. Absent functional fusion/translation/synchronization facts must surface as capabilities, not inferred native timing.

## P0. Typed target profile and independent mechanism ledger

**Files proposed:** `include/fastsim/spr_mechanism_profile.hpp`, `tools/generate_spr_mechanism_profile.py`, profile plumbing in config/main, tests `test_spr_mechanism_profile.py` and `test_memory_population_ledger.cpp`.

**Interface:** consumes actual config.ini plus declared source capabilities; emits typed units, domain IDs, active predicates and support status. Reuse PendingOwner rather than another generic request ID convention.

- [ ] RED: active CPU list selects IQ128/72/48 rather than orphan IQ64; CPU Sequencer16 rather than DMA64; FTQ/ReadResp dormant with current flags.
- [ ] RED: original store, synthetic merged write, hint, follower, service and retired macro remain separate populations; dangling mappings fail closed.
- [ ] Implement typed import and capabilities (`modeled`, `approximate`, `unsupported`, `disabled_by_target`, `missing_functional_input`).
- [ ] GREEN with actual saved configuration; revise stale mapping messages about core0 and active-clock coverage.
- [ ] Save profile/binary/source identity and review; no model default changes.

## P1a. Persistent core continuation extraction

**Files proposed:** `include/fastsim/core_continuation.hpp`, `src/core_continuation.cpp`; adapt `src/interval_core.cpp`, `src/simulator.cpp`, `include/fastsim/pending_resources.hpp`; tests `test_core_continuation.cpp`.

**Interface:** `advance_until_blocked(limit) -> CorePublication`; `on_completion`, `on_credit_release`. Publication covers every potentially emitted source with phase-aware NotBefore(SafeKey) or BlockedOn(tokens, successor-key). FunctionalEarliestReady, ProvisionalSchedule and CertifiedReservation are distinct types.

- [ ] RED: response-delayed dependent advances after completion; independent ALU overlap survives; ROB/LQ/credit releases unblock only actual waiters.
- [ ] RED: paused producer and chunk boundaries do not invent earlier memory requests or force completion; incomplete publication cannot claim infinity.
- [ ] RED: a provisional FU competitor delayed by memory can free an earlier slot for an independent UOP; raw producer reservations cannot certify the frontier.
- [ ] Extract current second-stage feedback state; advance unresolved work rather than replay all previously scheduled work.
- [ ] GREEN on local lifecycle traces and actual OFF-path compatibility. Record work per UOP and maximum live continuation state.

## P1b. Safe admission and cache event commit

**Files proposed:** `include/fastsim/memory_runtime.hpp`, `src/memory_runtime.cpp`; extend cache probe/commit/completion APIs and simulator coordinator; tests `test_memory_runtime_frontier.cpp`, `test_cache_event_visibility.cpp`.

**Interface:** `try_admit(RequestCandidate, Cycle) -> AdmissionResult`, `handle(MemoryEvent)`, `safe_frontier() -> SafeKey`.

- [ ] RED: later known candidate cannot overtake an unpublished earlier source; closure applies to the actual arbitration phase, not all events of a cycle.
- [ ] RED: same-cycle credit/response releases a blocked continuation and permits its new same-cycle send in the legal successor phase; no artificial cycle increment or false deadlock.
- [ ] RED: finite zero-extra-cycle dependency chains progress by target causal-delta rules; closed phases cannot accept newly invented earlier events.
- [ ] RED: miss probe does not install, update replacement, train prefetch or create physical service; fill visibility occurs at its event only.
- [ ] RED: invalidation between proposal and admission rejects stale generation; old callback cannot authorize a new owner.
- [ ] Implement versioned proposals, typed queue acceptance, sparse callbacks and deterministic resource phases.
- [ ] GREEN first with structural probes, then the real SharedSystem adapter. Toy backend tests alone do not qualify this phase.
- [ ] Verify chunk/lookahead/host producer-delay invariance and no global hierarchy redo. If no safe progress, return diagnostic failure.

## P2. Native SPR store + online Sequencer

**Files proposed:** extend `store_request_engine.hpp/.cpp` with a separate native profile implementation, memory_runtime line/request ledgers, `types.hpp` counters; tests `test_spr_committed_store_buffer.cpp`, `test_online_sequencer.cpp`.

**Consumes:** P0 IDs/profile, P1 events and final readiness. **Produces:** ordinary/hint admission, per-core store FIFO, unique callbacks, credit releases and typed terminal populations.

- [ ] RED: new-line offload preserves SQ+buffer used; tail merge releases one shared credit; nonhead callback does not pop prefix.
- [ ] RED: four distinct lines block a fifth new line while another request on existing in-flight line remains eligible.
- [ ] RED: early prepare before data/retirement when target permits; cap16 drops optional hint; demand credit is unaffected by this drop.
- [ ] RED: ordinary follower still consumes request credit; upgrade S needs TBE but not data fill; I miss needs both; ignored/takeover/fill return hint credit at correct terminal event.
- [ ] RED: read-to-write reissue, permission invalidation, generation, cross-chunk response, NO_ACCESS, split/atomic and byte-overlap forwarding.
- [ ] Implement native state machine and online alias decisions at admission; retain historical engine under its explicit old profile.
- [ ] GREEN with emitted mechanism ledger and real backend. Remove seed replay and64-pass bridge only from new profile.
- [ ] Audit whether source byte masks/address-data/publication identity suffice; unsupported combinations refuse rather than become ordinary stores.

## P3. Partition IQ, target arbitration and load-bank acceptance

**Files:** `interval_core.hpp/.cpp`, continuation resource calendars, typed config; tests `test_spr_partitioned_iq.cpp`, `test_spr_port_arbitration.cpp`, `test_spr_load_banks.cpp`.

- [ ] RED: memoryIQ full cannot be hidden by free executionIQ; nonmemory issue vs memory complete release differ; fused groups charge/release correct token.
- [ ] RED: `(family,mask)` RR rotates only on success; generic FU ring and store-port pairing retain their own semantics.
- [ ] RED: third load to same bank/cycle blocks; failed send consumes no bank slot; split packet and excluded request kinds use correct rules.
- [ ] Implement fixed-domain calendars/small cursor tables, connect actual admission/release to continuation.
- [ ] GREEN and certify batched/free-resource spans against same-state-machine slow execution.

## P4. Frontend groups and core-local shared STLB

**Files:** `spr_frontend.hpp/.cpp`, `retired_frontend_facts.hpp`, new/versioned functional facts reader if required, MMU/TLB state in continuation; tests `test_spr_macro_fusion.cpp`, `test_spr_frontend_backpressure.cpp`, `test_spr_shared_stlb.cpp`.

- [ ] RED: fusion qualification does not consume native fused outcomes; adjacent/same-line/ASID/CPL/non-MS positive and negative cases; macro/UOP/token denominators stay distinct.
- [ ] RED: source batching and rename consumption have independent IDQ credits; backend block delays only actual consumption.
- [ ] RED: same-core I/D share STLB, different cores do not; hit completion/fill/flush generation and page-size domains are explicit.
- [ ] Implement bounded lookahead and reuse current frontend/walker calendars. Add source capability gate before decoder/sidecar extension.
- [ ] GREEN under chunk/source-batch variation. Do not enable dormant decoupled frontend or ReadResp throttle for this target.

## P5. Autonomous prefetch and protocol/DRAM service ordering

**Files:** new small prefetch state module, memory_runtime/cache/uncore/DRAM calendar adapters and profile mapping; tests `test_spr_ruby_prefetch.cpp`, `test_service_order_frontier.cpp`.

- [ ] RED: L1/L2 distinct training predicates; stream/filter/dedup/cross-page settings are actually consumed.
- [ ] RED: demand/prefetch late merge and fill pressure; prepare hint vs hardware prefetch distinct; pollution/eviction/writeback recorded once.
- [ ] RED: an unpublished earlier arrival cannot change an already committed FR-FCFS choice; refresh stalls are model-derived with explicitly approximate entry phase.
- [ ] Implement autonomous generation and shared event/resource admission; cover named SLICC state subset, fail closed outside it.
- [ ] GREEN against eligible original-source structural traces; missing speculative traffic is declared, never synthesized from oracle totals.

## P6. Profile integration and host-cost gate

**Files:** config parser/validation, main reporting, maintained profile documentation and meaningful end-to-end mechanism tests.

- [ ] RED: mixed old/new engines rejected; stale/missing capabilities cannot silently enable full support; profile OFF retains prior behavior.
- [ ] Integrate new runtime selection. Preserve old switches as explicit historical profiles until migration is justified.
- [ ] Run structural combination tests, exactly-once resource/population audits, safe-frontier progress checks and full default tests.
- [ ] Qualify continuous-warmup start-policy identity independently: preserve original common-start refs; any new source-retirement-start-v1 view is a separately named, audited series, never a silent reference replacement.
- [ ] Instrument hierarchy classification count, core work, calendar events, retries, allocations/live state and wall/RSS without hot-path diagnostic strings.
- [ ] Only after above gates, run bounded host benchmarks with same source/config/placement, serial interleaved >=3 pairs; report warmup/measurement/end-to-end separately and retain every sample.
- [ ] Compare against the proposed throughput budget and established OFF-path rules. No global default change or completed-alignment claim if performance/capability gates fail.

## Delivery status

- [x] Current background process check: no active replay or parent A1/A3 loop; provided PIDs absent.
- [x] Preserve historical A3-Marian failure and incomplete A3-XZ evidence; no new experiment started.
- [x] Independent read-only audits completed for store/Ruby, core/frontend/TLB, and two-stage causality.
- [x] Design and implementation route documented with source references and explicit approximation/performance boundaries.
- [x] Independent design review completed. Two P2 risks corrected in the spec: phase-aware prerequisite/closure for valid same-cycle progress; provisional FU schedules separated from provable publication lower bounds. Matching structural regression cases are required in P1.
- [x] 实施已启动：功能输入扩展、四案重采/输入审计、默认构建与机制子域回归完成。
- [ ] 完整实施与验收仍进行中：生产子域、剩余门控见docs/spr-two-stage-implementation-20261104.md；尚无完整四案新模型精度/吞吐结论。


## Immediate execution milestone after scope review

Spec: [FS execution bridge](../specs/2026-11-04-spr-fs-execution-bridge-design.md).
This is the next order within this plan, using its existing SDD ledger. New
baseline/mval/C1/oracle implementations remain unreleased.

### F1: Continuous source and per-core accounting adapter

Files: src/simulator.cpp, config.hpp/config.cpp, src/main.cpp, task_clock.hpp,
recorded_scheduler.hpp/.cpp, focused runtime/task-clock/scheduler tests.
TraceSource wrapper source changes only if the existing public interface
cannot preserve the exact source ordinal/phase. Root owns CMake/docs/Git.
Interfaces: consume current_source_record_ordinal(), functional_sync_layout(),
measurement_boundary_pending(), start_measurement(), CorePublication::retired,
FunctionalSyncPlan; add begin_core_measurement(core,origin) to existing clocks.

- [x] RED actual Simulator: false next at warmup still continues to measured EOF;
  a warmup miss/store/RAW stays live across the cut; uneven core origins do not
  synchronize; source cutoff/record mismatch cannot score a prefix.
- [x] RED clocks: account-out/in pending across local begin, known/unknown owner,
  short-core finish before detached drain, old global begin behavior unchanged.
- [x] Implement core.causal_fs_execution guarded adapter and copy current facts
  into bounded live envelopes; integrate actual model retirement and graph cuts.
- [x] GREEN selected production/clock/scheduler tests + default build/test; scoped
  review; record still unsupported classes and unchanged target parameter gates.
- [ ] Source/code/docs checkpoint through explicit root-owned staging.

### F2: Actual FLAC input blockers and necessary functional classes

Files: the existing runtime/core continuation and maintained profile/preparation
entries only as evidenced by FLAC. No new input schema without a demonstrated
missing functional fact. Exact task brief follows actual F1/FLAC evidence.

- [ ] Use the qualified FLAC C4 full manifest with unchanged originals; classify
  first-entry and whole-source unsupported classes/requirements by provenance.
- [ ] Add production REDs for actual necessary kernel/raw-syscall/branch/serial/
  IO/prefetch classes before runtime support; retain unsupported gates otherwise.
- [ ] Complete a full-window model run or explicitly report the concrete unclosed
  load-bearing requirement. Never crop to obtain success or restart A3.

### F3: Comparable results and expansion

- [ ] Verify source/population/target identity and active-start/end contracts.
- [ ] Run maintained native validator only for a genuinely matching reference;
  otherwise report exact unavailable reason, no fabricated CPI error.
- [ ] Once first case qualifies, repeat four cases and separately measure
  throughput with serial interleaved same-source runs and complete samples.
