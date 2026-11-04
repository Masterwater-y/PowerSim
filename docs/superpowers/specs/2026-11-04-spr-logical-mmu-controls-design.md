# SPR logical MMU control input v1

2026-11-04：在用户已批准的机制对齐计划内，先固化设计后实施。父代码基线
976b972f17d93303ef0dc6ca2ba1ea60f355418d（A+B普通多核子域），独立child
8ccad4329cc51e5544208ab9d0f1c01912fc0955，当前功能策略
1c3b49b6fe9a287a986ff957e02c773180545ac513abd0efdb197ae41ce9cc4c。新代码
将产生新策略，不能沿用旧SHA冒称同一producer。

本片只补默认关闭的纯功能MMU控制输入，不改变原生CPU/MMU执行结构、flush/
调用次序、测量截止或调度；不实现模型TLB时序，不采原生TLB命中/清除条目数/
ready/grant/时刻。原始FST和已有伴随保持，旧源继续标control-unavailable。
PageFault/external效果在此第一片仍UNBOUND；架构边界桥接另做。未完成该桥接
和其余必要输入前，不重新采完整四案，不宣称完美输入/完整SPR/精度/吞吐通过。

以下细节采用先前只读proposal的协议与生命周期，现作为实施约束。首次preflight
核实全部实际文件/API和bit17未占用；若事实冲突，明确报告并由root按既有授权
裁定，不能猜测原生效果。audit_fst_retired_frontend.py和.cpp均已实际核实存在，接这两个维护入口及
prepare/validator，不新建平行审计工具。

补充wire裁定：声明为u64的单hot-row hash使用现有32-bit hot-row hash零扩展，
高32位必须为0；全流hash仍用原有64-bit算法。禁止静默引入另一套row identity。
布局digest覆盖明确字段/枚举/边界语义，须有固定规范化描述和parent/native一致
测试，不能通过对含自身digest的header做hash形成自引用。

架构baseline只证明raw CR0/CR3/CR4/EFER，不证明初始TLB residency/empty。
新增source-retirement-start-v1计分参考视图是另一个待实施observer任务，不在
本片偷偷改写native task-clock、reference或common-start语义。

## 2. Proposed wire contract and bounds

Reserve `kLogicalMmuControls = 1ull << 17`, subject to a final collision check at EDITSTART. Read-only inspection found companion features through bit 16, with 15 = instruction translations and 16 = core facts; no bit-17 declaration was found in inspected feature sources. Keep all existing hot and companion layouts unchanged. New collector switch `--functional-mmu-controls`, TaoTrace parameter `functional_mmu_controls=False`, and independent declaration in provenance. A declared bit requires the file, matching policy/layout and a well-formed footer even when status is incomplete. Undeclared/missing historical data means unavailable, never an empty complete stream.

Use explicit little-endian codecs, no compiler struct dumps. New parent/native codec mirrors must be byte-identical. Every body frame begins with `u16 kind, u16 flags, u32 bytes`; unknown bits/types, nonzero reserved fields, invalid enums and impossible sizes reject v1. Proposed fixed sizes:

| Object | Bytes | Contents |
|---|---:|---|
| Header | 160 | magic[8]; version/header-size u16 each; endian marker u32; producer-policy SHA256[32]; control-layout SHA256[32]; required-protocol-flags u64; context u32; coverage-mask u32; max-live-owners/max-owner-ops/max-diagnostic-details u32 each; reserved u32; six frame sizes u32 each; reserved[24]. |
| Baseline | 80 | prefix; first retained ordinal/hash u64 each; raw CR0/CR3/CR4/EFER u64 each; known-mask u32; lifecycle generation u32; cut-proof flags u64; reserved u64. Initial native TLB residency is explicitly not represented/known. Empty stream uses an explicit Empty flag and no first-member claim. |
| RetainedMember | 48 | prefix; original full-source ordinal and exact existing hot-row hash u64 each; context/generation u32 each; operation count/status u32 each; reserved u64. One frame for every hot row, including zero-effect rows, with metadata seal status. |
| AppliedWrite | 96 | prefix; operation index/parent operation u32 each; requested value u64; complete before/after four-register states (64 bytes); register u16; mode u8; application u8; known masks u8 each; reserved u16. Parent is UINT32_MAX except explicitly supported nesting. |
| LogicalCommand | 48 | prefix; operation index/parent write index u32 each; kind/domain u16 each; flags u32; actual VA/ASN u64 each; reserved u64. Flush operands must be canonical zero; demap preserves exact supplied operands. |
| UnboundDiagnostic | 48 | prefix; context/generation/reason/operation count u32 each; raw owner ordinal/hash slots u64 each MUST be zero; reserved u64. Followed by the same bounded write/command frames. No fabricated retained owner or serialized native sequence/tick. |
| Footer | 256 | prefix; status mask u64; fourteen u64 counters/hashes listed below; sixteen u64 reason counters. Mandatory terminal frame, no trailing bytes or second footer. |

