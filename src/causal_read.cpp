#include "fastsim/causal_read.hpp"

#include <algorithm>
#include <array>
#include <deque>
#include <limits>
#include <map>
#include <memory>
#include <optional>
#include <queue>
#include <set>
#include <stdexcept>
#include <string>
#include <tuple>
#include <utility>
#include <vector>

#include "fastsim/cache.hpp"
#include "fastsim/op_traits.hpp"
#include "fastsim/predictor.hpp"

namespace fastsim {

void validate_causal_read_config(const SimulatorConfig& c) {
    const auto require = [](bool ok, const char* message) {
        if (!ok) throw std::invalid_argument(
            std::string("core.model=causal_read: ") + message);
    };
    for (std::uint32_t core = 0; core != c.cores; ++core)
        require(c.frequency_hz(core) == c.reference_frequency_hz,
                "all cores require the reference clock (no DVFS)");
    require(!c.inclusive_llc, "inclusive LLC is not supported yet");
    require(!c.dtlb.enabled && !c.l1i_enabled && !c.fetch_supply_model &&
                c.fetch_buffer_bytes == 0 &&
                !c.fetch_supply_physical_request_ledger &&
                !c.fetch_supply_lower_hierarchy,
            "use the explicit ideal I-side/translation profile: disable "
            "DTLB, L1I, fetch supply and fetch_buffer_bytes");
    require(c.dram.scheduler == "fcfs",
            "only the existing FCFS DRAM service is supported");
    require(!c.cha_xor_hash, "CHA xor hashing is not supported yet");
    require(!c.syscall_cost_model && !c.syscall_kernel_event_model &&
                !c.page_fault_event_model && !c.page_fault_cache_state_model &&
                !c.page_fault_syscall_semantic_model &&
                !c.page_fault_roi_entry_page_state_model && !c.irq_event_model,
            "synthetic OS models are not supported");
    require(!c.interval_private_preview && c.interval_reweave_passes == 1 &&
                !c.interval_causal_timing && !c.interval_response_retime &&
                !c.interval_rob_head_suffix_replay &&
                !c.interval_corrected_suffix_carry &&
                !c.interval_parallel_feedback && !c.response_queue_feedback &&
                !c.response_sparse_scoreboard && !c.response_rob_lsq_feedback &&
                !c.response_fetch_queue_feedback && !c.response_rename_feedback &&
                !c.response_pending_fill && !c.rename_free_list &&
                !c.fu_gap_aware_schedule && !c.store_set_same_pc_feedback &&
                !c.committed_static_dependency_feedback &&
                !c.committed_static_memory_ordering &&
                !c.ruby_sequencer_line_coalescing &&
                c.ruby_sequencer_max_outstanding == 0 &&
                !c.ruby_sequencer_load_admission &&
                !c.ruby_line_generation_admission_audit,
            "legacy replay/feedback, rename and Ruby Sequencer modes must "
            "be disabled; cache miss generations have their own ownership");
    require(c.memory_exposure == 1.0, "memory_exposure must be 1");
    require(c.coherence_peer_response_latency <= (1u << 20),
            "coherence peer response latency exceeds 1048576");
    require(c.iew_to_rename == 0 && c.commit_to_rename == 0,
            "backward time-buffer delays are not supported yet");
    require(c.strict_physical_address, "requires physical addresses");
    require(!c.branch.shadow_rob && !c.branch.population_audit,
            "wrong-path occupancy is not modeled by the committed-path driver");
}

namespace {

constexpr auto never = std::numeric_limits<std::uint64_t>::max();

std::uint64_t add(std::uint64_t a, std::uint64_t b) {
    if (a >= never - b)
        throw std::overflow_error("causal_read cycle/counter overflow");
    return a + b;
}

void integrate(std::uint64_t& sum, std::size_t occupancy,
               std::uint64_t duration) {
    if (occupancy != 0 && duration > (never - sum) / occupancy)
        throw std::overflow_error("causal_read occupancy integral overflow");
    sum += static_cast<std::uint64_t>(occupancy) * duration;
}

// A reply either completes one load fragment or fills the requesting upper
// cache generation. Tokens never point at a movable container element.
struct Sink {
    std::uint64_t sequence = 0;
    std::uint32_t fragment = 0;
    int upper_level = -1;
    std::uint64_t upper_generation = 0;
    std::uint64_t lookup_ready = 0;
    bool write = false;
    std::uint32_t core = 0;
    bool measured = true;
    bool coherent_lease = false;
    // Ownership request is separate from dirty data. Lower-level GETX
    // requests must not dirty cache lines before the architectural write.
    bool write_intent = false;
    bool has_private_data = false;
};

struct Read {
    std::uint64_t line = 0;
    Sink sink;
    bool blocked = false;
    bool transfer_dirty = false;
    bool from_fill = false;
    bool exclusive_response = false;
};

struct Generation {
    std::uint64_t id = 0;
    std::uint64_t leader = 0;
    std::vector<Sink> waiters;
    bool dirty = false;
    std::uint32_t leader_core = 0;
};

enum class EventKind {
    // All already-scheduled releases/responses precede new admissions at t.
    kDramRelease, kReply, kExecutionComplete, kDirtyWriteback, kCoherenceData,
    kCoherenceSnoop, kCoherenceGrant, kCacheArrival, kCacheLookup, kDirectoryArrival,
    kDramArrival, kMemoryExecute
};

struct Event {
    std::uint64_t cycle = 0;
    EventKind kind = EventKind::kExecutionComplete;
    std::uint64_t ticket = 0;
    int level = -1;
    Read read;
    bool operator>(const Event& other) const {
        return std::tie(cycle, kind, ticket) >
               std::tie(other.cycle, other.kind, other.ticket);
    }
};

struct Uop {
    TraceRecord record;
    std::vector<std::uint32_t> dependency_extensions;
    std::uint64_t fetch = 0;
    std::uint64_t stage_ready = 0;
    std::uint64_t dispatch = 0;
    std::uint64_t issue_ready = 0;
    std::uint64_t issue = 0;
    std::uint64_t completion = 0;
    std::uint32_t unresolved = 0;
    std::uint32_t remaining_fragments = 0;
    bool dispatched = false;
    bool issued = false;
    bool complete = false;
    bool branch_miss = false;
    std::uint64_t prediction_sequence = 0;
    std::vector<std::uint64_t> supplemental_producers;
    std::vector<std::uint64_t> dependents;
};

// Store entries outlive their ROB entries. No pointers into retired Uops are
// used by cache responses, forwarding, or the TSO send gate.
struct Store {
    TraceRecord record;
    std::uint64_t commit = 0;
    std::uint32_t remaining = 0;
    std::uint32_t admitted = 0;
    std::uint32_t fragments = 0;
    bool executed = false;
    bool committed = false;
    bool queued = false;
    bool sent = false;
    bool complete = false;
};

enum Port : std::size_t { Fetch, Decode, Rename, Dispatch, Issue,
                          Writeback, Commit, LoadPort, StorePort, PortCount };
struct CacheDomain {
    explicit CacheDomain(const CacheConfig& config) : cache(config) {}
    SetAssociativeCache cache;
    std::map<std::uint64_t, Generation> pending;
    std::deque<Read> waiting;
};
struct DecodedRecord {
    TraceRecord record;
    std::vector<std::uint32_t> dependency_extensions;
};
struct CoreState {
    CoreState(const SimulatorConfig& c, TraceSource* input)
        : source(input), predictor(c.branch), private_cache{CacheDomain(c.l1d), CacheDomain(c.l2)} {
        const auto counts = target_fu_counts(c);
        for (std::size_t pool = 0; pool != counts.size(); ++pool) fu_ready[pool].resize(counts[pool]);
    }
    TraceSource* source;
    BranchPredictor predictor;
    std::array<CacheDomain, 2> private_cache;
    CoreCounters warmup;
    std::uint64_t fetched_count = 0, decoded_count = 0, last_retire_cycle = 0;
    std::uint64_t all_retired = 0, memory_fragments = 0, measurement_begin_cycle = 0;
    std::uint64_t boundary_sequence = 0;
    bool boundary_seen = false;
    std::uint64_t fetch_resume = 0;
    std::optional<std::uint64_t> fetch_blocked_branch;
    std::optional<std::uint64_t> fetch_blocked_serial;
    bool sq_blocked = false, eof = false;
    std::optional<std::uint64_t> source_asid, macro_pc;
    std::deque<std::uint64_t> macro_members;
    std::vector<std::uint64_t> macro_producers;
    std::array<std::shared_ptr<const std::vector<std::uint64_t>>, kStaticRegisterCount> register_writers{};
    std::deque<DecodedRecord> decoded;
    std::map<std::uint64_t, Uop> uops;
    std::map<std::uint64_t, Store> stores;
    std::map<std::uint64_t, bool> executed_loads;
    std::array<std::deque<std::uint64_t>, 3> pipe;
    std::deque<std::uint64_t> rob;
    std::set<std::uint64_t> ready, writeback_ready;
    std::size_t iq = 0, lq = 0;
    std::array<std::uint32_t, PortCount> used{};
    std::array<bool, 3> blocked{}, previous_blocked_interval{};
    std::array<std::vector<std::uint64_t>, kSpeculativeProfilePoolCount> fu_ready;
    std::set<std::uint64_t> exclusive_lines;
};

struct CoherentLine {
    std::deque<Read> waiting;
    std::size_t active = 0;
    bool writer = false;
    std::map<std::uint32_t, std::size_t> holders;
};

class Solver {
  public:
    Solver(const SimulatorConfig& config, const std::vector<TraceSource*>& sources,
           SimulationStats& stats, const CausalMulticoreDramService& service,
           const CausalReadObserver& observer, const CausalDramWriteService& dram_write)
        : c_(config), stats_(stats), out_(stats.causal_read), service_(service), observer_(observer),
          dram_write_(dram_write), traits_(target_op_traits(c_)), shared_(c_.llc),
          cha_ready_(c_.cha_count), llc_active_(c_.cha_count),
          dram_waiting_(c_.dram.channels), dram_active_(c_.dram.channels) {
        if (!service_) throw std::invalid_argument("missing causal DRAM service");
        for (auto* source : sources) {
            if (!source) throw std::invalid_argument("missing causal core source");
            const auto* omitted = source->measurement_boundary_memory_accesses();
            if (omitted && !omitted->empty())
                throw std::invalid_argument("causal_read: omitted boundary accesses require explicit events");
            cores_.push_back(std::make_unique<CoreState>(c_, source));
            cores_.back()->boundary_seen = !source->has_measurement_boundary();
            stats_.functional_warmup_enabled |= source->has_measurement_boundary();
        }
        out_.enabled = true;
    }

