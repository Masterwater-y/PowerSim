#pragma once

#include <cstddef>
#include <cstdint>
#include <optional>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <vector>

namespace fastsim {

enum class ReplacementPolicy {
    kLru,
    kTreePlru,
};

// Accuracy and throughput are reported for exactly one privilege scope per
// run.  kUnspecified keeps library construction backward compatible, but the
// CLI refuses to simulate until the caller selects one of the two concrete
// scopes.
enum class MeasurementScope {
    kUnspecified,
    kUser,
    kUserPlusKernel,
};

struct PageFaultAllocationProbability {
    std::uint32_t read_ppm = 0;
    std::uint32_t write_ppm = 0;
};

MeasurementScope parse_measurement_scope(const std::string& value);
const char* measurement_scope_name(MeasurementScope scope);

struct CacheConfig {
    std::uint64_t size_bytes = 0;
    std::uint32_t associativity = 1;
    std::uint32_t line_size = 64;
    std::uint32_t hit_latency = 1;
    ReplacementPolicy replacement = ReplacementPolicy::kLru;
    // causal_read only: admission-to-miss-request and fill-to-response
    // service edges. Unset miss latency retains the old generic fixture
    // default; zero is an explicit, valid combinational edge.
    std::optional<std::uint32_t> miss_request_latency = std::nullopt;
    std::uint32_t fill_response_latency = 0;
};

struct BranchConfig {
    std::string type = "tournament";
    std::uint32_t local_counter_bits = 2;
    std::uint32_t global_counter_bits = 2;
    std::uint32_t choice_counter_bits = 2;
    std::uint32_t local_history_entries = 2048;
    std::uint32_t local_entries = 2048;
    std::uint32_t global_entries = 8192;
    std::uint32_t choice_entries = 8192;
    std::uint32_t inst_shift = 0;
    std::uint32_t btb_entries = 4096;
    std::uint32_t btb_associativity = 1;
    std::uint32_t btb_tag_bits = 16;
    std::uint32_t btb_set_shift = 0;
    std::uint32_t ras_entries = 16;
    // Source-aligned call handling. When a producer-neutral static instruction
    // provider has an exact row for a call, push its architectural fallthrough
    // PC immediately, matching gem5 buildRetPC semantics. Missing metadata
    // remains a supported, explicitly counted causal-learning fallback.
    bool ras_static_return_target = true;
    std::uint32_t indirect_sets = 256;
    std::uint32_t indirect_ways = 2;
    std::uint32_t indirect_tag_bits = 16;
    std::uint32_t indirect_path_length = 3;
    std::uint32_t indirect_speculative_path_length = 256;
    std::uint32_t indirect_ghr_bits = 13;
    bool indirect_hash_ghr = true;
    bool indirect_hash_targets = true;
    bool requires_btb_hit = false;
    bool update_btb_at_squash = true;
    // Reproduce gem5's predictor-state timing without requiring wrong-path
    // instructions in the functional trace. Direction/local histories are
    // updated at the modeled Fetch prediction, repaired from a per-branch
    // checkpoint on a miss, and predictor tables are trained only when the
    // branch reaches its modeled ordered-retire cycle.
    bool speculative_history = false;
    std::uint32_t mispredict_penalty = 16;
    // gem5's optional squashWidth. Zero represents the target's unset/NullOpt
    // value: all younger instructions are squashed in one cycle. A nonzero
    // value is required before anonymous wrong-path occupancy may delay rename.
    std::uint32_t squash_width = 0;
    // Diagnostic anonymous wrong-path occupancy derived from the functional
    // branch miss and target pipeline geometry. It never invents wrong-path
    // addresses or operation classes. With squash_width=0 it is a no-op and
    // does not scan the ROB, matching the target and preserving throughput.
    bool shadow_rob = false;
    // Audit-only estimate of the younger wrong-path population present when
    // a mispredicted branch resolves. The estimator uses recent committed
    // frontend supply and the resolution-time ROB headroom; it never changes
    // a pipeline ready cycle. This must remain separate from shadow_rob until
    // a gem5 oracle validates a causal timing conversion.
    bool population_audit = false;
    std::uint32_t population_history_cycles = 64;
};

struct DramConfig {
    std::uint64_t size_bytes = 64ull << 30;
    std::uint32_t channels = 8;
    // Kept under the legacy key name for compatibility; this is gem5's
    // banks_per_rank, while ranks_per_channel supplies the other dimension.
    std::uint32_t banks_per_channel = 16;
    // Keep the generic default backward compatible. gem5-aligned profiles
    // set this explicitly from DRAMInterface::ranks_per_channel.
    std::uint32_t ranks_per_channel = 1;
    // gem5 DRAMInterface exposes bank groups independently of banks/rank.
    // A value of one preserves DDR generations without bank groups.
    std::uint32_t bank_groups_per_rank = 1;
    std::uint32_t row_bytes = 8192;
    std::uint32_t t_cl = 22;
    std::uint32_t t_rcd = 22;
    // Mixed RD/WB service timings, in cycles. Zero for CWL/RCD_WR inherits
    // CL/RCD respectively; legacy profiles must not acquire fixture defaults.
    // The remaining zero values disable the corresponding extra constraint.
    std::uint32_t t_cwl = 0;
    std::uint32_t t_rcd_wr = 0;
    std::uint32_t t_ccd_l_wr = 0;
    std::uint32_t t_rtw = 0;
    std::uint32_t t_wtr = 0;
    std::uint32_t t_wtr_l = 0;
    std::uint32_t t_wr = 0;
    std::uint32_t t_rp = 22;
    // Minimum ACT-to-PRE and read-to-PRE delays. Zero preserves the legacy
    // compact bank calendar for profiles that do not expose these timings.
    std::uint32_t t_ras = 0;
    std::uint32_t t_rtp = 0;
    // Minimum ACT-to-ACT spacing within a rank. DDR4 can require a longer
    // delay when both banks belong to the same bank group (tRRD_L).
    // Zero disables the corresponding constraint for legacy profiles.
    std::uint32_t t_rrd = 0;
    std::uint32_t t_rrd_l = 0;
    // Rolling activation-window constraint: at most activation_limit ACTs
    // may start in t_xaw cycles on one rank. An activation_limit of zero
    // disables the window while retaining the timing field in the profile.
    std::uint32_t t_xaw = 0;
    std::uint32_t activation_limit = 0;
    std::uint32_t burst_cycles = 4;
    // Same-bank-group column-to-column spacing (gem5 tCCD_L). Zero disables
    // the additional constraint, leaving burst_cycles as the channel data-
    // bus spacing and preserving legacy configurations.
    std::uint32_t t_ccd_l = 0;
    // Extra command gap when the data bus changes rank. gem5 calls this tCS.
    std::uint32_t t_cs = 0;
    // gem5 MemCtrl adds these fixed delays when returning a serviced packet;
    // they are deliberately outside DRAM bank and data-bus occupancy.
    std::uint32_t frontend_latency = 0;
    std::uint32_t backend_latency = 0;
    // `fcfs` is the deterministic single-request reference. `frfcfs`
    // performs an event-driven controller-window repair after an interval has
    // established canonical cache/directory paths.
    std::string scheduler = "fcfs";
    // Physical gem5 controller queue capacity. It remains distinct from the
    // bounded candidate set used when functional traces cannot reconstruct
    // the exact cycle at which requests became visible to the controller.
    std::uint32_t read_buffer_size = 64;
    // Source-aligned dirty-writeback path. Each channel buffers LLC dirty
    // victims, and demand reads retain priority until the gem5-style high
    // watermark and minimum-turnaround conditions request a bounded write
    // drain. Explicitly disabling it restores the legacy immediate-write
    // DRAM-calendar behavior for differential validation.
    bool separate_write_queue = true;
    std::uint32_t write_buffer_size = 128;
    std::uint32_t write_high_threshold_percent = 85;
    std::uint32_t write_low_threshold_percent = 50;
    std::uint32_t min_reads_per_switch = 16;
    std::uint32_t min_writes_per_switch = 16;
    // Zero uses the complete physical read buffer. A smaller nonzero window
    // limits only FR-FCFS lookahead, not queue admission or occupancy. In
    // topology-scaled mode it is the maximum automatically selected window.
    std::uint32_t frfcfs_selection_window = 0;
    // Bound ambiguity by static producer topology: core-rank lanes per
    // channel, less the request already at the service head.
    bool frfcfs_topology_scaled_window = false;
    // Experimental source-alignment edge: when enabled, open_adaptive scans
    // the complete reconstructed admitted queue after service selection.
    // Keep disabled until the isolated memory and full-suite gates pass.
    bool frfcfs_full_queue_page_policy = false;
    // gem5 skips adaptive page-policy evaluation once the per-row access cap
    // has already requested auto-precharge. Keep this correction independently
    // gated so it cannot silently change the production timing baseline.
    bool frfcfs_row_cap_single_precharge = false;
    // Experimental gem5 selection-event boundary: only requests already
    // arrived at this decision may influence selection/page policy. Advance
    // the next decision by nextBurstAt - (tRP + tRCD), not by completion.
    // Does not implement refresh, response-queue capacity or hidden-bank prep.
    // Has no effect when the outer repair gate bypasses FR-FCFS (including
    // the maintained C4/C8 topology-scaled window of one).
    bool frfcfs_causal_selection = false;
    std::uint32_t frfcfs_passes = 4;
    // Functional traces provide a lower-bound issue time but not the exact
    // cross-core arbitration phase. Nonzero values form a partial-order
    // arrival class that is fairly merged while preserving per-core order.
    std::uint32_t frfcfs_arrival_bucket_cycles = 0;
    // Zero keeps a row open indefinitely. gem5's captured DDR4 interface
    // auto-precharges after 16 column accesses.
    std::uint32_t max_accesses_per_row = 0;
};

// gem5's x86 TLB implementation is a capacity-configurable fully associative
// LRU. SE mode performs an atomic process-page-table lookup on a miss, while
// FS/timing studies can select an explicit fixed-latency walker because page
// table memory references are absent from the functional trace.
struct TlbConfig {
    bool enabled = false;
    // Optional state-only replay of predictor-selected static memory PCs.
    // It reuses instruction-path reconstruction but does not require or mutate
    // speculative L1I state. Dynamic pages come only from causally observed
    // committed PC mappings; speculative accesses remain outside
    // architectural PMU counters.
    bool speculative_path_state = false;
    std::uint32_t entries = 64;
    std::uint32_t hit_latency = 0;
    std::string miss_model = "timing_walk";
    std::uint32_t page_walk_latency = 60;
    // O3 memory instructions are deferred while a timing walk is active.
    // Once the final PTE response installs the translation, the instruction
    // must return through IQ issue/execute before its data request is sent.
    // This is distinct from the walker's zero-cycle response-to-next-level
    // edge and from the instant at which the timing TLB becomes visible.
    std::uint32_t page_walk_restart_latency = 2;
    // Experimental source-causal walker path. A timing miss emits one
    // read-only PTE request per captured level into the ordinary data
    // hierarchy. page_walk_levels is the maximum accepted depth; shorter
    // huge-page paths retain their actual lower-bound latency. The legacy
    // fixed-latency path remains the default.
    bool hierarchy_walk = false;
    std::uint32_t page_walk_levels = 4;
    // `physical_sidecar` consumes only functional PTE physical addresses
    // captured by the producer. A path missing from both causal snapshots
    // keeps the fixed-latency walk and is counted explicitly; it never falls
    // back to a synthetic address or a later page-table state.
    // `synthetic` preserves the structurally-sharing hash model solely for
    // explicit diagnostics; it must never be mistaken for a measured guest
    // page-table layout.
    std::string page_walk_address_mode = "physical_sidecar";
    // gem5's x86 timing walker has one active walk and queues followers.
    // Coalescing remains an explicit opt-in approximation for other targets;
    // the gem5-aligned default mirrors the source TODO/no-coalescing path.
    std::uint32_t page_walkers = 1;
    bool coalesce_misses = false;
};

// Frozen inference-time estimate for one kernel event. Syscalls select a
// profile by number; first-touch faults and periodic IRQs use one calibrated
// profile per enabled model. PMU fields are exclusive kernel contributions.
// An independently enabled page-fault state model may mutate cache state
// without adding these counters or kernel time. blocked_wall_cycles is
// deliberately report-only: per-task hardware counters stop while a task is
// descheduled, so it must never be folded into active service or core CPI.
struct KernelEventProfile {
    // Number of numeric fields in the parsed compact representation. Zero is
    // reserved for programmatically constructed/internal profiles.
    std::uint8_t encoding_fields = 0;
    std::uint32_t service_cycles = 0;
    std::uint64_t blocked_wall_cycles = 0;
    std::uint64_t retired_instructions = 0;
    std::uint64_t retired_uops = 0;
    std::uint64_t memory_uops = 0;
    std::uint64_t line_requests = 0;
    std::uint64_t branches = 0;
    std::uint64_t branch_misses = 0;
    std::uint64_t l1d_accesses = 0;
    std::uint64_t l1d_misses = 0;
    std::uint64_t l2_accesses = 0;
    std::uint64_t l2_misses = 0;
    std::uint64_t llc_accesses = 0;
    std::uint64_t llc_misses = 0;
    std::uint64_t permission_upgrades = 0;
    std::uint64_t remote_supplies = 0;
    std::uint64_t llc_merged_misses = 0;
    std::uint64_t llc_unique_fills = 0;
    std::uint64_t dram_reads = 0;
    std::uint64_t dram_writes = 0;
    std::uint64_t dtlb_accesses = 0;
    std::uint64_t dtlb_misses = 0;
};

using SyscallKernelEventProfile = KernelEventProfile;

struct SimulatorConfig {
    MeasurementScope measurement_scope = MeasurementScope::kUnspecified;
    // The functional input itself contains privilege-tagged CPL0 records.
    // This exact input mode is intentionally exclusive with every synthetic
    // kernel service/state model to prevent double counting.
    bool native_kernel_trace = false;
    std::uint32_t cores = 64;
    // FastSim's shared timing calendars retain the historical target-cycle
    // representation, but that cycle is now an explicit reference-time
    // domain rather than an implicit per-core clock.  A 3 GHz reference keeps
    // every existing profile bit-for-bit compatible.  Per-core clocks map
    // local pipeline cycles onto this common domain, allowing independently
    // clocked cores to participate in one deterministic event order.
    std::uint64_t reference_frequency_hz = 3'000'000'000ull;
    std::uint64_t core_frequency_hz = 3'000'000'000ull;
    // Empty selects core_frequency_hz for every core.  Otherwise this vector
    // must contain exactly one non-zero frequency for each configured core.
    std::vector<std::uint64_t> core_frequencies_hz;
    // Per-core producers decode this many retiring uops per resident chunk.
    std::uint32_t chunk_instructions = 4096;
    std::uint32_t lookahead_chunks = 2;
    std::string core_model = "scalar";
    // Experimental cross-quantum pending-service architecture. "off" keeps
    // the maintained closed-epoch timing path unchanged.
    std::string cross_q_service_mode = "off";
    // Only core.model=causal_read: stream diagnostic event identities/times
    // to CSV. This output is never an input to scheduling or path selection.
    std::string causal_read_audit_path;
    // Diagnostic projected RD/WB stream, including warmup and accepted batch
    // boundaries. Never consumed by production timing; empty disables it.
    std::string projected_dram_audit_path;
    // Experimental fixed-path RD/WB response feedback. Replaces read-only
    // FRFCFS repair, but retains canonical cache/MSHR state and projected
    // arrivals. Not an actual-arrival or fill-consistent controller.
    bool projected_dram_feedback = false;
    // The legacy frontier scheduler advances to the smallest per-core UOP
    // proposal.  The time_epoch scheduler instead treats chunk_instructions
    // as an internal decode microbatch and extends per-core lookahead until a
    // common simulated-time epoch is covered.
    std::uint32_t interval_target_uops = 256;
    std::uint32_t interval_max_cycles = 1024;
    // Optional canonical time-epoch quantum used only by a functional warmup
    // prefix. Zero preserves the measurement quantum. This allows Q-sweep
    // diagnostics to begin from one identical warmed shared/DRAM state while
    // leaving the measured scheduler quantum as the sole variable.
    std::uint32_t functional_warmup_interval_max_cycles = 0;
    std::string interval_scheduler = "frontier";
    bool interval_full_order_audit = true;
    // Count only response-corrected inversions among accesses to the same
    // cache line. This is diagnostic-only and can be disabled independently
    // of the target timing/state transition in production throughput runs.
    bool interval_same_line_order_audit = true;
    // Attribute each interval's newly exposed response-feedback cycles to
    // exactly one critical cause. This is an audit-only path: it must not
    // alter simulated timing or PMU state.
    bool cpi_attribution = false;
    // Conflict-certified private-cache preview is only used by time_epoch.
    // The permanent per-core decode workers execute the preview phase too.
    bool interval_private_preview = false;
    // Maximum number of transactional weave attempts. One preserves the
    // original single-pass behavior. Values above one enable corrected-
    // arrival replay for certified preview and cross-core same-line risk
    // epochs before falling back to the canonical lower-bound order.
    std::uint32_t interval_reweave_passes = 1;
    // Recompute only shared-resource timing at corrected arrivals while
    // preserving canonical order inside every non-commuting cache/directory
    // component. This is independent of the legacy whole-epoch reweave.
    bool interval_causal_timing = false;
    // Perform one bounded shared-timing replay at response-corrected memory
    // issue times, then accept it only when the resulting arrival order is
    // stable. Unlike interval_causal_timing this is deliberately not a
    // fixed-point loop; the canonical cache/directory path remains fixed.
    bool interval_response_retime = false;
    // Replay shared-resource timing only for the bounded suffix opened when
    // a response-extended ROB predecessor first blocks younger admission.
    // The suffix remains live across Q checkpoints until a younger ROB head
    // reaches its lower-bound retire time again. Functional cache/directory
    // state stays on the canonical path; this switch changes timing only
    // after a stable local certificate.
    bool interval_rob_head_suffix_replay = false;
    // Fixed-point attempts for the corrected-arrival timing certificate.
    std::uint32_t interval_causal_passes = 4;
    // Defer an epoch when the union of path-pinned LLC-set components is too
    // large. Zero is deliberately not accepted: an unbounded closure would
    // recreate B0's whole-epoch replay failure mode.
    std::uint32_t interval_causal_max_closure_events = 4096;
    // Defer the per-core suffix starting at the first memory request whose
    // response-corrected issue exceeds the time-epoch horizon. Canonical
    // functional cache/directory effects are materialized once and retained;
    // only replayable CHA/MSHR/DRAM timing is rebuilt from the epoch entry.
    bool interval_corrected_suffix_carry = false;
    // The response-driven timing correction is independent across cores once
    // an epoch's memory responses are known. Reuse the permanent per-core
    // producer workers for that phase; disabling this option retains the
    // deterministic serial implementation as an equivalence reference.
    bool interval_parallel_feedback = false;
    // Persistent host workers reserved for ownership-domain phases. Zero
    // selects min(target cores, 8). They are deliberately separate from
    // decode producers so an epoch barrier never waits for a transport decode
    // microbatch to finish.
    std::uint32_t domain_workers = 0;
    // Do not enter a parallel ownership-domain phase unless both its boundary
    // and certified-safe work reach this event count.
    std::uint32_t domain_min_events = 2048;
    std::uint32_t fetch_width = 8;
    // gem5's fetch buffer contains one aligned instruction block. A switch
    // to another block cannot consume unused width from the current cycle;
    // the refill latency counts completely empty cycles between blocks.
    // Zero bytes disables the functional-PC fetch-buffer model.
    std::uint32_t fetch_buffer_bytes = 0;
    std::uint32_t fetch_buffer_refill_latency = 0;
    // Source-aligned request/response admission for the one-block O3 Fetch
    // buffer.  The legacy compact model can complete a future block response
    // while Fetch is stopped by a redirect, serialization, or a full fetch
    // queue.  gem5 creates that request only after Fetch may run again.  This
    // candidate moves request creation to that causally valid admission edge.
    bool fetch_supply_model = false;
    // Use a portable static instruction map to request the second block of an
    // x86 macro instruction whose bytes cross the 64-byte boundary; all
    // micro-ops otherwise carry only the macro start PC. This exact,
    // producer-neutral edge is the production default after the complete
    // C4/C8/C16/C32 gate; it remains independently switchable for ablation.
    bool fetch_supply_static_instruction_span = true;
    // Address-free diagnostic shadow for requests that may be generated
    // after a mispredicted branch and before it resolves. The population,
    // request density, and response service prior all come from the bounded
    // branch.population_audit history window. It never invents a PC, changes
    // cache tags, or adds a fixed wrong-path penalty. Only an actually
    // outstanding response beyond recovery may delay Fetch.
    bool fetch_supply_speculative_shadow = false;
    // Optional committed-PC instruction-cache model. The functional PC is
    // sufficient for the target's VIPT L1I set index because all index bits
    // lie within a 4-KiB page. `miss_penalty` is the additional empty-fetch
    // time beyond the ordinary fetch-buffer/L1I hit refill; it does not
    // invent speculative instruction accesses.
    bool l1i_enabled = false;
    std::uint32_t l1i_miss_penalty = 0;
    // Bridge from a committed L1I miss to a physical lower-level request
    // descriptor. `trace.instruction_address_mode` selects either the
    // portable built-in modeled mapping or an exact `.fst.ifmap` oracle.
    bool fetch_supply_physical_request_ledger = false;
    // Replay committed L1I-miss descriptors through the unified private L2
    // and shared LLC/DRAM hierarchy. This remains independently gated so the
    // request ledger can still be audited without changing cache state/PMU.
    bool fetch_supply_lower_hierarchy = false;
    // On a replayed branch miss, touch the first exactly reconstructable
    // predicted-path instruction line in L1I state. This uses only the
    // predictor's causally available BTB target. It never fabricates a
    // fallthrough PC, data access, retired PMU event, or direct cycle charge.
    bool l1i_speculative_entry_state = false;
    // Extend the exact predicted entry through a causally learned committed
    // PC-successor graph. Replay is bounded by branch resolution time and ROB
    // capacity and stops at an unknown edge. With the physical I-fetch ledger
    // enabled, wrong-path L1I misses also enter the shared hierarchy as
    // non-retiring requests; they never replay data addresses or report
    // speculative PMU as retired.
    bool l1i_speculative_path_state = false;
    std::uint32_t decode_width = 8;
    std::uint32_t rename_width = 8;
    std::uint32_t issue_width = 4;
    std::uint32_t dispatch_width = 4;
    std::uint32_t writeback_width = 8;
    std::uint32_t commit_width = 4;
    std::uint32_t fetch_queue_entries = 32;
    std::uint32_t rob_entries = 192;
    std::uint32_t iq_entries = 64;
    std::uint32_t lq_entries = 32;
    std::uint32_t sq_entries = 32;
    // Audit only: reconstruct committed-path destination-register lifetimes
    // plus a mutually exclusive lower-bound dispatch-gate ledger.  The
    // functional trace has destination counts but no wrong-path UOPs or exact
    // destination register classes, so this switch must not alter timing.
    bool committed_pipeline_audit = false;
    // Experimental gap-aware FU scheduler. The legacy lower-bound scheduler
    // represents each FU lane with one tail timestamp, so a dependency-delayed
    // UOP can reserve a future slot and hide an otherwise usable earlier gap
    // from younger ready UOPs. This mode uses a bounded per-pool capacity
    // calendar while preserving the existing issue-width and memory-port
    // constraints. It is default-off until CPI and throughput gates pass.
    // With committed_pipeline_audit enabled and this switch disabled, the same
    // calendar is maintained read-only to report legacy future-reservation
    // opportunities without changing timing.
    bool fu_gap_aware_schedule = false;
    // Experimental committed RAW repair. Reconstruct architectural producer
    // edges from operand-complete `.fst.imap` v2 rows. Timing changes only for
    // UOPs whose n_src proves that the four fixed dynamic producer slots were
    // truncated and whose latest static writer is absent from those slots.
    // Default off until held-out accuracy gates pass.
    bool committed_static_dependency_feedback = false;
    // Consume producer-neutral memory-ordering facts from `.fst.imap` v3.
    // A barrier executes only after the older committed frontier (and, in
    // response replay, the older TSO store drain) has completed; younger
    // work cannot cross the macro's final barrier.  Missing/legacy maps keep
    // the historical behavior, so this remains an explicit accuracy gate.
    bool committed_static_memory_ordering = false;
    // Experimental committed-stream StoreSet lower bound. A PC is trained
    // after the functional stream proves that one macro instruction contains
    // an overlapping load/store (the x86 RMW decomposition that aliases the
    // target SSIT key). Later load and store UOPs at that PC wait on the
    // latest still-live same-PC store's address-generation completion. This
    // never consumes gem5 timing labels or wrong-path instructions.
    bool store_set_same_pc_feedback = false;
    // Experimental committed-path-only physical-register model. Capacities
    // are the initially free entries after architectural mappings are
    // installed (gem5 x86 baseline: 256-38, 256-48, 256-1, 1280-5).
    // It requires the destination-class feature carried by FST v6+ and never
    // fabricates wrong-path allocations. Formal FS inputs currently use v7.
    bool rename_free_list = false;
    // Reconstruct the per-class free list inside the C2 response scoreboard
    // and release mappings only at response-corrected ordered retirement.
    // This is a distinct alternative to the lower-bound free list; production
    // experiments must not enable both models at once.
    bool response_rename_feedback = false;
    std::uint32_t rename_int_free_entries = 218;
    std::uint32_t rename_float_free_entries = 208;
    std::uint32_t rename_vec_free_entries = 255;
    std::uint32_t rename_cc_free_entries = 1275;
    std::uint32_t dispatch_to_issue = 1;
    std::uint32_t fetch_to_decode = 1;
    std::uint32_t decode_to_rename = 1;
    std::uint32_t rename_to_dispatch = 2;
    // Experimental backward free-entry visibility into rename. gem5
    // communicates IQ/LSQ state from IEW and ROB state from commit through
    // time-buffer edges, but charging those edges on top of this interval
    // abstraction regressed the full workload gate. Keep the compatibility
    // default at zero and enable them only for source-alignment ablations.
    std::uint32_t iew_to_rename = 0;
    std::uint32_t commit_to_rename = 0;
    // gem5's OpDesc::opLat already determines producer-ready time in this
    // interval abstraction. These optional extra delays default to zero to
    // avoid charging the IEW time-buffer edge twice.
    std::uint32_t issue_to_execute = 0;
    std::uint32_t execute_to_commit = 0;
    // Core-side minimum issue-to-producer-ready delay for loads/atomics.
    // This is intentionally independent of the Ruby/private-cache response
    // path: response feedback exposes only latency beyond this lower bound.
    std::uint32_t minimum_load_latency = 1;
    // Interval-core ordinary (non-atomic) load FU/producer-ready lower bound. Unset uses
    // minimum_load_latency, preserving legacy profiles and atomic timing.
    // issue_to_execute remains a separate edge when configured nonzero.
    std::optional<std::uint32_t> ordinary_load_latency = std::nullopt;
    // Two-stage sparse feedback: ordinary data response -> visible producer
    // ready, before arbitration in the shared ready/writeback calendar.
    // Zero preserves the legacy response floor. Does not delay the memory
    // callback, MSHR/Sequencer release, page-walk response, store or atomic.
    std::uint32_t load_response_to_ready = 0;
    // Reconstruct response-extended OoO queue lifetimes at each committed
    // interval checkpoint. This is an event model, not a per-cycle scan.
    bool response_queue_feedback = false;
    // Carry response-corrected dispatch back through the finite fetch queue.
    // If UOP i-N has not dispatched, UOP i cannot have left an N-entry fetch
    // queue; the resulting fetch displacement shifts its downstream lower
    // bounds.  This closes a committed-path backpressure edge without
    // fabricating wrong-path UOPs or consuming target timing labels.
    bool response_fetch_queue_feedback = false;
    // Extend response feedback to persistent ROB/LQ/SQ calendars. Loads free
    // LQ entries at ordered commit; stores retain SQ entries until their
    // post-commit memory response.
    bool response_rob_lsq_feedback = false;
    // Sequence-tagged fixed-ring response scoreboard. Unlike the legacy
    // dense ROB/LQ/SQ experiment, this path preserves only the most recent
    // ROB window, carries producer completion across interval boundaries,
    // and performs O(1) capacity checks without scanning target-sized arrays.
    bool response_sparse_scoreboard = false;
    // Incremental block-summary checkpoint for the sparse response path.
    // Keep the entry ROB ring read-only while processing one accepted block,
    // retain only per-UOP completion/retire deltas, then write back the final
    // ROB window in one compact exit transfer. Disabling this switch retains
    // the per-UOP ring writes as an equivalence reference.
    bool response_block_summary = false;
    // Diagnostic-only response-frontier samples at sequence-number milestones.
    // Zero disables sampling. A nonzero stride requires the exact sparse
    // response path plus CPI attribution and never changes target state.
    std::uint32_t response_frontier_audit_stride_uops = 0;
    // Optional inclusive sequence window and single-core filter for a
    // fine-grained frontier audit.  End zero is unbounded; UINT32_MAX selects
    // every core.  Filters are inert unless the stride above is nonzero.
    std::uint64_t response_frontier_audit_begin_sequence = 0;
    std::uint64_t response_frontier_audit_end_sequence = 0;
    std::uint32_t response_frontier_audit_core = ~std::uint32_t{0};
    // When frontier auditing is active, additionally sample each real
    // predictor miss and its immediately following committed UOP.  This keeps
    // long branch-recovery witnesses sparse without changing target timing.
    bool response_branch_recovery_audit = false;
    // Experimental response-causal branch recovery.  A true predictor miss
    // publishes its corrected completion as a persistent frontend floor for
    // younger committed UOPs.  The existing single response traversal carries
    // the resulting fetch delay through rename/dispatch/issue/resources. It
    // does not replay cache/coherence choices already made for the current
    // checkpoint; later checkpoints naturally observe the delayed core time.
    bool response_branch_recovery = false;
    // Experimental partition-invariant response frontier.  ROB completion /
    // retirement, SQ release / store-drain, and ordered commit retain paired
    // lower-bound and response-displacement state.  As that semantic frontier
    // advances, only a displacement common to the complete persistent
    // frontier is folded into the scalar interval gap; the remaining wave
    // stays open across checkpoints or until the final core tail.
    bool response_paired_frontier = false;
    // Reuse producer-computed load/store admission descriptors instead of
    // rescanning every memory event in response feedback. The descriptors are
    // updated only for materialized in-range events, preserving MMIO and
    // atomic event semantics.
    bool response_memory_descriptor = false;
    // Certified read-only, single-line private components use the owner's
    // actual callback instead of exposing an allocated-but-unfilled tag.
    // Kept opt-in until the full-ROI accuracy/throughput gate passes.
    bool response_private_read_services = false;
    bool response_shared_service_constraints = false;
    // Validate the maximum producer stage cycle once, then encode all five
    // Q16 timing fields without repeating identical overflow checks.
    bool response_batch_timing_encode = false;
    // Reallocate issue width, target FU/port occupancy, and writeback only for
    // UOPs in a response causal cone. Independent younger UOPs retain their
    // certified lower-bound slots, preserving OoO bypass without replaying a
    // whole ROB window in target-cycle order.
    bool response_sparse_resource_repair = false;
    // Experimental response availability in the final core timing pass.
    // Functional cache paths and store admission remain canonical by
    // default. This does not enable two-pass Ruby line coalescing.
    bool response_pending_fill = false;
    // Explicit mechanism ablations. The admission/store combination failed
    // the phase-one P99 gate and must not be enabled implicitly. All three
    // controls are inactive when response_pending_fill is false.
    bool response_pending_fill_wait = true;
    bool response_pending_fill_load_admission = false;
    bool response_pending_fill_store_commit = false;
    // Certify a memory-free checkpoint segment as response-inactive and use
    // a reduced state-transition loop.  A failed certificate falls back to
    // the full sparse scoreboard path without committing tentative state.
    bool response_activity_certificate = false;
    // Use a monotone radix calendar for the response-side IQ capacity state.
    // Every IQ transition removes the minimum release and inserts a release
    // no earlier than it, so this changes only the host data structure.
    // Outside the optimized 32..256-entry IQ range, use the binary heap.
    bool response_monotone_iq_calendar = false;
    // Try exact, sequence-aligned 64/32/16-UOP response transfers before the
    // scalar feedback path.  Every dynamic state check is a certificate;
    // failure leaves state untouched and falls back to scalar evaluation.
    bool response_causal_block_transfer = false;
    // Compile the maintained sparse response feature set into a dedicated
    // materialized-UOP kernel.  This changes only host control flow: the
    // dispatch/completion/retire frontier and every queue/event transition
    // remain identical to the generic scalar reference.
    bool response_materialized_uop_fast_kernel = false;
    // Explicit throughput/accuracy tradeoff. Replay every memory and
    // instruction-fetch event, but collapse response propagation through
    // ordinary UOPs into one checkpoint-local tail estimate. Branch/cache
    // events are never sampled; only their detailed OoO response closure is
    // approximated. This is an experimental fast mode and is never enabled by
    // a maintained production profile.
    bool response_event_only_approximation = false;
    // Per-core exact checkpoints used to learn response-penalty cycles per
    // accepted UOP before that core switches permanently to event-only
    // feedback. The calibration state follows the normal TimingFeedback
    // commit/rollback contract, so speculative reweaves cannot train it twice.
    std::uint32_t response_event_only_calibration_checkpoints = 256;
    // Keep one exact response-feedback teacher at this stride and offset.
    // A zero stride disables continuous teaching (useful for directed tests).
    // Non-teacher cores scale their calibrated rate by the teacher's rolling
    // phase change while retaining their own initial per-core rate.
    std::uint32_t response_event_only_teacher_stride = 8;
    std::uint32_t response_event_only_teacher_offset = 7;
    std::uint32_t response_event_only_teacher_window_epochs = 128;
    // Fraction of response-induced completion extension exposed to ordered
    // retirement in the interval abstraction. One is the structural model;
    // smaller values are explicit functional-trace uncertainty experiments.
    double response_retire_exposure = 1.0;
    // A regular store may generate address/data before retirement, but its
    // cache/coherence request becomes visible only after commit eligibility.
    // This experimental time-epoch edge retains atomics at execute time.
    bool store_post_commit_request = false;
    // The captured x86 O3 profile permits only one post-commit store request
    // in flight. Other ISAs may explicitly disable this constraint.
    bool needs_tso = true;
    std::uint32_t integer_alu_units = 6;
    std::uint32_t integer_multiply_units = 2;
    std::uint32_t float_simple_units = 4;
    std::uint32_t float_complex_units = 2;
    std::uint32_t simd_units = 4;
    std::uint32_t predicate_units = 1;
    std::uint32_t memory_units = 4;
    std::uint32_t system_units = 1;
    std::uint32_t cache_load_ports = 200;
    std::uint32_t cache_store_ports = 200;
    std::uint32_t integer_alu_latency = 1;
    std::uint32_t integer_multiply_latency = 3;
    std::uint32_t integer_divide_latency = 1;
    bool integer_alu_pipelined = true;
    bool integer_multiply_pipelined = true;
    bool integer_divide_pipelined = false;
    std::uint32_t float_simple_latency = 2;
    std::uint32_t float_multiply_latency = 4;
    std::uint32_t float_multiply_accumulate_latency = 5;
    std::uint32_t float_misc_latency = 3;
    std::uint32_t float_divide_latency = 12;
    std::uint32_t float_sqrt_latency = 24;
    bool float_simple_pipelined = true;
    bool float_complex_pipelined = true;
    bool float_divide_pipelined = false;
    bool float_sqrt_pipelined = false;
    std::uint32_t simd_latency = 1;
    std::uint32_t predicate_latency = 1;
    std::uint32_t system_latency = 1;
    // A marked syscall is a serializing system UOP.  `system_latency` models
    // execution on the core's system FU; this additional service latency is
    // reserved for a known gem5-SE ABI cost and defaults to zero rather than a
    // workload-fitted proxy.  Blocking time is intentionally not represented
    // here and will be supplied by a later thread-event/scheduler layer.
    std::uint32_t syscall_service_latency = 0;
    // Empty frontend cycles after the syscall retires before the bound thread
    // can fetch again.  This is target timing, not host synchronization cost.
    std::uint32_t syscall_restart_latency = 1;
    // Synthetic per-sysnum syscall cost model.  When enabled, a syscall marker
    // whose recorded syscall number is present in `syscall_cost_table` charges
    // that number's on-core service cycles instead of the scalar
    // `syscall_service_latency`.  Absent numbers fall back to the scalar.  This
    // keeps replay deterministic (trace + table fix the cost) and is the only
    // syscall-cost source at inference time; see docs/syscall-modeling-dual-cpi.md.
    bool syscall_cost_model = false;
    // sysnum -> on-core service cycles.  Populated from a calibration file or
    // config; empty means "always use the scalar fallback".
    std::unordered_map<std::uint64_t, std::uint32_t> syscall_cost_table;