Header's six sizes refer to Baseline, RetainedMember, AppliedWrite, LogicalCommand, UnboundDiagnostic, Footer, in that order. Layout digest covers these exact field/enumeration/bound semantics, independent of the producer source policy. Body operation indices are contiguous and local to one member/diagnostic group; commands identify the exact earlier containing write, or UINT32_MAX for a standalone command. Serialize one applied write plus each actual child command; a consumer uses the commands explicitly and **must not additionally derive flushes from the write**. Write snapshots describe the full setter before/after transition; contained commands retain their order, without inventing intermediate state snapshots. Unsupported nested control setters fail completeness rather than flattening unknown order.

The fourteen footer scalars are hot rows, sealed hot rows, total/retained/unbound applied writes, total/retained/unbound logical commands, propagation callback count, diagnostic detail groups, suppressed diagnostic detail operations, pending owners, full-hot rolling hash, and body rolling hash. Use the existing FST full-hot hash contract and a documented domain-separated body hash including all frames before Footer. Hashes prove binding/integrity; native build/source identity remains a separate SHA256 gate.

Sixteen diagnostic counters: InitialCutUnknown, UnboundExternal, UnboundPageFault, EffectfulSquash, FilteredInterior, UnsealedOwner, EffectOrderUnsupported, OwnerOverflow, OperationOverflow, PropagationUnknown, DomainUnsupported, LifecycleInWindow, StateUnknown, CrossContextEffect, DiagnosticTruncated, CounterOverflow. They are reason occurrences and can overlap; they are not the disjoint conservation buckets. `total = retained + unbound` separately for applied writes and commands over the proven interval. Unsupported/squashed/filtered/unsealed effects enter unbound, never retained. Pending operations cannot be reported complete. An unknown initial cut makes interval totals uncertified even if numerically conserved. Prefix effects proven wholly before the cut are excluded from interval totals and folded into baseline; ambiguous crossings remain incomplete.

Proposed bounded defaults: at most 4096 live effect-owning DynInst payloads per context, 256 operations per owner, 256 serialized unbound diagnostic operations per stream; pending retained member seals bounded by 4096. Allocate an owner payload lazily on first effect; effect-free members need only a transient pending seal descriptor. Payload ownership follows native DynInst lifetime, without retaining DynInst pointers to prolong execution objects. Scalar mirrors, monotonic internal evidence counters and sticky diagnostics are O(contexts); no ROI event history. These caps are observation limits, **not target capacity parameters**. Crossing a cap retains exact scalar actual-call counts where possible, suppresses detail with an explicit count and permanently fails control completeness. Counter overflow saturates with a dedicated failure, never wraps into apparent completeness. Never stall/drop native work to preserve metadata.

Default v1 domain is `ContextAllTranslations` for the three supported MMU roots. Direct low-level TLB commands can be recorded as Instruction/Data/Unified diagnostic domains but remain DomainUnsupported until a separately validated logical domain mapping is supported. No TLB pointer, physical hierarchy object ID, contents, occupancy, cleared-entry count, hit/miss, source, invalidation generation, cycle, tick, ready time, issue time, native grant or speculative trace enters this wire.

## 3. Exact causal owners and setter instrumentation