    void run() {
        std::uint64_t next_core = 0;
        while (true) {
            const auto next = std::min(next_core, events_.empty() ? never : events_.top().cycle);
            if (next == never) break;
            if (next < now_) throw std::logic_error("causal time moved backward");
            for (active_core_ = 0; active_core_ != cores_.size(); ++active_core_) account_until(next);
            const auto duration = next - now_;
            integrate(out_.mshr_occupancy_cycles[2], shared_.pending.size(), duration);
            for (auto count : dram_active_) integrate(out_.dram_occupancy_cycles, count, duration);
            if (next != budget_cycle_) {
                for (auto& state : cores_) state->used.fill(0);
                budget_cycle_ = next;
            }
            now_ = next;
            next_core_ = never;
            while (!events_.empty() && events_.top().cycle == now_) {
                const auto event = events_.top();
                events_.pop();
                active_core_ = event.read.sink.core;
                ++out_.events_processed;
                handle(event);
            }
            // Rotate same-tick admissions. All cores expose their next live
            // frontier before any shared service is committed at a later tick.
            for (std::size_t i = 0; i != cores_.size(); ++i) {
                active_core_ = (i + now_ % cores_.size()) % cores_.size();
                pump();
                update_maxima();
            }
            coherence_requests();
            cache_requests(2);
            dram_requests();
            // Shared admissions occur after the local pumps. Observe their
            // occupancy before the next event can release a controller slot.
            update_maxima();
            next_core = next_core_;
        }
        std::uint64_t retired = 0, fragments = 0, total_cycles = 0;
        for (active_core_ = 0; active_core_ != cores_.size(); ++active_core_) {
            if (!local().eof || !local().uops.empty() || !events_.empty() ||
                local().iq != 0 || local().lq != 0 || !local().rob.empty() || !local().stores.empty() ||
                !local().executed_loads.empty() || !local().boundary_seen)
                throw std::logic_error("causal_read stalled with live core/boundary state");
            for (std::size_t level = 0; level != 3; ++level)
                if (!domain(level).pending.empty() || !domain(level).waiting.empty())
                    throw std::logic_error("causal_read leaked cache state");
            retired += local().all_retired;
            fragments += local().memory_fragments;
            total_cycles += local().last_retire_cycle;
            core().cycles = core().retired_uops ? local().last_retire_cycle - local().measurement_begin_cycle : 0;
            out_.measurement_begin_cycles.push_back(local().measurement_begin_cycle);
            out_.last_retire_cycles.push_back(local().last_retire_cycle);
            local().predictor.drain();
            stats_.functional_warmup_records += local().warmup.records;
            stats_.functional_warmup_uops += local().warmup.retired_uops;
            stats_.functional_warmup_instructions += local().warmup.retired_instructions;
        }
        for (std::size_t channel = 0; channel != dram_active_.size(); ++channel)
            if (dram_active_[channel] != 0 || !dram_waiting_[channel].empty())
                throw std::logic_error("causal_read leaked controller state");
        if (out_.issued_uops != retired || out_.completed_uops != retired ||
            out_.load_fragments != out_.data_callbacks || out_.store_fragments != out_.store_callbacks ||
            out_.load_fragments + out_.store_fragments != fragments || out_.miss_generations != out_.fills)
            throw std::logic_error("causal_read instruction/response conservation");
        out_.drained_cycle = now_;
        if (!coherence_.empty()) throw std::logic_error("coherence lease leak");
        out_.retire_idle_cycles = total_cycles - out_.retire_active_cycles;
    }

  private:
    CoreState& local() { return *cores_.at(active_core_); }
    const CoreState& local() const { return *cores_.at(active_core_); }
    TraceSource& source() { return *local().source; }
    CoreCounters& core() { return stats_.cores.at(active_core_); }
    CacheDomain& domain(std::size_t level) {
        return level == 2 ? shared_ : local().private_cache.at(level);
    }
    const CacheDomain& domain(std::size_t level) const {
        return level == 2 ? shared_ : local().private_cache.at(level);
    }
    bool measured(std::uint64_t sequence) const {
        return local().boundary_seen && sequence >= local().boundary_sequence;
    }

    void emit(const char* kind, const Read& read, int level = -1,
              std::uint64_t generation = 0, std::uint64_t leader = 0,
              std::uint32_t leader_core = 0) {
        if (observer_) observer_(CausalReadEvent{
            kind, now_, read.sink.sequence, read.sink.fragment, read.line,
            level, generation, leader, read.sink.core, leader_core, read.sink.measured});
    }

    void emit_uop(const char* kind, std::uint64_t sequence) {
        Read read;
        read.sink.sequence = sequence;
        read.sink.core = static_cast<std::uint32_t>(active_core_);
        read.sink.measured = measured(sequence);
        emit(kind, read);
    }

    void schedule(EventKind kind, std::uint64_t cycle, int level, Read read) {
        if (cycle < now_ || cycle == never)
            throw std::logic_error("causal_read scheduled an invalid event time");
        ticket_ = add(ticket_, 1);
        events_.push(Event{cycle, kind, ticket_, level, std::move(read)});
        out_.max_pending_events = std::max<std::uint64_t>(
            out_.max_pending_events, events_.size());
    }

    void consider(std::uint64_t cycle) {
        if (cycle < now_) throw std::logic_error("invalid next core frontier");
        next_core_ = std::min(next_core_, cycle);
    }

    std::size_t front_size() const {
        return local().pipe[0].size() + local().pipe[1].size() + local().pipe[2].size();
    }