    // Joint active-service/PMU model.  When enabled and a sysnum has a
    // profile, its service_cycles take precedence over the legacy cost table
    // and the remaining fields are accumulated into a separate synthetic
    // kernel PMU domain. A calibrated default profile can cover sysnums that
    // were absent from the per-number calibration table.
    bool syscall_kernel_event_model = false;
    std::unordered_map<std::uint64_t, KernelEventProfile>
        syscall_kernel_event_table;
    bool syscall_kernel_event_default_profile_enabled = false;
    KernelEventProfile syscall_kernel_event_default_profile;

    const SyscallKernelEventProfile*
    syscall_kernel_event_profile(std::uint64_t syscall_number) const {
        if (!syscall_kernel_event_model) return nullptr;
        const auto entry = syscall_kernel_event_table.find(syscall_number);
        if (entry != syscall_kernel_event_table.end()) {
            return &entry->second;
        }
        return syscall_kernel_event_default_profile_enabled
                   ? &syscall_kernel_event_default_profile
                   : nullptr;
    }

    std::uint32_t
    syscall_service_cycles(std::uint64_t syscall_number) const {
        if (const auto* profile =
                syscall_kernel_event_profile(syscall_number)) {
            return profile->service_cycles;
        }
        if (syscall_cost_model) {
            const auto entry = syscall_cost_table.find(syscall_number);
            if (entry != syscall_cost_table.end()) return entry->second;
        }
        return syscall_service_latency;
    }