Create a production observer helper, separate from the codec, with an observer registry keyed internally by context, lifecycle generation and dynamic sequence identity. Keys are never emitted; only successfully written retained ordinal/hash is serialized. Disabled mode does no register reads and makes all observation calls no-ops. Registry teardown must unregister ISA/MMU/TLB endpoints before their lifetimes end.

Actual hook points verified in current source:

1. `cpu/o3/dyn_inst.hh::updateMiscRegs`: enter this DynInst's owner/application scope around each existing `cpu->setMiscReg` invocation. Preserve the loop, order, values and `noSquashFromTC` behavior exactly.
2. `setMiscRegOperand`: enter an Immediate scope only around the existing immediate `cpu->setMiscReg` branch; do not create a call or claim this branch activates for the current x86 classes. Its later deferred invocation remains a second actual operation if it occurs.
3. `DynInst::demapPage`: enter this DynInst's owner scope around the unchanged native call with its actual VA/ASN. Do not use mnemonic decoding or hot memory address as operand substitutes.
4. `arch/x86/isa.cc::setMiscReg` and `setMiscRegNoEffect`: observe CR0/CR3/CR4/EFER at the lowest actual setters. An outer WithEffect invocation owns its nested terminal NoEffect storage update; suppress only that explicitly matched storage child, not every nested call. Direct NoEffect remains an actual separate write. Before/after snapshots use `readMiscRegNoEffect`; capture CR0's direct EFER.LMA mutation and forced ET in post-state. An unexpected nested independent setter is counted and fails v1 ordering qualification.
5. `setMiscReg` staging is unchanged: current duplicate register handling returns without replacing its staged value. Requested-but-unapplied staging entries generate no AppliedWrite record. Same-value applied CR3 generates both its write and its native FlushNonGlobal child. Same-value direct NoEffect generates its write and no invented flush.

Scope membership alone does not prove a retired functional order. Internal effect tickets track each owner's first/last effect and whether other owners intervened. These tickets never become replay timing/order data. Retained owners must form a compatible ordered operation stream; effects by a younger owner before an older member's own deferred writes, interleaved owners or unresolved crossing of the initial cut fail qualification. Do not reorder actual effects into apparently valid retirement order. Destruction/squash of an effectful owner makes it unbound; destruction without effects creates no failure.

`PageFault::invoke` can add an observer-only diagnostic-cause scope so its existing demap is specifically counted as UnboundPageFault. This scope supplies **no owner**, token, ordinal or handler relation. An active unrelated owner scope must not accidentally absorb this external fault effect. External ThreadContext/CPU commands likewise require their explicit absence of retained ownership. Native calls remain unconditional and unchanged.

## 4. Logical command identity and explicit propagation ancestry

Top-level `BaseMMU::flushAll`, `BaseMMU::demapPage` and `X86ISA::MMU::flushNonGlobal` create a fresh command token on **every invocation**, even with identical arguments or inside another callback. Capture owner and containing write scope once. Each original MMU-to-TLB dispatch installs a single-use propagation-edge permit `(token, parent endpoint, expected child endpoint, kind, operands)` immediately around that unchanged call. Each x86 TLB entry consumes only a matching permit. Before its original nextLevel recursion it installs another matching edge permit. This distinguishes declared propagation from arbitrary reentrancy.

The three low-level `TLB::{flushAll,flushNonGlobal,demapPage}` methods must all observe entry before native changes. A call without a matching permit is a new direct command, even if another command is on the stack. It is never silently merged by depth or equal arguments. A callback such as `codeInvalidation` that causes a genuinely independent nested MMU/TLB call receives a new token; it cannot reuse a still-active parent permit. Permits are consumed before callbacks, are bound to the expected callee and expire at call return. Token stack depth and registry limits fail closed if exceeded.

One command may visit the same shared STLB repeatedly through distinct valid edges. Count propagation witnesses separately and preserve **every native visit and invalidationGeneration increment**; emit only the originating logical command. No dedup of actual calls occurs. Build registration from the actual selected context/MMU and BaseTLB hierarchy; instruction/data/shared membership is explicit. Same TLB registered to unrelated contexts, unsupported direct domains, unknown endpoints, unexpected next-level edges or mismatched operands become completeness failures. No callback-time current-core or most-recent-instruction lookup assigns an owner.