    void validate_record(const TraceRecord& record, std::uint64_t ordinal,
                         const std::vector<std::uint32_t>& extra) {
        const auto reject = [this, ordinal](const char* reason) {
            throw std::invalid_argument("causal_read unsupported core " +
                std::to_string(active_core_) + " source record " +
                std::to_string(ordinal) + ": " + reason);
        };
        const std::uint16_t allowed = kRetires | kLoad | kStore | kMicroOp |
            kLastMicroOp | kPhysicalAddress | kVirtualPageToken | kBranch |
            kConditional | kIndirect | kCall | kReturn | kTaken | kBranchOutcomeValid |
            kSerialize;
        if (!record.retires() || (record.flags & ~allowed) != 0 ||
            record.is_syscall())
            reject("requires retiring records without atomic or syscall semantics");
        if (record.is_kernel() && !c_.native_kernel_trace)
            reject("kernel records require native_kernel_trace");
        if (record.is_serializing() && record.is_memory())
            reject("serializing memory operations are not supported yet");
        if (has_flag(record.flags, kLoad) && record.is_write())
            reject("combined read/write requires atomic semantics");
        const auto op = record.canonical_op_class();
        if (!record.is_memory() && !(op <= 55 || (op >= 77 && op <= 87)))
            reject("unsupported non-memory operation class");
        if (has_flag(record.flags, kBranch) &&
            (record.is_memory() || !has_flag(record.flags, kBranchOutcomeValid)))
            reject("requires a non-memory branch with a functional outcome");
        if (record.is_write() && !dram_write_)
            reject("stores require a dirty DRAM writeback service");
        const auto* info = source().static_instruction(record.pc);
        if (!source().complete_dependencies() && record.n_src > record.producer_dists.size() &&
            (!source().static_instruction_operands_complete() || info == nullptr ||
             !info->operand_semantics_valid))
            reject("truncated dependencies require a complete functional operand map");
        for (std::size_t i = 0; i != record.producer_dists.size(); ++i) {
            if (record.producer_dists[i] > ordinal)
                reject("dependency precedes the supplied functional prefix");
            if (i >= record.n_src && record.producer_dists[i] != 0)
                reject("producer slot lies outside n_src");
        }
        if (!extra.empty() && !source().complete_dependencies())
            reject("extended producers require declared complete dependencies");
        for (auto distance : extra)
            if (distance == 0 || distance > ordinal)
                reject("extended dependency precedes the supplied functional prefix");
        if (record.is_memory()) {
            if (!has_flag(record.flags, kPhysicalAddress) || record.size == 0)
                reject("memory access must have a nonempty physical range");
            if (record.address > never - (record.size - 1u))
                reject("physical memory range overflows uint64");
            const auto last_line = (record.address + record.size - 1) / c_.l1d.line_size;
            if (!ram_line(last_line) && !c_.allow_mmio_escape)
                reject("physical memory access exceeds RAM; requires trace.allow_mmio_escape");
        }
        if (info != nullptr &&
            (info->is_memory_barrier() || info->is_locked_rmw()))
            reject("static instruction requires memory ordering not yet modeled");
        const auto asid = source().current_address_space_id();
        if (!local().source_asid) local().source_asid = asid;
        if (*local().source_asid != asid)
            reject("address-space transitions are not supported yet");
    }

    bool read_next(Uop& uop) {
        if (local().decoded.empty() && !local().eof) {
            for (std::uint32_t i = 0; i != c_.chunk_instructions; ++i) {
                TraceRecord next;
                if (!source().next(next)) {
                    if (source().measurement_boundary_pending()) {
                        if (local().boundary_seen)
                            throw std::invalid_argument("duplicate functional measurement boundary");
                        local().boundary_sequence = local().decoded_count;
                        local().boundary_seen = true;
                        source().start_measurement();
                        if (!source().next(next))
                            throw std::invalid_argument("empty measurement phase");
                    } else {
                        if (!local().boundary_seen)
                            throw std::invalid_argument("declared measurement boundary was not supplied");
                        local().eof = true;
                        break;
                    }
                }
                auto extra = source().current_dependency_extensions();
                validate_record(next, local().decoded_count, extra);
                local().decoded_count = add(local().decoded_count, 1);
                local().decoded.push_back(DecodedRecord{next, std::move(extra)});
            }
            if (!local().decoded.empty()) ++stats_.chunks_consumed;
            out_.max_decode_buffer = std::max<std::uint64_t>(
                out_.max_decode_buffer, local().decoded.size());
        }
        if (local().decoded.empty()) return false;
        uop.record = local().decoded.front().record;
        uop.dependency_extensions = std::move(local().decoded.front().dependency_extensions);
        local().decoded.pop_front();
        return true;
    }

    void functional_operands(Uop& uop, std::uint64_t sequence) {
        // Complete dynamic RAW edges supersede the conservative static macro
        // reconstruction. In particular, n_src > 4 does not imply lost edges.
        if (source().complete_dependencies()) {
            ++out_.complete_dependency_uops;
            if (!uop.dependency_extensions.empty()) ++out_.extended_dependency_uops;
            out_.extended_dependency_edges += uop.dependency_extensions.size();
            return;
        }
        if (!source().static_instruction_operands_complete()) return;
        const auto* instruction = source().static_instruction(uop.record.pc);
        if (!instruction || !instruction->operand_semantics_valid)
            throw std::invalid_argument("causal_read: incomplete declared operand map");
        if (!local().macro_pc || *local().macro_pc != uop.record.pc) {
            if (local().macro_pc)
                throw std::invalid_argument("causal_read: missing functional macro boundary");
            local().macro_pc = uop.record.pc;
            local().macro_producers.clear();
            std::set<std::uint64_t> unique;
            for (std::size_t reg = 0; reg != kStaticRegisterCount; ++reg) {
                if (!instruction->reads_register(reg) || !local().register_writers[reg]) continue;
                for (auto producer : *local().register_writers[reg])
                    if (local().uops.count(producer)) unique.insert(producer);
            }
            local().macro_producers.assign(unique.begin(), unique.end());
        }
        if (uop.record.n_src > uop.record.producer_dists.size()) {
            uop.supplemental_producers = local().macro_producers;
            ++out_.operand_completed_uops;
        }
        // Retired members are already ready. Pruning keeps even a long macro
        // bounded by the live pipeline, rather than the functional trace size.
        while (!local().macro_members.empty() && !local().uops.count(local().macro_members.front()))
            local().macro_members.pop_front();
        local().macro_members.push_back(sequence);
        if (!has_flag(uop.record.flags, kMicroOp) ||
            has_flag(uop.record.flags, kLastMicroOp)) {
            auto writers = std::make_shared<const std::vector<std::uint64_t>>(
                local().macro_members.begin(), local().macro_members.end());
            for (std::size_t reg = 0; reg != kStaticRegisterCount; ++reg)
                if (instruction->writes_register(reg)) local().register_writers[reg] = writers;
            local().macro_pc.reset();
            local().macro_members.clear();
        }
    }

    void fetch() {
        if (local().fetch_blocked_branch || local().fetch_blocked_serial) return;
        if (local().fetch_resume > now_) { consider(local().fetch_resume); return; }
        local().predictor.advance_to(now_);
        while (front_size() < c_.fetch_queue_entries &&
               local().used[Fetch] < c_.fetch_width) {
            Uop uop;
            if (!read_next(uop)) break;
            const auto& record = uop.record;
            const auto sequence = local().fetched_count++;
            functional_operands(uop, sequence);
            uop.fetch = now_;
            uop.stage_ready = add(now_, c_.fetch_to_decode);
            if (has_flag(record.flags, kBranch)) {
                BranchCounters delta;
                const auto prediction = c_.branch.speculative_history
                    ? local().predictor.predict_speculative(record, delta, &source())
                    : local().predictor.process(record, delta, &source());
                auto& counters = measured(sequence) ? core() : local().warmup;
                counters.branch += delta;
                if (record.is_kernel()) counters.native_kernel_branch += delta;
                uop.branch_miss = prediction.miss;
                uop.prediction_sequence = prediction.sequence;
                if (prediction.miss) local().fetch_blocked_branch = sequence;
            }
            if (record.is_serializing()) local().fetch_blocked_serial = sequence;
            local().uops.emplace(sequence, std::move(uop));
            local().pipe[0].push_back(sequence);
            ++local().used[Fetch];
            emit_uop("fetch", sequence);
            if (local().fetch_blocked_branch || local().fetch_blocked_serial) break;
        }
    }

