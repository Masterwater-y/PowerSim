// quantum.cc —— LocalRefSim / Coordinator 的实现（D.0 后真私有）
//
// 见 include/quantum.hpp 的设计说明：D.0 后 LocalRefSim 持私有 CoreLocal，
// Coordinator 持唯一 SharedState；probe()/probeIFetch() 直接调
// stepImpl/stepIFetchImpl，不再走中心化 Simulator 中转。
//
// 行为不变性：
//   - 4 核串行调用顺序下，stepImpl(local_, shared_) 与原
//     Simulator::step 在 5K Δt=1 smoke 上 bit-exact（守门测试）。
//   - 共享侧字段（mesi_before/sharer_bucket/dir/llc_set_*）仍由
//     stepImpl 直接写 SharedState；probe/commit 暂未分轨（D.1 工作）。
//
#include "quantum.hpp"

#include <utility>

namespace mesi_ref {
namespace quantum {

// =========================== LocalRefSim ===========================

LocalRefSim::LocalRefSim(Coordinator *coord, uint32_t core_id)
    : coord_(coord), core_id_(core_id),
      local_(core_id, coord->cfg()) {}

OracleResult LocalRefSim::probe(uint64_t paddr, bool is_store, uint16_t size,
                                uint64_t seq, uint32_t thread_id) {
    Event ev;
    ev.seq = seq;
    ev.core_id = core_id_;
    ev.thread_id = thread_id;
    ev.cacheline_addr = paddr & ~uint64_t(63);
    ev.is_store = is_store;
    ev.size = size;

    auto &shared = coord_->shared();

    OracleResult r;
    r.d = stepImpl(local_, shared, ev);
    r.is_ifetch = false;
    r.cl = ev.cacheline_addr;
    r.core_id = core_id_;
    r.seq = seq;
    r.is_store = is_store;
    r.shared_authoritative = true;

    coord_->accumulateD(core_id_, r.d, is_store);
    return r;
}

OracleResult LocalRefSim::probeIFetch(uint64_t vaddr_cl) {
    auto &shared = coord_->shared();

    OracleResult r;
    r.i = stepIFetchImpl(local_, shared, core_id_, vaddr_cl);
    r.is_ifetch = true;
    r.cl = vaddr_cl & ~uint64_t(63);
    r.core_id = core_id_;
    r.seq = 0;
    r.is_store = false;
    r.shared_authoritative = true;

    coord_->accumulateI(core_id_, r.i);
    return r;
}

LineDelta LocalRefSim::commit(uint64_t paddr, bool is_store, uint16_t /*size*/,
                              uint64_t seq, uint32_t /*thread_id*/) {
    LineDelta d{};
    d.cl = paddr & ~uint64_t(63);
    d.core_id = core_id_;
    d.seq = seq;
    d.is_store = is_store;
    d.is_ifetch = false;
    return d;
}

LineDelta LocalRefSim::commitIFetch(uint64_t vaddr_cl) {
    LineDelta d{};
    d.cl = vaddr_cl & ~uint64_t(63);
    d.core_id = core_id_;
    d.seq = 0;
    d.is_store = false;
    d.is_ifetch = true;
    return d;
}

// =========================== Coordinator ===========================

namespace {

void mergeCounterMap(std::unordered_map<uint32_t, uint64_t> &dst,
                     const std::unordered_map<uint32_t, uint64_t> &src) {
    for (const auto &kv : src) {
        dst[kv.first] += kv.second;
    }
}

void mergeCounterSnapshot(CounterSnapshot &dst, const CounterSnapshot &src) {
    dst.llc_hits += src.llc_hits;
    dst.llc_misses += src.llc_misses;
    dst.cha_remote_clean += src.cha_remote_clean;
    dst.cha_remote_dirty += src.cha_remote_dirty;
    dst.wb_required += src.wb_required;
    dst.inval_fanout_sum += src.inval_fanout_sum;

    mergeCounterMap(dst.l1d_hits, src.l1d_hits);
    mergeCounterMap(dst.l1d_misses, src.l1d_misses);
    mergeCounterMap(dst.l2_hits, src.l2_hits);
    mergeCounterMap(dst.l2_misses, src.l2_misses);
    mergeCounterMap(dst.l1i_hits, src.l1i_hits);
    mergeCounterMap(dst.l1i_misses, src.l1i_misses);
    mergeCounterMap(dst.dtlb_misses, src.dtlb_misses);
    mergeCounterMap(dst.itlb_misses, src.itlb_misses);
    mergeCounterMap(dst.walker_dram_misses, src.walker_dram_misses);

    dst.pmu_l1d_loads += src.pmu_l1d_loads;
    dst.pmu_l1d_stores += src.pmu_l1d_stores;
    dst.pmu_l1d_load_misses += src.pmu_l1d_load_misses;
    dst.pmu_l1d_store_misses += src.pmu_l1d_store_misses;
    dst.pmu_l2_misses += src.pmu_l2_misses;
    dst.pmu_llc_load_misses += src.pmu_llc_load_misses;
    dst.pmu_llc_store_misses += src.pmu_llc_store_misses;
    dst.pmu_cha_requests_reads += src.pmu_cha_requests_reads;
    dst.pmu_cha_requests_writes += src.pmu_cha_requests_writes;
    dst.pmu_cha_tor_inserts_ia_miss_drd += src.pmu_cha_tor_inserts_ia_miss_drd;
    dst.pmu_cha_dir_lookup_snp += src.pmu_cha_dir_lookup_snp;
    dst.pmu_cha_core_snp_any_one += src.pmu_cha_core_snp_any_one;
}

}  // namespace

Coordinator::Coordinator(const tao_uarch::UarchProfile &cfg)
    : shared_(cfg),
      pending_by_core_(cfg.num_cores > 0 ? cfg.num_cores : 1) {}

CounterSnapshot &Coordinator::counterSlotForCore(uint32_t core_id) {
    if (core_id < pending_by_core_.size()) {
        return pending_by_core_[core_id];
    }
    // 非配置内 core_id 只应出现在兼容旧入口或异常输入。慢路径加锁扩容，
    // 避免给正常 16c 热路径引入每 op mutex。
    std::lock_guard<std::mutex> lk(pending_mu_);
    if (core_id >= pending_by_core_.size()) {
        pending_by_core_.resize(std::size_t(core_id) + 1);
    }
    return pending_by_core_[core_id];
}

OracleResult Coordinator::stepForCore(uint32_t core_id, uint64_t paddr,
                                      bool is_store, uint16_t size,
                                      uint64_t seq, uint32_t thread_id) {
    // 兼容旧 Python 旧路径（PyCoordinator 直调），D.0 内部委托给 LocalRefSim
    // 等价；但旧路径不持 LocalRefSim 时退回手工 stepImpl。这里保留是为了
    // PyLocalRefSim 之外有人直接 stepForCore 也能跑（比如 reconcile dual-path）。
    // 注意：此分支为旧 PyRefSim API 提供的回退；PyLocalRefSim 走 probe()。
    static thread_local std::unordered_map<uint32_t, CoreLocal> fallback_locals;
    auto it = fallback_locals.find(core_id);
    if (it == fallback_locals.end()) {
        it = fallback_locals.emplace(std::piecewise_construct,
                                     std::forward_as_tuple(core_id),
                                     std::forward_as_tuple(core_id, shared_.cfg)).first;
    }

    Event ev;
    ev.seq = seq;
    ev.core_id = core_id;
    ev.thread_id = thread_id;
    ev.cacheline_addr = paddr & ~uint64_t(63);
    ev.is_store = is_store;
    ev.size = size;

    OracleResult r;
    r.d = stepImpl(it->second, shared_, ev);
    r.is_ifetch = false;
    r.cl = ev.cacheline_addr;
    r.core_id = core_id;
    r.seq = seq;
    r.is_store = is_store;
    r.shared_authoritative = true;
    accumulateD(core_id, r.d, is_store);
    return r;
}

OracleResult Coordinator::stepIFetchForCore(uint32_t core_id,
                                            uint64_t vaddr_cl) {
    static thread_local std::unordered_map<uint32_t, CoreLocal> fallback_locals;
    auto it = fallback_locals.find(core_id);
    if (it == fallback_locals.end()) {
        it = fallback_locals.emplace(std::piecewise_construct,
                                     std::forward_as_tuple(core_id),
                                     std::forward_as_tuple(core_id, shared_.cfg)).first;
    }
    OracleResult r;
    r.i = stepIFetchImpl(it->second, shared_, core_id, vaddr_cl);
    r.is_ifetch = true;
    r.cl = vaddr_cl & ~uint64_t(63);
    r.core_id = core_id;
    r.seq = 0;
    r.is_store = false;
    r.shared_authoritative = true;
    accumulateI(core_id, r.i);
    return r;
}

void Coordinator::accumulateD(uint32_t core_id, const DSideOracle &d, bool is_store) {
    CounterSnapshot &pending = counterSlotForCore(core_id);
    switch (CohAction(d.coh_oracle)) {
    case CohAction::REMOTE_HIT_CLEAN: ++pending.cha_remote_clean; break;
    case CohAction::REMOTE_HIT_DIRTY: ++pending.cha_remote_dirty; break;
    case CohAction::WB_REQUIRED:      ++pending.wb_required; break;
    case CohAction::LLC_HIT:          ++pending.llc_hits; break;
    case CohAction::DRAM:             ++pending.llc_misses; break;
    default: break;
    }
    pending.inval_fanout_sum += d.inval_fanout;
    switch (d.path_class) {
    case 0: pending.l1d_hits[core_id]++; break;
    case 1: pending.l1d_misses[core_id]++; pending.l2_hits[core_id]++; break;
    case 2: pending.l1d_misses[core_id]++; pending.l2_misses[core_id]++; break;
    case 3: pending.l1d_misses[core_id]++; pending.l2_misses[core_id]++; break;
    case 4: pending.l1d_misses[core_id]++; pending.l2_misses[core_id]++; break;
    default: break;
    }
    if (!d.dtlb_hit) pending.dtlb_misses[core_id]++;
    pending.walker_dram_misses[core_id] += d.d_walker_dram_misses;

    if (is_store) ++pending.pmu_l1d_stores;
    else          ++pending.pmu_l1d_loads;

    const CohAction coh = CohAction(d.coh_oracle);
    if (coh != CohAction::L1_HIT) {
        if (is_store) ++pending.pmu_l1d_store_misses;
        else          ++pending.pmu_l1d_load_misses;
    }
    if (coh != CohAction::L1_HIT && coh != CohAction::L2_HIT) {
        ++pending.pmu_l2_misses;
    }
    if (coh == CohAction::DRAM) {
        if (is_store) ++pending.pmu_llc_store_misses;
        else          ++pending.pmu_llc_load_misses;
    }
    if (is_store) ++pending.pmu_cha_requests_writes;
    else          ++pending.pmu_cha_requests_reads;
    if (!is_store && coh == CohAction::DRAM) {
        ++pending.pmu_cha_tor_inserts_ia_miss_drd;
    }
    if (coh == CohAction::REMOTE_HIT_CLEAN ||
        coh == CohAction::REMOTE_HIT_DIRTY ||
        coh == CohAction::WB_REQUIRED) {
        ++pending.pmu_cha_dir_lookup_snp;
        ++pending.pmu_cha_core_snp_any_one;
    }
}

void Coordinator::accumulateI(uint32_t core_id, const IFetchResult &r) {
    CounterSnapshot &pending = counterSlotForCore(core_id);
    // i-side counters (l1i_hits/misses, itlb_misses, walker) are kept for
    // legacy reporting of driver-internal i-cache state. They are NOT
    // semantically aligned with gem5 oracle ifetch events — the driver only
    // sees retire-stream macro_pc cacheline edges, not real IFU fetch ticks.
    // For the same reason, PMU accumulation (l1d_load_misses / l2_misses /
    // llc_load_misses / cha_tor_inserts_ia_miss_drd / cha_requests_reads) is
    // intentionally excluded here: PMU bit-exact alignment is restricted to
    // d-side only when input is functional_parquet.
    switch (r.i_path_class) {
    case 0: pending.l1i_hits[core_id]++; break;
    case 1: pending.l1i_misses[core_id]++; break;
    case 2: pending.l1i_misses[core_id]++; break;
    case 4: pending.l1i_misses[core_id]++; break;
    default: break;
    }
    if (!r.itlb_hit) pending.itlb_misses[core_id]++;
    pending.walker_dram_misses[core_id] += r.i_walker_dram_misses;
}

void Coordinator::reconcile(const std::vector<LineDelta> & /*deltas*/,
                            std::vector<OracleResult> & /*results*/) {
    // D.5a: probe mutates the atomic/bank-locked shared structures directly.
    // Keep the barrier as a stable API hook for the driver.
}

void Coordinator::drainCounters(CounterSnapshot &dst) {
    std::lock_guard<std::mutex> lk(pending_mu_);
    dst = CounterSnapshot{};
    for (const CounterSnapshot &slot : pending_by_core_) {
        mergeCounterSnapshot(dst, slot);
    }
    for (CounterSnapshot &slot : pending_by_core_) {
        slot = CounterSnapshot{};
    }
}

}  // namespace quantum
}  // namespace mesi_ref