## 5. Baseline, retained binding, post-update seal and footer

Register observation before native execution. `ISA::clear`, `ISA::unserialize`, TLB restore and MMU/CPU reset/takeover are explicit lifecycle transitions, not ordinary control deltas. Establish a baseline generation only after completed restore or KVM-to-O3 transfer and before resumed execution. Candidate final hooks are O3 startup after BaseCPU startup and before stage startup, O3 takeOverFrom after BaseCPU/state/stage takeover and before any activation, and drainResume before stage resumption, guarded by pending-lifecycle state. Merely calling drainResume again does not create a fresh baseline or erase failures. In-window lifecycle replacement is unsupported and sticky. Source-hook fixtures must demonstrate these positions; if actual runner restore/resume ordering cannot prove them, report InitialCutUnknown.

Keep a four-register scalar mirror; capture an owner's pre-state at its first actual effect. At a successful hot write, select this stored pre-state if the owner already had effects; otherwise capture the successful-commit pre-update state. The first actually emitted member chooses the stream baseline only after a compatible initial cut is proven. A merely called Commit callback, a filtered member or a counter sampled before `fwrite` cannot choose it. Pretracking plus wholly completed excluded prefix owners may establish the cut without a log; an effectful live owner spanning it cannot. Filtered effectful interior members invalidate completeness; do not filter more data to repair the window.

Current source has `ppPreCommit -> commitHead -> successful ppCommit -> ... -> updateMiscRegs`. PreCommit can repeat/fault and is not used as a retained anchor. Add a new observer-only `CommitMiscRegsApplied` notification immediately after the unchanged `head_inst->updateMiscRegs()` and before checker verification. In TaoTrace bind owner to the exact ordinal/hash **after successful hot write**, before incrementing `fst_record_count_`; audit both hot emission paths (normal micro record and syscall/marker route) so unsupported synthetic ownership fails explicitly. Matching post-update notification seals the whole owner's operations. Member records become eligible for sidecar emission only then. Check hot-row hash and identity again at seal; never bind on pointer reuse.

Separate existing FST/measurement closing from the new sidecar's metadata close. Existing native stop, task clock, common end, exit scheduling and existing companion finalization remain at their original sites. A sidecar close request stores final hot count/hash and waits only for already executing pending post-update metadata notifications; it schedules no native instruction or extra cycle. The post-update hook can finalize the sidecar when its last bound member is sealed. If actual exit/destruction occurs without a seal, write a finalized **incomplete** footer including UnsealedOwner/pending count, or leave an interrupted file invalid if IO cannot finish. Never manufacture a seal or report completeness on EOF. Effects after the declared functional cut cannot extend the stream or get assigned to the last row; unsupported crossings fail completeness.

Empty, disabled, absent, truncated, structurally valid incomplete and control-ready are separate states. Structural validation may accept a finalized incomplete file for diagnosis; model qualification requires full known baseline, coverage registration, unique binding, all members sealed, conserved operations and no completeness failure. Initial architectural state alone never certifies warm/cold/native TLB history.

## 6. Proposed exact file ownership after release

Paths below are intended edits, not edits made by this preflight. Some overlap the frozen core-facts files; **none can be touched until root releases a new post-review checkpoint**. Read-only policy closure files are distinguished from source edits.