    // A virtual-page token's first appearance in one functional trace is only
    // a candidate for an active minor fault.  The frozen integer probability
    // accumulator selects events without PRNG or host-order dependence.
    bool page_fault_event_model = false;
    // Reconstruct the cache-state side effect of selected first-touch faults
    // by functionally writing the containing 4 KiB physical page before the
    // user demand. This is deliberately independent of event_model: user-only
    // reports need kernel-created cache state without kernel cycles or PMU.
    bool page_fault_cache_state_model = false;
    // Select a first access exactly when the token's portable virtual page
    // lies in a successful trace-visible mmap range. A separate shared
    // fallback below covers first writes whose VMA creation predates the
    // trace. Both paths require FST `.vmap` companions and complete Linux
    // x86-64 syscall metadata for every trace-visible mmap/munmap.
    bool page_fault_syscall_semantic_model = false;
    // Consume optional initial and ROI-entry guest page state from `.fst.vmap`.
    // The current single-process model shares page state across streams.
    // Known present pages suppress heuristics; known non-present pages select
    // one first touch. A producer-marked fault already in flight at ROI entry
    // keeps page-fill state without adding a new measured kernel event.
    // Unknown pages use the semantic fallback. The initial snapshot is used
    // only while replaying functional warmup; ROI-entry state is authoritative
    // once measurement begins.
    bool page_fault_roi_entry_page_state_model = false;
    // A source-level trace begins after process startup and therefore cannot
    // observe every VMA-creating syscall.  For first writes to pages outside
    // trace-visible mmap ranges, this single frozen probability models the
    // residual COW/demand-zero population.  It is deliberately shared across
    // workloads and is applied only after the semantic selector declines.
    std::uint32_t
        page_fault_syscall_semantic_fallback_write_probability_ppm = 0;
    // A trace-visible allocation syscall arms a separate first-touch channel
    // for the rest of that thread. This distinguishes newly mapped pages from
    // pages that merely appear for the first time inside the measured ROI.
    std::unordered_set<std::uint64_t> page_fault_allocation_syscalls;
    // Zero preserves the legacy unbounded behavior. A positive value limits
    // the allocation channel to first touches within this many trace records
    // of the most recent trace-visible allocation syscall.
    std::uint64_t page_fault_allocation_window_records = 0;
    // Background first-read and first-write probabilities outside the
    // allocation-recency window.
    std::uint32_t page_fault_probability_ppm = 0;
    std::uint32_t page_fault_background_write_probability_ppm = 0;
    std::uint32_t page_fault_allocation_probability_ppm = 0;
    // The scalar allocation probability remains the fallback for syscalls
    // absent from this frozen table. Read/write entries are deliberately
    // shared across workloads; calibration may not key them by workload ID.
    std::uint32_t page_fault_allocation_write_probability_ppm = 0;
    std::unordered_map<std::uint64_t, PageFaultAllocationProbability>
        page_fault_allocation_probability_table;
    KernelEventProfile page_fault_event_profile;