    void move_stage(std::size_t index, Port port, std::uint32_t width,
                    std::uint32_t delay, const char* name) {
        auto& queue = local().pipe[index];
        while (!queue.empty()) {
            auto& uop = local().uops.at(queue.front());
            if (uop.stage_ready > now_) { consider(uop.stage_ready); break; }
            if (local().used[port] == width) { consider(add(now_, 1)); break; }
            const auto sequence = queue.front();
            queue.pop_front();
            uop.stage_ready = add(now_, delay);
            local().pipe[index + 1].push_back(sequence);
            ++local().used[port];
            emit_uop(name, sequence);
        }
    }

    void dispatch() {
        auto& queue = local().pipe[2];
        while (!queue.empty()) {
            const auto sequence = queue.front();
            auto& uop = local().uops.at(sequence);
            if (uop.stage_ready > now_) { consider(uop.stage_ready); break; }
            if (local().used[Dispatch] == c_.dispatch_width) {
                consider(add(now_, 1)); break;
            }
            if (local().rob.size() == c_.rob_entries || local().iq == c_.iq_entries ||
                (uop.record.is_memory() && !uop.record.is_write() && local().lq == c_.lq_entries) ||
                (uop.record.is_write() && local().stores.size() == c_.sq_entries)) break;
            queue.pop_front();
            uop.dispatched = true;
            uop.dispatch = now_;
            uop.issue_ready = add(now_, c_.dispatch_to_issue);
            std::set<std::uint64_t> producers;
            for (const auto distance : uop.record.producer_dists) {
                if (distance != 0) producers.insert(sequence - distance);
            }
            for (const auto distance : uop.dependency_extensions)
                producers.insert(sequence - distance);
            uop.dependency_extensions.clear();
            producers.insert(uop.supplemental_producers.begin(),
                             uop.supplemental_producers.end());
            uop.supplemental_producers.clear();
            for (const auto producer : producers) {
                const auto found = local().uops.find(producer);
                if (found != local().uops.end() && !found->second.complete) {
                    found->second.dependents.push_back(sequence);
                    ++uop.unresolved;
                }
            }
            local().rob.push_back(sequence);
            ++local().iq;
            if (uop.record.is_write()) local().stores.emplace(sequence, Store{uop.record});
            else if (uop.record.is_memory()) ++local().lq;
            ++local().used[Dispatch];
            update_maxima();
            emit_uop("dispatch", sequence);
            if (uop.unresolved == 0) {
                local().ready.insert(sequence);
                emit_uop("ready", sequence);
            }
        }
    }

    TargetOpTraits op(const TraceRecord& record) const {
        if (record.is_memory()) return {IntervalFuPool::kMemory, 1, true};
        return traits_.at(static_cast<std::size_t>(record.canonical_op_class()));
    }

    void issue() {
        for (auto it = local().ready.begin(); it != local().ready.end();) {
            const auto sequence = *it;
            auto& uop = local().uops.at(sequence);
            // A non-memory serializing instruction owns the fetch gate until
            // retirement. It executes only after older ROB and committed SQ
            // work has drained; neither a fixed drain cost nor a trace tick is
            // substituted for these actual resource lifetimes.
            if (uop.record.is_serializing() &&
                (local().rob.front() != sequence || !local().stores.empty())) {
                ++it; continue;
            }
            if (uop.issue_ready > now_) {
                consider(uop.issue_ready); ++it; continue;
            }
            if (local().used[Issue] == c_.issue_width) {
                consider(add(now_, 1)); break;
            }
            const auto traits = op(uop.record);
            auto& lanes = local().fu_ready[static_cast<std::size_t>(traits.pool)];
            auto lane = std::min_element(lanes.begin(), lanes.end());
            if (*lane > now_) { consider(*lane); ++it; continue; }
            *lane = add(now_, traits.pipelined ? 1u : traits.latency);
            uop.issued = true;
            uop.issue = now_;
            ++local().used[Issue];
            ++out_.issued_uops;
            it = local().ready.erase(it);
            emit_uop("issue", sequence);
            Read read;
            read.sink.sequence = sequence;
            read.sink.core = static_cast<std::uint32_t>(active_core_);
            read.sink.measured = measured(sequence);
            if (uop.record.is_memory()) {
                const auto first = uop.record.address / c_.l1d.line_size;
                const auto last = (uop.record.address + uop.record.size - 1) /
                                  c_.l1d.line_size;
                const auto fragments = static_cast<std::uint32_t>(last - first + 1);
                if (uop.record.is_write()) {
                    local().stores.at(sequence).remaining = fragments;
                    local().stores.at(sequence).fragments = fragments;
                    out_.store_fragments += fragments;
                } else {
                    uop.remaining_fragments = fragments;
                    out_.load_fragments += fragments;
                }
                // Address generation consumes the memory FU's execution
                // stage for loads as well as stores. Cache admission and
                // store forwarding cannot occur at the issue timestamp.
                schedule(EventKind::kMemoryExecute,
                         add(add(now_, c_.issue_to_execute), traits.latency), 0, read);
            } else {
                --local().iq; // A memory IQ entry is held through data writeback.
                schedule(EventKind::kExecutionComplete,
                         add(add(now_, c_.issue_to_execute), traits.latency),
                         -1, read);
            }
        }
    }

    void writeback() {
        while (!local().writeback_ready.empty()) {
            if (local().used[Writeback] == c_.writeback_width) {
                consider(add(now_, 1)); break;
            }
            const auto sequence = *local().writeback_ready.begin();
            local().writeback_ready.erase(local().writeback_ready.begin());
            auto& uop = local().uops.at(sequence);
            if (uop.complete || !uop.issued || uop.remaining_fragments != 0)
                throw std::logic_error("invalid causal writeback");
            uop.complete = true;
            uop.completion = now_;
            if (uop.record.is_memory()) --local().iq;
            ++local().used[Writeback];
            ++out_.completed_uops;
            emit_uop("writeback", sequence);
            if (uop.branch_miss) {
                if (local().fetch_blocked_branch != sequence)
                    throw std::logic_error("branch recovery lost its fetch owner");
                local().fetch_blocked_branch.reset();
                local().fetch_resume = add(now_, c_.branch.mispredict_penalty);
                emit_uop("branch_resolve", sequence);
                consider(local().fetch_resume);
            }
            for (const auto child : uop.dependents) {
                auto& dependent = local().uops.at(child);
                if (dependent.unresolved == 0)
                    throw std::logic_error("duplicate producer wakeup");
                if (--dependent.unresolved == 0) {
                    local().ready.insert(child);
                    emit_uop("ready", child);
                }
            }
            uop.dependents.clear();
        }
    }

    void retire() {
        while (!local().rob.empty()) {
            const auto sequence = local().rob.front();
            const auto& uop = local().uops.at(sequence);
            if (!uop.complete) break;
            const auto eligible = add(uop.completion, c_.execute_to_commit);
            if (eligible > now_) { consider(eligible); break; }
            if (local().used[Commit] == c_.commit_width) {
                consider(add(now_, 1)); break;
            }
            if (local().used[Commit] == 0) ++out_.retire_active_cycles;
            ++local().used[Commit];
            ++local().all_retired;
            auto& retired = measured(sequence) ? core() : local().warmup;
            if (!measured(sequence)) local().measurement_begin_cycle = now_;
            ++retired.records;
            ++retired.retired_uops;
            if (uop.record.is_kernel()) {
                ++retired.native_kernel_records;
                ++retired.native_kernel_retired_uops;
            }
            if (!has_flag(uop.record.flags, kMicroOp) ||
                has_flag(uop.record.flags, kLastMicroOp)) {
                ++retired.retired_instructions;
                if (uop.record.is_kernel()) ++retired.native_kernel_retired_instructions;
            }
            if (uop.record.is_serializing()) {
                if (local().fetch_blocked_serial != sequence)
                    throw std::logic_error("serialization lost its fetch owner");
                ++retired.serializing_uops;
                local().fetch_blocked_serial.reset();
                local().fetch_resume = std::max(local().fetch_resume, add(now_, 1));
                emit_uop("serialize_release", sequence);
            }
            if (uop.record.is_memory()) {
                if (uop.record.is_write()) {
                    auto& store = local().stores.at(sequence);
                    store.committed = true;
                    store.commit = now_;
                    ++out_.store_uops;
                } else --local().lq;
                ++retired.memory_uops;
                const auto count =
                    (uop.record.address + uop.record.size - 1) / c_.l1d.line_size -
                    uop.record.address / c_.l1d.line_size + 1;
                const auto first = uop.record.address / c_.l1d.line_size;
                const auto ram_lines = c_.dram.size_bytes / c_.l1d.line_size;
                const auto ram_count = first < ram_lines ? std::min(count, ram_lines - first) : 0;
                retired.memory_accesses += ram_count;
                retired.mmio_escape_accesses += count - ram_count;
                if (uop.record.is_kernel()) {
                    ++retired.native_kernel_memory_uops;
                    retired.native_kernel_memory_accesses += ram_count;
                }
                local().memory_fragments += count;
            }
            emit_uop("retire", sequence);
            if (has_flag(uop.record.flags, kBranch) && c_.branch.speculative_history) {
                local().predictor.schedule_commit(uop.prediction_sequence, now_);
                local().predictor.advance_to(now_);
            }
            local().last_retire_cycle = now_;
            local().rob.pop_front();
            local().uops.erase(sequence);
        }
    }