| Owner | Files / purpose |
|---|---|
| Worker, new shared codec | `include/fastsim/fst_mmu_controls.hpp`; exact mirror `gem5-spr/taogen/shared/fst_mmu_controls.hh`: schema/codec/stream writer, bound validation. |
| Worker, new reader | `include/fastsim/fst_mmu_control_reader.hpp`, `src/fst_mmu_control_reader.cpp`: full-source streaming prevalidation, reopen and bounded replay, current facts/coverage. |
| Worker, new native helper | `gem5-spr/src/mem/functional_mmu_controls.hh`: production bounded owner/state/command/propagation helper and observer registry, independent of O3 target scheduling. Prefer header-only to avoid unrelated build plumbing. |
| Worker, native owners/seal | `gem5-spr/src/cpu/o3/dyn_inst.hh`, `dyn_inst.cc`, `commit.hh`, `commit.cc`: immutable/lifetime-owned payload, cleanup, probe registration and post-update notification. |
| Worker, native state/lifecycle | `gem5-spr/src/arch/x86/isa.cc`, `isa.hh`, `tlb.cc`, `tlb.hh`, `faults.cc`; `gem5-spr/src/cpu/o3/cpu.cc`; `gem5-spr/src/arch/generic/mmu.cc`, `mmu.hh`, `tlb.hh`; `gem5-spr/src/arch/x86/mmu.hh`: actual setter, registration, lifecycle, diagnostic cause and propagation scopes. |
| Worker, collector integration | `gem5-spr/src/cpu/o3/probe/tao_trace.cc`, `tao_trace.hh`, `TaoTrace.py`; `gem5-spr/configs/fastsim/collection.py`: switch, registry lifetime, exact emission binding, seal and independent metadata close. |
| Worker, provenance | `gem5-spr/src/cpu/o3/probe/generate_static_uop_policy.py`, `SConscript`: complete source digest and dependency closure. Extending common digest is proposed so parent/native source qualification stays unified; root must freeze a new policy after review. |
| Worker, parent integration | `include/fastsim/trace.hpp`, `src/trace.cpp`; `tools/audit_fst_retired_frontend.cpp`, `audit_fst_retired_frontend.py`, `prepare_spr_capture.py`, `run_native_kernel_fastsim_validation.py`: optional exposure, full-source checks, feature masks, separate capabilities and companion fingerprint. No runtime timing consumption. |
| Worker, tests | New `tests/test_fst_mmu_controls.cpp`, `tests/test_mmu_control_observer.cpp`, `tests/test_mmu_control_source_hooks.py`; extend `tests/test_prepare_spr_capture.py`, `tests/test_spr_collection_entry.py`, `tests/test_fst_privilege_features.py` where necessary. No changes to core-facts tests merely to accommodate a failing gate. |
| Root only | Canonical `CMakeLists.txt` and curated docs; parent/native full builds; all captures; source freeze/receipts/portable patch; commits/push. No parent gitlink advance or child-origin push. |

Source closure must include all edited paths and the existing actual ancestor paths, even when unedited: `arch/x86/{faults.hh,pagetable.cc,pagetable.hh}`; `arch/x86/kvm/{x86_cpu.cc,x86_cpu.hh}`; `cpu/{base.cc,base.hh,BaseCPU.py,thread_context.cc,thread_context.hh,exec_context.hh,reg_class.hh,reg_class.cc}`; `cpu/o3/{cpu.hh,thread_context.cc,thread_context.hh,thread_state.cc,thread_state.hh}`; existing used `cpu/kvm/base.*`; `sim/mem_state.cc`; generic ISA and relevant MMU/TLB registration metadata; x86 ISA/regs trees including INVLPG/tia; existing retirement/filter helpers and the actual selected capture runner. Before editing the manifest, enumerate only existing used files and verify build dependency parity. No handoff/reset/KVM execution rewrites are proposed; existing bulk-change observation plus explicit supported resume boundaries supply proof or fail closed.

Parent `TraceSource` adds optional `current_mmu_controls`, `mmu_control_baseline`, version/policy/coverage accessors. Binary source validates the **entire original source** before slice replay; Slice/Warmup wrappers keep original ordinals and clear current facts at every EOF/phase transition. A slice does not relabel source baseline as slice baseline or discard controls from its prefix: expose original baseline and prefix requirement, and keep standalone slice-control readiness false unless a later consumer reconstructs the required logical prefix explicitly. `BinaryTraceWriter` initially rejects the new feature unless a real bound-companion writer API is deliberately supplied; fixture construction may use the actual codec directly. Maintained prepare reports availability, structure, baseline, retained-owned coverage, unbound reasons and control readiness separately; fullSPR remains false.


## Implementation gate

Worker先核实当前源接口与该schema，按本计划TDD实际observer/helper/codec/reader，
而非平行toy；独立复审及root维护build、同原检查点OFF/ON验证后才能登记输入
能力。root负责CMake、native/parent canonical build、capture、portable patch和
提交推送。源/协议可读性和明确不完整原因优先，保留所有失败输出。