    std::uint32_t page_fault_allocation_probability_for(
        std::uint64_t syscall_number, bool is_write) const {
        const auto entry = page_fault_allocation_probability_table.find(
            syscall_number);
        if (entry != page_fault_allocation_probability_table.end()) {
            return is_write ? entry->second.write_ppm
                            : entry->second.read_ppm;
        }
        return is_write ? page_fault_allocation_write_probability_ppm
                        : page_fault_allocation_probability_ppm;
    }

    // Attributable periodic IRQ background.  One event is emitted per
    // `period_cycles` of foreground active core time, excluding the synthetic
    // IRQ service itself.  This intentionally does not attempt to reproduce
    // the exact interrupt point from a user-only trace.
    bool irq_event_model = false;
    std::uint64_t irq_period_cycles = 0;
    KernelEventProfile irq_event_profile;
    std::uint32_t l1d_mshrs = 16;
    std::uint32_t l2_mshrs = 32;
    std::uint32_t llc_mshrs = 64;
    // CPU-side Ruby Sequencer request capacity. Zero disables the gate for
    // non-Ruby configurations; this is distinct from controller TBE/MSHR
    // capacity (cache.*.mshrs).
    std::uint32_t ruby_sequencer_max_outstanding = 0;
    // Model Ruby Sequencer's per-core, per-cache-line request table. A
    // request that aliases an outstanding line waits for the parent response
    // instead of probing private-cache tags or issuing a second hierarchy
    // request. A write queued behind a read is reissued after that response.
    bool ruby_sequencer_line_coalescing = false;
    // Audit the load-only line-generation model at the final core-side
    // admission edge.  This is a shadow path: it must not change cache,
    // response, retirement, or CPI state.
    bool ruby_line_generation_admission_audit = false;
    // Use the interval core's OoO producer issue for ordinary loads and
    // place the Ruby admission one core cycle later, at execute/address
    // generation. The monotone memory timestamp remains only a checkpoint
    // envelope. Atomics and regular stores retain their separate lifecycle.
    bool ruby_sequencer_load_admission = false;
    std::uint32_t cha_count = 8;
    std::uint32_t noc_one_way_latency = 12;
    // causal_read: peer controller service between snoop arrival and reply.
    std::uint32_t coherence_peer_response_latency = 0;
    std::uint32_t llc_service_cycles = 2;
    // Fixed L3-miss path from the shared-cache lookup through the Ruby
    // directory to MemCtrl admission. Kept separate so an LLC hit does not
    // pay directory/memory-controller protocol stages.
    std::uint32_t directory_memory_latency = 0;
    // DRAM completion to LLC fill visibility/TBE release. Ruby keeps the
    // line transient while the memory response traverses the directory and
    // L3 response path; this is service latency, not DRAM queue time.
    std::uint32_t llc_fill_response_latency = 0;
    bool cha_xor_hash = false;
    double memory_exposure = 1.0;
    bool coherence = true;
    bool inclusive_llc = false;
    bool strict_physical_address = true;
    bool require_virtual_page_token = false;
    bool require_instruction_page_map = false;
    // Instruction PCs are virtual. In `modeled` mode FastSim preserves the
    // configured page offset and deterministically hashes (ASID, VPN, seed)
    // into the configured physical capacity. `trace` consumes `.fst.ifmap`
    // and is intended only for exact-oracle/debug comparisons.
    std::string instruction_address_mode = "modeled";
    std::uint32_t instruction_physical_address_bits = 48;
    std::uint32_t instruction_page_bits = 12;
    std::uint64_t instruction_mapping_seed = 1;
    // The streaming FST converter cannot encode both translations for one
    // memory UOP spanning a 4-KiB page boundary. This narrowly-scoped escape
    // leaves only those provably cross-page UOPs untracked by the DTLB model.
    bool allow_cross_page_without_virtual_token = false;
    // Full-system traces can contain architecturally committed MMIO and gem5
    // pseudo-operation accesses outside guest RAM. When enabled, those UOPs
    // retain core/DTLB timing but bypass cache/coherence/DRAM state.
    bool allow_mmio_escape = false;

