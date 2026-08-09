#pragma once

#include <cstddef>
#include <cstdint>
#include <string>
#include <unordered_map>

namespace fastsim {

enum class ReplacementPolicy {
    kLru,
    kTreePlru,
};

struct CacheConfig {
    std::uint64_t size_bytes = 0;
    std::uint32_t associativity = 1;
    std::uint32_t line_size = 64;
    std::uint32_t hit_latency = 1;
    ReplacementPolicy replacement = ReplacementPolicy::kLru;
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
    std::uint32_t mispredict_penalty = 16;
    // Anonymous wrong-path occupancy derived from the functional branch miss
    // and target pipeline geometry. It never invents wrong-path addresses or
    // operation classes; it only delays correct-path rename while the bounded
    // shadow ROB population drains after squash.
    bool shadow_rob = false;
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
    // Experimental source-aligned dirty-writeback path. When disabled, LLC
    // dirty victims retain the historical behavior and immediately update
    // the DRAM calendar. When enabled, each channel buffers writebacks and
    // demand reads retain priority until the gem5-style high watermark and
    // minimum-turnaround conditions request a bounded write drain.
    bool separate_write_queue = false;
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
    std::uint32_t entries = 64;
    std::uint32_t hit_latency = 0;
    std::string miss_model = "timing_walk";
    std::uint32_t page_walk_latency = 60;
    // gem5's x86 timing walker has one active walk and queues followers.
    // Coalescing remains an explicit opt-in approximation for other targets;
    // the gem5-aligned default mirrors the source TODO/no-coalescing path.
    std::uint32_t page_walkers = 1;
    bool coalesce_misses = false;
};

struct SimulatorConfig {
    std::uint32_t cores = 64;
    // Per-core producers decode this many retiring uops per resident chunk.
    std::uint32_t chunk_instructions = 4096;
    std::uint32_t lookahead_chunks = 2;
    std::string core_model = "scalar";
    // The legacy frontier scheduler advances to the smallest per-core UOP
    // proposal.  The time_epoch scheduler instead treats chunk_instructions
    // as an internal decode microbatch and extends per-core lookahead until a
    // common simulated-time epoch is covered.
    std::uint32_t interval_target_uops = 256;
    std::uint32_t interval_max_cycles = 1024;
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
    // Transactionally defer the per-core suffix starting at the first memory
    // request whose response-corrected issue exceeds the time-epoch horizon.
    // Every retry restores the same epoch-entry target state, so response gap
    // is never compounded across repair passes.
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
    // Experimental committed-path-only physical-register model. Capacities
    // are the initially free entries after architectural mappings are
    // installed (gem5 x86 baseline: 256-38, 256-48, 256-1, 1280-5).
    // It requires FST v6 destination class counts and never fabricates
    // wrong-path allocations.
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
    // Reconstruct response-extended OoO queue lifetimes at each committed
    // interval checkpoint. This is an event model, not a per-cycle scan.
    bool response_queue_feedback = false;
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
    // Reuse producer-computed load/store admission descriptors instead of
    // rescanning every memory event in response feedback. The descriptors are
    // updated only for materialized in-range events, preserving MMIO and
    // atomic event semantics.
    bool response_memory_descriptor = false;
    // Validate the maximum producer stage cycle once, then encode all five
    // Q16 timing fields without repeating identical overflow checks.
    bool response_batch_timing_encode = false;
    // Reallocate issue width, target FU/port occupancy, and writeback only for
    // UOPs in a response causal cone. Independent younger UOPs retain their
    // certified lower-bound slots, preserving OoO bypass without replaying a
    // whole ROB window in target-cycle order.
    bool response_sparse_resource_repair = false;
    // Certify a memory-free checkpoint segment as response-inactive and use
    // a reduced state-transition loop.  A failed certificate falls back to
    // the full sparse scoreboard path without committing tentative state.
    bool response_activity_certificate = false;
    // Fraction of response-induced completion extension exposed to ordered
    // retirement in the interval abstraction. One is the structural model;
    // smaller values are explicit functional-trace uncertainty experiments.
    double response_retire_exposure = 1.0;
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

    std::uint32_t
    syscall_service_cycles(std::uint64_t syscall_number) const {
        if (syscall_cost_model) {
            const auto entry = syscall_cost_table.find(syscall_number);
            if (entry != syscall_cost_table.end()) return entry->second;
        }
        return syscall_service_latency;
    }
    std::uint32_t l1d_mshrs = 16;
    std::uint32_t l2_mshrs = 32;
    std::uint32_t llc_mshrs = 64;
    // CPU-side Ruby Sequencer request capacity. Zero disables the gate for
    // non-Ruby configurations; this is distinct from controller TBE/MSHR
    // capacity (cache.*.mshrs).
    std::uint32_t ruby_sequencer_max_outstanding = 0;
    std::uint32_t cha_count = 8;
    std::uint32_t noc_one_way_latency = 12;
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
    // The streaming FST converter cannot encode both translations for one
    // memory UOP spanning a 4-KiB page boundary. This narrowly-scoped escape
    // leaves only those provably cross-page UOPs untracked by the DTLB model.
    bool allow_cross_page_without_virtual_token = false;
    // Full-system traces can contain architecturally committed MMIO and gem5
    // pseudo-operation accesses outside guest RAM. When enabled, those UOPs
    // retain core/DTLB timing but bypass cache/coherence/DRAM state.
    bool allow_mmio_escape = false;

    CacheConfig l1d{32ull << 10, 8, 64, 4, ReplacementPolicy::kLru};
    CacheConfig l2{1ull << 20, 8, 64, 12, ReplacementPolicy::kTreePlru};
    CacheConfig llc{64ull << 20, 16, 64, 36, ReplacementPolicy::kTreePlru};
    BranchConfig branch;
    DramConfig dram;
    TlbConfig dtlb;

    void validate() const;
};

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