    bool ram_line(std::uint64_t line) const {
        // Match the existing native-FS escape boundary, per cache-line fragment.
        return line < c_.dram.size_bytes / c_.l1d.line_size;
    }

    void store_admitted(const Read& read) {
        auto& store = local().stores.at(read.sink.sequence);
        if (store.admitted++ == 0) emit_uop("store_send", read.sink.sequence);
        store.sent = store.admitted == store.fragments;
        if (store.sent && !local().executed_loads.empty()) consider(now_);
    }

    void fragments(const TraceRecord& record, std::uint64_t sequence,
                   bool forwarded = false) {
        const auto first = record.address / c_.l1d.line_size;
        const auto last = (record.address + record.size - 1) / c_.l1d.line_size;
        for (auto line = first; line <= last; ++line) {
            Read request;
            request.line = line;
            request.sink.sequence = sequence;
            request.sink.core = static_cast<std::uint32_t>(active_core_);
            request.sink.measured = measured(sequence);
            request.sink.fragment = static_cast<std::uint32_t>(line - first);
            request.sink.write = record.is_write();
            request.sink.write_intent = record.is_write();
            if (!ram_line(line)) {
                // Existing allow_mmio_escape approximation: retain core/LSQ
                // lifetime, but create no cache, coherence or DRAM request.
                // This local completion carries no device service latency.
                emit("mmio_escape", request);
                if (request.sink.write) store_admitted(request);
                const auto ready = request.sink.write ? now_ : std::max(now_,
                    add(add(local().uops.at(sequence).issue, c_.issue_to_execute),
                        c_.minimum_load_latency));
                schedule(EventKind::kReply, ready, -1, request);
            } else if (forwarded) schedule(EventKind::kReply, now_, -1, request);
            else domain(0).waiting.push_back(request);
        }
    }

    void send_stores() {
        for (auto& entry : local().stores) {
            auto& store = entry.second;
            if (store.complete) continue;
            if (store.queued) {
                if (c_.needs_tso) break;
                continue;
            }
            if (!store.committed) break;
            if (!store.executed) throw std::logic_error("unexecuted committed store");
            store.queued = true;
            fragments(store.record, entry.first);
            if (c_.needs_tso) break;
        }
    }

    void release_stores() {
        while (!local().stores.empty() && local().stores.begin()->second.complete) {
            const auto& entry = *local().stores.begin();
            out_.store_commit_to_release_cycles += now_ - entry.second.commit;
            emit_uop("sq_release", entry.first);
            local().stores.erase(local().stores.begin());
        }
    }

    void loads_after_address_generation() {
        for (auto it = local().executed_loads.begin(); it != local().executed_loads.end();) {
            const auto sequence = it->first;
            const auto& record = local().uops.at(sequence).record;
            bool wait = false, forward = false;
            std::uint64_t forwarding_store = 0;
            // Resolve from youngest older store. Unknown addresses are held
            // conservatively; functional future addresses cannot act as an
            // oracle disambiguator. No violation/squash prediction is claimed.
            auto older = local().stores.lower_bound(sequence);
            while (older != local().stores.begin()) {
                const auto& entry = *--older;
                const auto& store = entry.second;
                if (store.complete) continue;
                if (!store.executed) { wait = true; break; }
                const auto& st = store.record;
                const auto store_last = st.address + st.size - 1;
                const auto load_last = record.address + record.size - 1;
                if (record.address > store_last || st.address > load_last) continue;
                // All packets of a sent store precede this load at L1. The
                // cache/generation now owns ordering, as opposed to an unsent
                // SQ entry. A split store advances only after every packet.
                if (store.sent) break;
                forward = ram_line(record.address / c_.l1d.line_size) &&
                    !store.sent && st.address <= record.address && store_last >= load_last;
                wait = !forward;
                forwarding_store = entry.first;
                break;
            }
            if (wait) {
                if (!it->second) {
                    it->second = true;
                    ++out_.load_order_waits;
                    emit_uop("load_order_wait", sequence);
                }
                ++it;
                continue;
            }
            if (forward) {
                ++out_.forwarded_loads;
                Read request;
                request.sink.sequence = sequence;
                request.sink.core = static_cast<std::uint32_t>(active_core_);
                request.sink.measured = measured(sequence);
                emit("store_forward", request, -1, 0, forwarding_store, request.sink.core);
            }
            fragments(record, sequence, forward);
            it = local().executed_loads.erase(it);
        }
    }

    std::size_t home(std::uint64_t line) const { return line % c_.cha_count; }
    std::size_t channel(std::uint64_t line) const { return line % c_.dram.channels; }
    CacheCounters& cache_counters(std::size_t level, bool measured_origin) {
        auto& counters = measured_origin ? core() : local().warmup;
        return level == 0 ? counters.l1d : level == 1 ? counters.l2 :
            measured_origin ? stats_.llc : warmup_llc_;
    }
    ChaCounters& cha_counters(const Read& read) {
        return read.sink.measured ? stats_.cha[home(read.line)] : warmup_cha_;
    }
    std::uint32_t latency(std::size_t level) const {
        return domain(level).cache.config().hit_latency;
    }
    std::uint32_t miss_latency(std::size_t level) const {
        const auto& config = domain(level).cache.config();
        return config.miss_request_latency.value_or(config.hit_latency);
    }
    bool peer_copy_or_request(std::uint64_t line, std::uint32_t requester) const {
        for (std::size_t peer = 0; peer != cores_.size(); ++peer) {
            if (peer == requester) continue;
            for (const auto& cache : cores_[peer]->private_cache)
                if (cache.cache.contains(line) || cache.pending.count(line)) return true;
        }
        const auto found = coherence_.find(line);
        if (found != coherence_.end())
            for (const auto& holder : found->second.holders)
                if (holder.first != requester && holder.second != 0) return true;
        return false;
    }
    bool capacity_available(std::size_t level, std::uint64_t line) const {
        if (level == 2) return llc_active_[home(line)] < c_.llc_mshrs;
        return domain(level).pending.size() < (level == 0 ? c_.l1d_mshrs : c_.l2_mshrs);
    }

    void coherence_requests() {
        for (auto it = coherence_.begin(); it != coherence_.end();) {
            auto& line = it->second;
            if (line.active == 0 && line.waiting.empty()) { it = coherence_.erase(it); continue; }
            while (!line.waiting.empty() && !line.writer &&
                   (!line.waiting.front().sink.write || line.active == 0)) {
                auto request = line.waiting.front();
                line.waiting.pop_front();
                active_core_ = request.sink.core;
                ++line.active;
                ++line.holders[request.sink.core];
                line.writer = request.sink.write;
                request.sink.coherent_lease = true;
                ++out_.coherence_transactions;
                emit("coherence_acquire", request, 0);
                // This is an ordering lease, not a network permission reply.
                // Physical GETS/GETX starts only after a private miss/upgrade.
                // Retaining the lease through callback prevents a late fill
                // from resurrecting a copy invalidated by a younger writer.
                schedule(EventKind::kCoherenceGrant, now_, 0, request);
            }
            ++it;
        }
    }