    CacheConfig l1i{32ull << 10, 8, 64, 1, ReplacementPolicy::kLru};
    CacheConfig l1d{32ull << 10, 8, 64, 4, ReplacementPolicy::kLru};
    CacheConfig l2{1ull << 20, 8, 64, 12, ReplacementPolicy::kTreePlru};
    CacheConfig llc{64ull << 20, 16, 64, 36, ReplacementPolicy::kTreePlru};
    BranchConfig branch;
    DramConfig dram;
    TlbConfig dtlb;

    void validate() const;

    std::uint64_t frequency_hz(std::uint32_t core) const {
        return core_frequencies_hz.empty()
            ? core_frequency_hz
            : core_frequencies_hz.at(core);
    }
};

// Portable instruction-side address model. It is deliberately stateless so
// per-core decode workers produce identical mappings independent of host
// scheduling order. The result is a modeled physical address, not a claim
// about the guest OS page allocator used by a particular gem5 run.
std::uint64_t modeled_instruction_physical_address(
    const SimulatorConfig& config, std::uint64_t address_space_id,
    std::uint64_t virtual_address);

class KeyValueConfig {
  public:
    static KeyValueConfig load(const std::string& path);
    static KeyValueConfig parse(const std::string& text);

    bool contains(const std::string& key) const;
    std::string get_string(const std::string& key,
                           const std::string& fallback) const;
    std::uint64_t get_u64(const std::string& key,
                          std::uint64_t fallback) const;
    std::uint32_t get_u32(const std::string& key,
                          std::uint32_t fallback) const;
    double get_double(const std::string& key, double fallback) const;
    bool get_bool(const std::string& key, bool fallback) const;

  private:
    std::unordered_map<std::string, std::string> values_;
};

SimulatorConfig load_simulator_config(const std::string& path);

}  // namespace fastsim