    void directory_request(const Read& request) {
        ++out_.directory_requests;
        emit("directory_request", request, 2);
        bool snoop = false;
        for (std::size_t peer = 0; peer != cores_.size(); ++peer) {
            if (peer == request.sink.core) continue;
            const auto& state = *cores_[peer];
            const bool resident = state.private_cache[0].cache.contains(request.line) ||
                                  state.private_cache[1].cache.contains(request.line);
            snoop |= resident && (request.sink.write_intent ||
                                  state.exclusive_lines.count(request.line) ||
                                  !shared_.cache.contains(request.line));
        }
        if (snoop) {
            schedule(EventKind::kCoherenceSnoop,
                     add(add(now_, miss_latency(2)), c_.noc_one_way_latency), 2, request);
        } else domain(2).waiting.push_back(request);
    }

    void coherence_snoop(const Read& request) {
        bool data = false, dirty = false;
        for (std::size_t peer = 0; peer != cores_.size(); ++peer) {
            if (peer == request.sink.core) continue;
            auto& state = *cores_[peer];
            const bool resident = state.private_cache[0].cache.contains(request.line) ||
                                  state.private_cache[1].cache.contains(request.line);
            data |= resident;
            state.exclusive_lines.erase(request.line);
            for (auto& domain : state.private_cache) {
                if (request.sink.write_intent) {
                    bool was_dirty = false;
                    domain.cache.invalidate(request.line, &was_dirty);
                    dirty |= was_dirty;
                } else dirty |= domain.cache.clear_dirty(request.line);
            }
            if (resident) {
                if (request.sink.write_intent) ++out_.coherence_invalidations;
                emit(request.sink.write_intent ? "coherence_invalidate" : "coherence_downgrade",
                     request, 1, 0, 0, static_cast<std::uint32_t>(peer));
            }
        }
        const auto reply = add(add(now_, c_.coherence_peer_response_latency), c_.noc_one_way_latency);
        if (data && (dirty || !shared_.cache.contains(request.line))) {
            auto transfer = request;
            transfer.transfer_dirty = dirty;
            transfer.sink.coherent_lease = false;
            schedule(EventKind::kCoherenceData,
                     reply, 2, transfer);
        }
        // Peer data/ack reaches the requesting directory transaction before
        // it can issue a response or fall through to a DRAM read.
        schedule(EventKind::kCacheLookup, reply, 2, request);
    }

    void release_coherence(const Read& read) {
        const auto found = coherence_.find(read.line);
        if (found == coherence_.end() || found->second.active == 0 ||
            found->second.writer != read.sink.write)
            throw std::logic_error("duplicate or mismatched coherence response");
        auto& line = found->second;
        --line.active;
        const auto holder = line.holders.find(read.sink.core);
        if (holder == line.holders.end() || holder->second == 0)
            throw std::logic_error("missing coherence lease holder");
        if (--holder->second == 0) line.holders.erase(holder);
        if (read.sink.write) line.writer = false;
        emit("coherence_release", read, 0);
        if (line.active == 0 && line.waiting.empty()) coherence_.erase(found);
    }

    void cache_requests(std::size_t level) {
        auto& queue = domain(level).waiting;
        const auto count = queue.size();
        for (std::size_t i = 0; i != count; ++i) {
            auto read = queue.front();
            queue.pop_front();
            active_core_ = read.sink.core;
            // Re-probe after any ordering/capacity wait; no early mutation
            // of replacement or dirty state is allowed by a proposal.
            const auto probe = domain(level).cache.prepare_lookup(read.line);
            if (level == 0 && c_.coherence && !read.sink.coherent_lease) {
                coherence_[read.line].waiting.push_back(read);
                emit("coherence_queue", read, 0);
                continue;
            }
            if (level == 2 && c_.coherence && read.sink.write_intent &&
                read.sink.has_private_data) {
                // GETX/upgrade already carries a private full-line copy.
                // Even a non-inclusive LLC tag miss needs permission, not a
                // memory read. Private generations remain live until reply.
                auto& counters = cha_counters(read);
                ++counters.requests;
                ++counters.writes;
                ++counters.upgrades;
                read.exclusive_response = true;
                emit("permission_grant", read, 2);
                schedule(EventKind::kReply, add(now_, latency(2)), 2, read);
                continue;
            }
            const bool permission_miss = c_.coherence && level < 2 &&
                read.sink.write_intent && !local().exclusive_lines.count(read.line);
            const auto active = domain(level).pending.find(read.line);
            if ((!probe.hit || permission_miss) && active == domain(level).pending.end() &&
                !capacity_available(level, read.line)) {
                if (!read.blocked) {
                    ++out_.capacity_blocks[level];
                    emit("capacity_wait", read, static_cast<int>(level));
                    read.blocked = true;
                }
                queue.push_back(read);
                continue;
            }
            const auto port = read.sink.write ? StorePort : LoadPort;
            const auto width = read.sink.write ? c_.cache_store_ports : c_.cache_load_ports;
            if (level == 0 && local().used[port] == width) {
                queue.push_back(read);
                consider(add(now_, 1));
                continue;
            }
            if (level == 0) {
                ++local().used[port];
                if (read.sink.write) store_admitted(read);
            }
            CacheCounters llc_probe;
            const auto committed = domain(level).cache.commit_probe(
                probe, read.sink.write && !permission_miss,
                level == 2 ? llc_probe : cache_counters(level, read.sink.measured));
            if (!committed) throw std::logic_error("immediate cache proposal stale");
            read.sink.lookup_ready = add(now_, latency(level));
            if (level == 2) {
                auto& counters = cha_counters(read);
                ++counters.requests;
                ++counters.reads;
                ++cache_counters(2, read.sink.measured).accesses;
                if (probe.hit) { ++counters.llc_hits; ++cache_counters(2, read.sink.measured).hits; }
                else if (active != domain(level).pending.end()) ++counters.llc_merged_misses;
                else { ++counters.llc_misses; ++cache_counters(2, read.sink.measured).misses; }
            }
            if (probe.hit && !permission_miss) {
                read.exclusive_response = c_.coherence && (level == 2
                    ? !peer_copy_or_request(read.line, read.sink.core)
                    : local().exclusive_lines.count(read.line) != 0);
                if (level == 0 && read.sink.write && c_.coherence) {
                    ++out_.exclusive_store_hits;
                    emit("exclusive_store_hit", read, 0);
                }
                emit("hit", read, static_cast<int>(level));
                schedule(EventKind::kReply, add(now_, latency(level)),
                         static_cast<int>(level), read);
            } else if (active != domain(level).pending.end()) {
                ++out_.merged_misses[level];
                active->second.waiters.push_back(read.sink);
                active->second.dirty |= read.sink.write;
                emit("merge", read, static_cast<int>(level),
                     active->second.id, active->second.leader, active->second.leader_core);
            } else {
                generation_ = add(generation_, 1);
                domain(level).pending.emplace(read.line, Generation{
                    generation_, read.sink.sequence, {read.sink}, read.sink.write, read.sink.core});
                if (level == 2) ++llc_active_[home(read.line)];
                ++out_.miss_generations[level];
                emit("miss", read, static_cast<int>(level),
                     generation_, read.sink.sequence, read.sink.core);
                if (probe.hit && permission_miss) {
                    if (level == 0) ++out_.permission_upgrades;
                    emit("permission_request", read, static_cast<int>(level),
                         generation_, read.sink.sequence, read.sink.core);
                }
                Read lower{read.line, Sink{read.sink.sequence, read.sink.fragment,
                                          static_cast<int>(level), generation_}};
                lower.sink.core = read.sink.core;
                lower.sink.measured = read.sink.measured;
                lower.sink.write_intent = read.sink.write_intent;
                lower.sink.has_private_data = read.sink.has_private_data || (level < 2 && probe.hit);
                auto arrival = add(now_, miss_latency(level));
                if (level == 1) arrival = add(arrival, c_.noc_one_way_latency);
                if (level == 2) arrival = add(arrival, c_.directory_memory_latency);
                schedule(level == 2 ? EventKind::kDramArrival : EventKind::kCacheArrival,
                         arrival, static_cast<int>(level + 1), lower);
            }
        }
    }

    void deliver(const Read& read) {
        active_core_ = read.sink.core;
        if (read.sink.upper_level >= 0) {
            finish_fill(static_cast<std::size_t>(read.sink.upper_level), read);
            return;
        }
        if (read.sink.coherent_lease) release_coherence(read);
        if (read.sink.write) {
            auto& store = local().stores.at(read.sink.sequence);
            if (!store.committed || store.admitted == 0 || store.remaining == 0)
                throw std::logic_error("invalid store fragment response");
            ++out_.store_callbacks;
            emit("store_response", read, ram_line(read.line) ? 0 : -1);
            if (--store.remaining == 0) store.complete = true;
            release_stores();
            return;
        }
        auto& uop = local().uops.at(read.sink.sequence);
        if (uop.remaining_fragments == 0)
            throw std::logic_error("duplicate load fragment response");
        ++out_.data_callbacks;
        emit("data", read, ram_line(read.line) ? 0 : -1);
        if (--uop.remaining_fragments == 0) {
            schedule(EventKind::kExecutionComplete,
                     std::max(now_, add(uop.issue, c_.minimum_load_latency)),
                     -1, read);
        }
    }

    void respond(std::size_t level, const Read& read) {
        active_core_ = read.sink.core;
        const auto ready = std::max(read.sink.lookup_ready, add(now_,
            read.from_fill ? domain(level).cache.config().fill_response_latency : 0));
        if (ready > now_) {
            auto pending = read;
            pending.from_fill = false;
            schedule(EventKind::kReply, ready, static_cast<int>(level), pending);
            return;
        }
        emit("cache_response", read, static_cast<int>(level));
        if (level == 2 && c_.noc_one_way_latency != 0)
            schedule(EventKind::kReply, add(now_, c_.noc_one_way_latency), -1, read);
        else deliver(read);
    }

    void finish_fill(std::size_t level, const Read& read) {
        const auto found = domain(level).pending.find(read.line);
        if (found == domain(level).pending.end() ||
            found->second.id != read.sink.upper_generation)
            throw std::logic_error("stale or duplicate cache fill generation");
        auto generation = std::move(found->second);
        domain(level).pending.erase(found);
        if (level == 2) --llc_active_[home(read.line)];
        const bool discarded = level == 0 && !domain(1).cache.contains(read.line);
        const auto fill = discarded ? CacheAccessResult{} :
            domain(level).cache.complete_fill(read.line, generation.dirty, cache_counters(level, read.sink.measured));
        if (c_.coherence && level < 2 && !discarded) {
            // Revalidate a grant after its return flight. Another reader may
            // have arrived while the exclusive response was in flight; its
            // live lease/pending fill prevents two private E copies.
            if (read.exclusive_response && !peer_copy_or_request(read.line, read.sink.core)) {
                local().exclusive_lines.insert(read.line);
                emit("permission_exclusive", read, static_cast<int>(level));
            } else {
                // A new shared fill must not silently revoke existing
                // private-L2 ownership during an L2->L1 return. In particular
                // dirty E/M data still needs a real snoop before LLC may
                // supply another reader. Only snoop/eviction revokes that
                // existing authority; this fill does not acquire new E.
                emit("permission_shared", read, static_cast<int>(level));
            }
        }
        // Local fill releases only this generation. The upper generation
        // remains allocated until its separately scheduled response arrives.
        evict(level, fill, read);
        ++out_.fills[level];
        emit("fill", read, static_cast<int>(level), generation.id, generation.leader, generation.leader_core);
        if (discarded) {
            ++out_.discarded_l1_fills;
            emit("fill_discard", read, 0, generation.id, generation.leader, generation.leader_core);
            // The incoming full line carries the store's bytes even when an
            // inclusive L2 eviction prevents keeping an L1 copy. Preserve it
            // in the writeback path instead of dropping the committed write.
            if (generation.dirty) dirty_writeback(0, read.line, read);
        }
        for (const auto& waiter : generation.waiters) {
            Read reply{read.line, waiter};
            reply.from_fill = true;
            reply.exclusive_response = c_.coherence && (level == 2
                ? !peer_copy_or_request(read.line, waiter.core)
                : local().exclusive_lines.count(read.line) != 0);
            respond(level, reply);
        }
    }

    void dirty_writeback(std::size_t level, std::uint64_t line, Read origin) {
        origin.line = line;
        origin.sink.upper_level = -1;
        origin.sink.write = false;
        origin.sink.coherent_lease = false;
        ++out_.dirty_writebacks[level];
        emit("dirty_evict", origin, static_cast<int>(level));
        const auto transfer = level == 0 ? 0u : c_.noc_one_way_latency;
        schedule(EventKind::kDirtyWriteback, add(now_, transfer),
                 static_cast<int>(level + 1), origin);
    }

    void evict(std::size_t level, const CacheAccessResult& fill, const Read& origin) {
        if (!fill.evicted) return;
        bool dirty = fill.evicted_dirty;
        if (level == 1) {
            local().exclusive_lines.erase(fill.evicted_line);
            bool upper_dirty = false;
            domain(0).cache.invalidate(fill.evicted_line, &upper_dirty);
            dirty |= upper_dirty;
        }
        if (dirty) dirty_writeback(level, fill.evicted_line, origin);
    }

    void accept_writeback(std::size_t level, const Read& request) {
        emit("dirty_accept", request, static_cast<int>(level));
        if (level == 3) {
            if (!dram_write_) throw std::logic_error("missing DRAM writeback service");
            dram_write_(now_, request.line);
            ++cha_counters(request).dram_writes;
            return;
        }
        // This packet already contains a full cache line; no read-for-fill is
        // required. Keep any older demand generation alive for its callbacks.
        const auto active = domain(level).pending.find(request.line);
        if (active != domain(level).pending.end()) active->second.dirty = true;
        const auto fill = domain(level).cache.complete_fill(
            request.line, true, cache_counters(level, request.sink.measured));
        evict(level, fill, request);
    }

    void dram_requests() {
        for (std::size_t ch = 0; ch != dram_waiting_.size(); ++ch) {
            auto& queue = dram_waiting_[ch];
            while (!queue.empty() && dram_active_[ch] < c_.dram.read_buffer_size) {
                const auto read = queue.front();
                queue.pop_front();
                ++dram_active_[ch];
                const auto result = service_(now_, read.line, read.sink.core,
                                             read.sink.sequence, read.sink.fragment);
                if (result.command < now_ || result.data_ready < result.command ||
                    result.response < result.data_ready)
                    throw std::logic_error("DRAM service violated causal ordering");
                auto& counters = cha_counters(read);
                ++counters.dram_reads;
                ++counters.llc_unique_fills;
                counters.queue_cycles += result.queue_cycles;
                emit("dram_admit", read, 3);
                schedule(EventKind::kDramRelease, result.data_ready, 3, read);
                schedule(EventKind::kReply,
                         add(result.response, c_.llc_fill_response_latency), -1, read);
            }
            for (auto& read : queue) {
                if (!read.blocked) {
                    read.blocked = true;
                    ++out_.dram_capacity_blocks;
                    emit("capacity_wait", read, 3);
                }
            }
        }
    }

    void handle(const Event& event) {
        switch (event.kind) {
          case EventKind::kDramRelease:
            if (dram_active_[channel(event.read.line)] == 0)
                throw std::logic_error("duplicate controller release");
            --dram_active_[channel(event.read.line)];
            emit("dram_release", event.read, 3);
            break;
          case EventKind::kReply:
            if (event.level >= 0) respond(static_cast<std::size_t>(event.level), event.read);
            else deliver(event.read);
            break;
          case EventKind::kExecutionComplete:
            if (!local().writeback_ready.insert(event.read.sink.sequence).second)
                throw std::logic_error("duplicate execution completion");
            // A load reaches this point only after all fragment callbacks
            // and its core completion bound. WB bandwidth can still delay
            // producer wakeup and IQ release; expose both boundaries.
            emit_uop("completion_ready", event.read.sink.sequence);
            break;
          case EventKind::kCacheArrival:
            if (event.level == 2) {
                const auto cha = home(event.read.line);
                const auto start = std::max(now_, cha_ready_[cha]);
                cha_ready_[cha] = add(start, c_.llc_service_cycles);
                cha_counters(event.read).queue_cycles += start - now_;
                if (c_.coherence) {
                    // Shared service admission is the directory entry point.
                    // Use a distinct arrival event so capacity retries and
                    // post-snoop lookups cannot repeat the transaction.
                    schedule(EventKind::kDirectoryArrival, start, 2, event.read);
                } else schedule(EventKind::kCacheLookup, start, 2, event.read);
            } else domain(static_cast<std::size_t>(event.level)).waiting.push_back(event.read);
            break;
          case EventKind::kCacheLookup:
            domain(static_cast<std::size_t>(event.level)).waiting.push_back(event.read);
            break;
          case EventKind::kDirectoryArrival:
            directory_request(event.read);
            break;
          case EventKind::kDramArrival:
            dram_waiting_[channel(event.read.line)].push_back(event.read);
            break;
          case EventKind::kMemoryExecute: {
            const auto& record = local().uops.at(event.read.sink.sequence).record;
            if (record.is_write()) {
                local().stores.at(event.read.sink.sequence).executed = true;
                schedule(EventKind::kExecutionComplete, now_, -1, event.read);
            } else local().executed_loads.emplace(event.read.sink.sequence, false);
            emit_uop("execute", event.read.sink.sequence);
            break;
          }
          case EventKind::kDirtyWriteback:
            accept_writeback(static_cast<std::size_t>(event.level), event.read);
            break;
          case EventKind::kCoherenceSnoop:
            coherence_snoop(event.read);
            break;
          case EventKind::kCoherenceData: {
            const auto fill = shared_.cache.complete_fill(event.read.line,
                event.read.transfer_dirty, cache_counters(2, event.read.sink.measured));
            evict(2, fill, event.read);
            ++out_.coherence_data_transfers;
            emit("coherence_data", event.read, 2);
            break;
          }
          case EventKind::kCoherenceGrant:
            emit("coherence_grant", event.read, 0);
            domain(0).waiting.push_back(event.read);
            break;
        }
    }

    void pump() {
        ++out_.core_pumps;
        writeback();
        retire();
        fetch();
        move_stage(0, Decode, c_.decode_width, c_.decode_to_rename, "decode");
        move_stage(1, Rename, c_.rename_width, c_.rename_to_dispatch, "rename");
        dispatch();
        issue();
        // Forwarding examines only stores whose address/data have executed.
        // A committed store is sent after this check, as in an LSQ where the
        // unsent store buffer can still satisfy a younger load.
        loads_after_address_generation();
        send_stores();
        // Lower-level releases have already run. Cached hits bypass a full
        // miss table, and blocked requests re-probe the current state.
        for (std::size_t level = 0; level != 2; ++level) cache_requests(level);
        // An ALU issue can free IQ space after this pump's dispatch phase.
        // Settle that same-cycle edge without waiting for the ALU response.
        if (!local().pipe[2].empty() && local().used[Dispatch] < c_.dispatch_width) {
            const auto& head = local().uops.at(local().pipe[2].front());
            if (head.stage_ready <= now_ && local().rob.size() < c_.rob_entries &&
                local().iq < c_.iq_entries &&
                (!head.record.is_memory() || (head.record.is_write()
                    ? local().stores.size() < c_.sq_entries : local().lq < c_.lq_entries)))
                consider(now_);
        }
        if (!local().fetch_blocked_branch && !local().fetch_blocked_serial &&
            (!local().eof || !local().decoded.empty()) &&
            front_size() < c_.fetch_queue_entries)
            consider(std::max(local().fetch_resume,
                local().used[Fetch] == c_.fetch_width ? add(now_, 1) : now_));
        update_blocked();
    }

    void update_blocked() {
        std::array<bool, 3> blocked{};
        local().sq_blocked = false;
        if (!local().pipe[2].empty()) {
            const auto& uop = local().uops.at(local().pipe[2].front());
            if (uop.stage_ready <= now_ && local().used[Dispatch] < c_.dispatch_width) {
                blocked = {local().rob.size() == c_.rob_entries, local().iq == c_.iq_entries,
                           uop.record.is_memory() && !uop.record.is_write() &&
                               local().lq == c_.lq_entries};
                local().sq_blocked = uop.record.is_write() && local().stores.size() == c_.sq_entries;
            }
        }
        local().blocked = blocked;
    }

    void account_until(std::uint64_t next) {
        const auto duration = next - now_;
        const std::array<std::size_t, 3> sizes{local().rob.size(), local().iq, local().lq};
        for (std::size_t i = 0; i != sizes.size(); ++i) {
            if (duration != 0) {
                if (local().blocked[i] && !local().previous_blocked_interval[i])
                    ++out_.dispatch_block_episodes[i];
                local().previous_blocked_interval[i] = local().blocked[i];
            }
            integrate(out_.queue_occupancy_cycles[i], sizes[i], duration);
            integrate(out_.dispatch_blocked_cycles[i], local().blocked[i] ? 1u : 0u, duration);
            if (i != 2) integrate(out_.mshr_occupancy_cycles[i], domain(i).pending.size(), duration);
        }

        integrate(out_.sq_occupancy_cycles, local().stores.size(), duration);
        integrate(out_.sq_dispatch_blocked_cycles, local().sq_blocked ? 1u : 0u, duration);
    }

    void update_maxima() {
        auto& queues = stats_.o3.at(active_core_);
        queues.rob_max_occupancy = std::max<std::uint64_t>(queues.rob_max_occupancy, local().rob.size());
        queues.iq_max_occupancy = std::max<std::uint64_t>(queues.iq_max_occupancy, local().iq);
        queues.lq_max_occupancy = std::max<std::uint64_t>(queues.lq_max_occupancy, local().lq);
        queues.sq_max_occupancy = std::max<std::uint64_t>(queues.sq_max_occupancy, local().stores.size());
        out_.max_live_uops = std::max<std::uint64_t>(out_.max_live_uops, local().uops.size());
        for (std::size_t i = 0; i != 3u; ++i)
            out_.max_mshrs[i] = std::max<std::uint64_t>(out_.max_mshrs[i], domain(i).pending.size());
        std::uint64_t active = 0;
        for (const auto count : dram_active_) active += count;
        out_.max_dram_outstanding = std::max(out_.max_dram_outstanding, active);
    }

    const SimulatorConfig& c_;
    SimulationStats& stats_;
    CausalReadCounters& out_;
    const CausalMulticoreDramService& service_;
    const CausalReadObserver& observer_;
    const CausalDramWriteService& dram_write_;
    const std::array<TargetOpTraits, 128> traits_;
    std::vector<std::unique_ptr<CoreState>> cores_;
    std::size_t active_core_ = 0;
    std::uint64_t now_ = 0, next_core_ = never, budget_cycle_ = never;
    std::uint64_t ticket_ = 0, generation_ = 0;
    std::priority_queue<Event, std::vector<Event>, std::greater<Event>> events_;
    CacheDomain shared_;
    std::map<std::uint64_t, CoherentLine> coherence_;
    CacheCounters warmup_llc_;
    ChaCounters warmup_cha_;
    std::vector<std::uint64_t> cha_ready_;
    std::vector<std::size_t> llc_active_;
    std::vector<std::deque<Read>> dram_waiting_;
    std::vector<std::size_t> dram_active_;

};

}  // namespace

void run_causal_multicore(const SimulatorConfig& config,
                         const std::vector<TraceSource*>& sources,
                         SimulationStats& stats,
                         const CausalMulticoreDramService& dram_service,
                         const CausalReadObserver& observer,
                         const CausalDramWriteService& dram_write) {
    if (config.core_model != "causal_read")
        throw std::invalid_argument("run_causal_multicore requires core.model=causal_read");
    config.validate();
    if (sources.size() != config.cores || stats.cores.size() != config.cores ||
        stats.o3.size() != config.cores || stats.cha.size() != config.cha_count ||
        stats.causal_read.enabled || stats.llc.accesses != 0 ||
        std::any_of(stats.cores.begin(), stats.cores.end(), [](const auto& c) { return c.records != 0; }))
        throw std::invalid_argument("causal_read requires one source per core and fresh statistics");
    Solver(config, sources, stats, dram_service, observer, dram_write).run();
}

void run_causal_read(const SimulatorConfig& config, TraceSource& source,
                     SimulationStats& stats, const CausalReadDramService& dram_service,
                     const CausalReadObserver& observer,
                     const CausalDramWriteService& dram_write) {
    const CausalMulticoreDramService service = [&](auto arrival, auto line, auto, auto seq, auto fragment) {
        return dram_service(arrival, line, seq, fragment);
    };
    if (!dram_service) throw std::invalid_argument("missing causal DRAM service");
    run_causal_multicore(config, {&source}, stats, service, observer, dram_write);
}

}  // namespace fastsim
