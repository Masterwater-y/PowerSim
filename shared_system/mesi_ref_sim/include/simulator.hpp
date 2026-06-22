// MESI_Three_Level reference simulator —— V9.5 状态机版本
//
// 设计要点（D.0 后）:
//   1. 每核私有状态收敛到 CoreLocal（L1d/L1i/L2/L2_i/dtlb/itlb/MSHR）。
//   2. 跨核共享状态收敛到 SharedState（cfg/lines/i_lines/l3/l3_i/walker/
//      recent_line_count）。
//   3. step()/stepIFetch() 作为纯函数 stepImpl/stepIFetchImpl，显式接受
//      CoreLocal&+SharedState&。Simulator 类保留为 main.cc 的 façade，
//      内部持 SharedState + map<cid, CoreLocal>，行为与重构前 bit-exact。
//   4. LocalRefSim/Coordinator 直接持 CoreLocal/SharedState，绕过 façade，
//      为后续 D.1（dirty 集合）/D.4（GIL release + ThreadPool）打基础。
//
#pragma once

#include <array>
#include <atomic>
#include <cstdint>
#include <cstdio>
#include <fstream>
#include <list>
#include <memory>
#include <mutex>
#include <shared_mutex>
#include <sstream>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <vector>

#include "lru_banked.hh"
#include "uarch_profile.hh"

namespace mesi_ref {

enum class CohAction : uint8_t {
    UNKNOWN = 0, L1_HIT = 1, REMOTE_HIT_CLEAN = 2, REMOTE_HIT_DIRTY = 3,
    LLC_HIT = 4, DRAM = 5, WB_REQUIRED = 6, L2_HIT = 7
};

// MESI 简化状态机: I=0, S=1, E=2, M=3.
// D.5a: line directory is stored as one atomic word:
// [0:1]=state [2:9]=owner+1 (0 means no owner) [10:13]=4-core sharer bitmap.
struct AtomicLine {
    std::atomic<uint64_t> raw{0};
};

struct DecodedLine {
    uint8_t state = 0;
    int32_t owner_core = -1;
    uint8_t sharer_bits = 0;
};

inline uint64_t packLine(uint8_t state, int32_t owner_core, uint8_t sharer_bits)
{
    const uint64_t owner_enc = (owner_core < 0) ? 0 : uint64_t(owner_core + 1);
    return (uint64_t(state) & 0x3ull) |
           ((owner_enc & 0xffull) << 2) |
           ((uint64_t(sharer_bits) & 0xfull) << 10);
}

inline DecodedLine decodeLine(uint64_t raw)
{
    DecodedLine d;
    d.state = uint8_t(raw & 0x3ull);
    const uint64_t owner_enc = (raw >> 2) & 0xffull;
    d.owner_core = (owner_enc == 0) ? -1 : int32_t(owner_enc - 1);
    d.sharer_bits = uint8_t((raw >> 10) & 0xfull);
    return d;
}

inline uint8_t coreBit(uint32_t cid)
{
    return (cid < 4) ? uint8_t(1u << cid) : uint8_t(0);
}

inline bool hasSharer(const DecodedLine &line, uint32_t cid)
{
    return (line.sharer_bits & coreBit(cid)) != 0;
}

inline size_t sharerCount(uint8_t bits)
{
    size_t n = 0;
    for (uint8_t v = bits & 0x0f; v != 0; v >>= 1) n += (v & 1u);
    return n;
}

constexpr size_t SHARD_N = 64;

struct LinesShard {
    mutable std::shared_mutex mu;
    std::unordered_map<uint64_t, std::unique_ptr<AtomicLine>> map;
};

struct RlcShard {
    mutable std::shared_mutex mu;
    std::unordered_map<uint64_t, std::unique_ptr<std::atomic<uint32_t>>> map;
};

class BankLockedLRU {
public:
    void configure(const tao_uarch::CacheCfg &cfg)
    {
        lru_.configure(cfg);
        bank_mu_ = std::vector<std::mutex>(lru_.numBanks());
    }

    bool touch(uint64_t cl, int64_t *evicted_byte_addr = nullptr)
    {
        std::lock_guard<std::mutex> lk(mutexFor(cl));
        return lru_.touch(cl, evicted_byte_addr);
    }

    bool contains(uint64_t cl) const
    {
        std::lock_guard<std::mutex> lk(mutexFor(cl));
        return lru_.contains(cl);
    }

    void peekSetState(uint64_t cl, uint32_t *res, uint32_t *pos) const
    {
        std::lock_guard<std::mutex> lk(mutexFor(cl));
        lru_.peekSetState(cl, res, pos);
    }

    void invalidate(uint64_t cl)
    {
        std::lock_guard<std::mutex> lk(mutexFor(cl));
        lru_.invalidate(cl);
    }

    uint32_t bankIdOf(uint64_t cl) const { return lru_.bankIdOf(cl); }

private:
    tao_uarch::BankedSetAssocLRU lru_;
    mutable std::vector<std::mutex> bank_mu_;

    std::mutex &mutexFor(uint64_t cl) const
    {
        const uint32_t bank = lru_.bankIdOf(cl);
        return bank_mu_[bank % bank_mu_.size()];
    }
};

struct DSideOracle {
    uint8_t  mesi_before        = 0;
    uint8_t  coh_oracle         = 0;
    uint8_t  sharer_bucket      = 0;
    uint8_t  owner_dist         = 0;
    uint8_t  dirty_owner        = 0;
    uint8_t  path_class         = 0;
    uint8_t  inval_fanout       = 0;
    uint8_t  same_line_recent   = 0;
    uint8_t  oracle_source      = 1;
    uint8_t  d_mshr_depth        = 0;
    uint8_t  dtlb_hit            = 0;
    uint8_t  d_walker_levels     = 0;
    uint8_t  d_walker_dram_misses = 0;
    uint8_t  d_bank_id           = 0;
    uint8_t  d_llc_set_residency = 0;
    uint8_t  d_llc_set_lru_pos   = 0;
};

struct IFetchResult {
    uint8_t i_path_class = 0;
    uint8_t i_coh_oracle = 0;
    uint8_t i_mesi_before = 0;
    uint8_t i_mshr_depth        = 0;
    uint8_t itlb_hit            = 0;
    uint8_t i_walker_levels     = 0;
    uint8_t i_walker_dram_misses = 0;
    uint8_t i_bank_id           = 0;
    uint8_t i_llc_set_residency = 0;
    uint8_t i_llc_set_lru_pos   = 0;
};

struct Event {
    uint64_t seq;
    uint32_t core_id;
    uint32_t thread_id;
    uint64_t cacheline_addr;
    bool     is_store;
    uint16_t size;
};

// ---- D.0：每核私有状态（L1d/L1i/L2/L2_i/dtlb/itlb/MSHR） ----
//   每个 LocalRefSim 持有一份独立 CoreLocal；step()/stepIFetch() 只读
//   /写本核的 CoreLocal 与共享 SharedState，不再跨核访问。
struct CoreLocal {
    uint32_t core_id = 0;
    tao_uarch::BankedSetAssocLRU l1d;
    tao_uarch::BankedSetAssocLRU l1i;
    tao_uarch::BankedSetAssocLRU l2;
    tao_uarch::BankedSetAssocLRU l2_i;
    tao_uarch::TlbSim dtlb;
    tao_uarch::TlbSim itlb;
    tao_uarch::MshrTracker l1d_mshr;
    tao_uarch::MshrTracker l1i_mshr;

    CoreLocal() = default;
    CoreLocal(uint32_t cid, const tao_uarch::UarchProfile &cfg)
        : core_id(cid)
    {
        l1d.configure(cfg.l1d);
        l1i.configure(cfg.l1i);
        l2.configure(cfg.l2);
        l2_i.configure(cfg.l2);
        dtlb.configure(cfg.dtlb, cfg.walker.page_size_bits);
        itlb.configure(cfg.itlb, cfg.walker.page_size_bits);
        l1d_mshr.configure(cfg.mshr.l1d);
        l1i_mshr.configure(cfg.mshr.l1d);
    }
};

// ---- D.5a：跨核共享状态（atomic lines + sharded maps + bank locks） ----
//   Coordinator 持有唯一一份 SharedState；LocalRefSim 通过引用访问。
//   lines/i_lines/rlc 均按 cacheline 分片，map 只在插入时加 shard 独占锁；
//   已存在 entry 的状态更新走 atomic CAS。LLC LRU 先按 bank 加锁。
struct SharedState {
    tao_uarch::UarchProfile cfg;
    std::array<LinesShard, SHARD_N> lines;
    std::array<LinesShard, SHARD_N> i_lines;
    std::array<RlcShard, SHARD_N> recent_line_count;
    BankLockedLRU l3;
    BankLockedLRU l3_i;
    tao_uarch::PageWalkSim walker;
    tao_uarch::PageWalkSim i_walker;

    explicit SharedState(const tao_uarch::UarchProfile &c) : cfg(c)
    {
        l3.configure(cfg.l3);
        l3_i.configure(cfg.l3);
        walker.configure(cfg.walker);
        i_walker.configure(cfg.walker);
    }
};

inline size_t shardIndex(uint64_t cl)
{
    return (cl >> 6) & (SHARD_N - 1);
}

inline AtomicLine *ensureLine(std::array<LinesShard, SHARD_N> &shards,
                              uint64_t cl)
{
    auto &shard = shards[shardIndex(cl)];
    {
        std::shared_lock<std::shared_mutex> lk(shard.mu);
        auto it = shard.map.find(cl);
        if (it != shard.map.end()) return it->second.get();
    }
    std::unique_lock<std::shared_mutex> lk(shard.mu);
    auto &slot = shard.map[cl];
    if (!slot) slot = std::make_unique<AtomicLine>();
    return slot.get();
}

inline const AtomicLine *findLine(const std::array<LinesShard, SHARD_N> &shards,
                                  uint64_t cl)
{
    auto &shard = shards[shardIndex(cl)];
    std::shared_lock<std::shared_mutex> lk(shard.mu);
    auto it = shard.map.find(cl);
    return (it == shard.map.end()) ? nullptr : it->second.get();
}

inline uint32_t fetchAndIncrementRlc(std::array<RlcShard, SHARD_N> &shards,
                                     uint64_t cl)
{
    auto &shard = shards[shardIndex(cl)];
    std::atomic<uint32_t> *counter = nullptr;
    {
        std::shared_lock<std::shared_mutex> lk(shard.mu);
        auto it = shard.map.find(cl);
        if (it != shard.map.end()) counter = it->second.get();
    }
    if (!counter) {
        std::unique_lock<std::shared_mutex> lk(shard.mu);
        auto &slot = shard.map[cl];
        if (!slot) slot = std::make_unique<std::atomic<uint32_t>>(0);
        counter = slot.get();
    }
    return counter->fetch_add(1, std::memory_order_acq_rel);
}

// 与 gem5 tao_trace.cc:bucketCount 完全同公式：0/1/2/3-7/8+
inline uint8_t bucketCount(size_t n)
{
    if (n == 0) return 0;
    if (n == 1) return 1;
    if (n == 2) return 2;
    if (n <= 7) return 3;
    return 4;
}

// ---- D.0：纯函数 step()，显式接受 CoreLocal&+SharedState& ----
//   行为与原 Simulator::step 完全一致；唯一区别是数据来源从成员
//   变量切换为参数。bit-exact 由 5K Δt=1 smoke 守门。
inline DSideOracle stepImpl(CoreLocal &local, SharedState &shared, const Event &ev)
{
    const uint64_t cl = ev.cacheline_addr & ~uint64_t(63);
    const uint32_t cid = ev.core_id;
    AtomicLine *atomic_line = ensureLine(shared.lines, cl);
    uint64_t line_raw = atomic_line->raw.load(std::memory_order_acquire);
    DecodedLine line = decodeLine(line_raw);

    DSideOracle out;

    if (line.state == 0) {
        out.mesi_before = 0;
    } else if (line.owner_core == int32_t(cid)) {
        out.mesi_before = line.state;
    } else if (hasSharer(line, cid)) {
        out.mesi_before = 1;
    } else {
        out.mesi_before = 0;
    }

    size_t sc = sharerCount(line.sharer_bits);
    if (hasSharer(line, cid)) sc = (sc > 0) ? sc - 1 : 0;
    out.sharer_bucket = bucketCount(sc);

    bool other_owns = (line.owner_core >= 0 &&
                       line.owner_core != int32_t(cid));
    out.dirty_owner = (line.state == 3 && other_owns) ? 1 : 0;
    out.owner_dist  = (line.owner_core < 0) ? 0 : (other_owns ? 2 : 0);

    bool l1_hit = local.l1d.contains(cl);
    bool l2_hit = local.l2.contains(cl);
    bool l3_hit = shared.l3.contains(cl);

    CohAction coh = CohAction::UNKNOWN;
    uint8_t   pc  = 0;
    bool resolved = false;
    if (ev.is_store && (other_owns || sc > 0)) {
        coh = CohAction::WB_REQUIRED;
        pc  = 3;
        resolved = true;
    } else if (!ev.is_store && other_owns) {
        coh = (line.state == 3) ? CohAction::REMOTE_HIT_DIRTY
                                : CohAction::REMOTE_HIT_CLEAN;
        pc  = 3;
        resolved = true;
    }
    if (!resolved) {
        if (l1_hit) {
            coh = CohAction::L1_HIT;     pc = 0;
        } else if (l2_hit) {
            coh = CohAction::L2_HIT;     pc = 1;
        } else if (l3_hit) {
            coh = CohAction::LLC_HIT;    pc = 2;
        } else {
            coh = CohAction::DRAM;       pc = 4;
        }
    }
    out.coh_oracle = uint8_t(coh);
    out.path_class = pc;

    out.inval_fanout = ev.is_store ? bucketCount(sc) : 0;

    const uint32_t rc_before = fetchAndIncrementRlc(shared.recent_line_count, cl);
    out.same_line_recent = (rc_before >= 3) ? 3 : uint8_t(rc_before);

    out.oracle_source = 0;

    {
        uint32_t res = 0, pos = 0;
        shared.l3.peekSetState(cl, &res, &pos);
        out.d_llc_set_residency = (res > 31) ? 31 : uint8_t(res);
        out.d_llc_set_lru_pos   = (pos > 31) ? 31 : uint8_t(pos);
    }

    local.l1d.touch(cl);
    local.l2.touch(cl);
    shared.l3.touch(cl);

    // P0-A 对账修复（gem5 d-side：取 insert 之前 size，retire 在 commit 阶段）：
    // 此处仅 insert（不立刻 retire），size 在 insert 之前读取，与 gem5 端
    // tao_trace.cc 同语义；MshrTracker capacity（cfg.mshr.l1d=16）做上限自然 evict。
    size_t md = local.l1d_mshr.size();
    local.l1d_mshr.insert(cl, ev.seq);

    bool dtlb_hit = local.dtlb.translate(ev.cacheline_addr);
    tao_uarch::PageWalkSim::WalkResult wr;
    if (!dtlb_hit) {
        wr = shared.walker.walkWithL3(ev.cacheline_addr,
                                      local.l1d, local.l2, shared.l3);
    }

    out.d_mshr_depth = (md > 15) ? 15 : uint8_t(md);
    out.dtlb_hit     = dtlb_hit ? 1 : 0;
    uint32_t wl = wr.levels;
    out.d_walker_levels = (wl > 7) ? 7 : uint8_t(wl);
    uint32_t wd = wr.miss_dram;
    out.d_walker_dram_misses = (wd > 7) ? 7 : uint8_t(wd);
    uint32_t bid = local.l1d.bankIdOf(cl);
    out.d_bank_id = (bid > 15) ? 15 : uint8_t(bid);

    uint64_t expected = line_raw;
    for (;;) {
        DecodedLine cur = decodeLine(expected);
        DecodedLine next = cur;
        if (ev.is_store) {
            next.sharer_bits = 0;
            next.owner_core = int32_t(cid);
            next.state = 3;
        } else {
            if (cur.state == 0) {
                next.owner_core = int32_t(cid);
                next.state = 2;
                next.sharer_bits = coreBit(cid);
            } else if (cur.state == 3) {
                next.sharer_bits = cur.sharer_bits | coreBit(uint32_t(cur.owner_core)) | coreBit(cid);
                next.owner_core = -1;
                next.state = 1;
            } else if (cur.state == 2) {
                if (cur.owner_core != int32_t(cid)) {
                    next.sharer_bits = cur.sharer_bits | coreBit(uint32_t(cur.owner_core)) | coreBit(cid);
                    next.owner_core = -1;
                    next.state = 1;
                } else {
                    next.sharer_bits = cur.sharer_bits | coreBit(cid);
                }
            } else {
                next.sharer_bits = cur.sharer_bits | coreBit(cid);
            }
        }
        const uint64_t desired = packLine(next.state, next.owner_core, next.sharer_bits);
        if (atomic_line->raw.compare_exchange_weak(
                expected, desired, std::memory_order_release,
                std::memory_order_acquire)) {
            break;
        }
    }
    return out;
}

inline IFetchResult stepIFetchImpl(CoreLocal &local, SharedState &shared,
                                   uint32_t core_id, uint64_t cl_byte_addr)
{
    const uint64_t cl = cl_byte_addr & ~uint64_t(63);
    IFetchResult r;
    bool l1i_hit = local.l1i.contains(cl);
    bool l2_hit  = local.l2_i.contains(cl);
    bool l3_hit  = shared.l3_i.contains(cl);
    if (l1i_hit) {
        r.i_coh_oracle = uint8_t(CohAction::L1_HIT);
        r.i_path_class = 0;
    } else if (l2_hit) {
        r.i_coh_oracle = uint8_t(CohAction::L2_HIT);
        r.i_path_class = 1;
    } else if (l3_hit) {
        r.i_coh_oracle = uint8_t(CohAction::LLC_HIT);
        r.i_path_class = 2;
    } else {
        r.i_coh_oracle = uint8_t(CohAction::DRAM);
        r.i_path_class = 4;
    }
    auto *ip = findLine(shared.i_lines, cl);
    if (ip != nullptr) {
        const DecodedLine ls = decodeLine(ip->raw.load(std::memory_order_acquire));
        if (ls.owner_core == int32_t(core_id))
            r.i_mesi_before = ls.state;
        else if (hasSharer(ls, core_id))
            r.i_mesi_before = 1;
        else
            r.i_mesi_before = 0;
    }
    {
        uint32_t res = 0, pos = 0;
        shared.l3_i.peekSetState(cl, &res, &pos);
        r.i_llc_set_residency = (res > 31) ? 31 : uint8_t(res);
        r.i_llc_set_lru_pos   = (pos > 31) ? 31 : uint8_t(pos);
    }
    local.l1i.touch(cl);
    local.l2_i.touch(cl);
    shared.l3_i.touch(cl);
    local.l1i_mshr.insert(cl, /*seq=*/0);
    local.l1i_mshr.retire(cl);
    bool itlb_hit = local.itlb.translate(cl_byte_addr);
    tao_uarch::PageWalkSim::WalkResult iwr;
    if (!itlb_hit) {
        iwr = shared.i_walker.walkWithL3(cl_byte_addr,
                                         local.l1i, local.l2_i, shared.l3_i);
    }
    size_t imd = local.l1i_mshr.size();
    r.i_mshr_depth = (imd > 15) ? 15 : uint8_t(imd);
    r.itlb_hit     = itlb_hit ? 1 : 0;
    uint32_t iwl = iwr.levels;
    r.i_walker_levels = (iwl > 7) ? 7 : uint8_t(iwl);
    uint32_t iwd = iwr.miss_dram;
    r.i_walker_dram_misses = (iwd > 7) ? 7 : uint8_t(iwd);
    uint32_t ibid = local.l1i.bankIdOf(cl);
    r.i_bank_id = (ibid > 15) ? 15 : uint8_t(ibid);
    AtomicLine *atomic_line = ensureLine(shared.i_lines, cl);
    uint64_t expected = atomic_line->raw.load(std::memory_order_acquire);
    for (;;) {
        DecodedLine cur = decodeLine(expected);
        DecodedLine next = cur;
        if (cur.state == 0) {
            next.state = 2;
            next.owner_core = int32_t(core_id);
            next.sharer_bits = coreBit(core_id);
        } else {
            next.sharer_bits = cur.sharer_bits | coreBit(core_id);
            if (sharerCount(next.sharer_bits) >= 2) {
                next.state = 1;
                next.owner_core = -1;
            }
        }
        const uint64_t desired = packLine(next.state, next.owner_core, next.sharer_bits);
        if (atomic_line->raw.compare_exchange_weak(
                expected, desired, std::memory_order_release,
                std::memory_order_acquire)) {
            break;
        }
    }
    return r;
}

inline void applyEvictImpl(CoreLocal &local, SharedState &shared,
                           uint64_t cl_byte_addr, int cache_level)
{
    const uint64_t cl = cl_byte_addr & ~uint64_t(63);
    switch (cache_level) {
    case 0: local.l1d.invalidate(cl); break;
    case 1: local.l2.invalidate(cl); break;
    case 2: shared.l3.invalidate(cl); break;
    case 4: local.l1i.invalidate(cl); break;
    default: break;
    }
}

inline void applyPrefetchImpl(CoreLocal &local, SharedState &shared,
                              uint64_t cl_byte_addr, int cache_level)
{
    const uint64_t cl = cl_byte_addr & ~uint64_t(63);
    switch (cache_level) {
    case 0: local.l1d.touch(cl); break;
    case 1: local.l2.touch(cl); break;
    case 2: shared.l3.touch(cl); break;
    case 4: local.l1i.touch(cl); break;
    default: break;
    }
}

// MESI_Three_Level 状态机仿真器 façade（保留给 main.cc / 旧 PyRefSim 用）。
//   D.0 后内部委托到 stepImpl/stepIFetchImpl，行为与重构前完全一致。
class Simulator {
public:
    using Event = mesi_ref::Event;
    using IFetchResult = mesi_ref::IFetchResult;

    explicit Simulator(const tao_uarch::UarchProfile &cfg) : shared_(cfg)
    {
        for (uint32_t c = 0; c < cfg.num_cores; ++c) {
            cores_.emplace(std::piecewise_construct,
                           std::forward_as_tuple(c),
                           std::forward_as_tuple(c, cfg));
        }
    }

    DSideOracle step(const Event &ev)
    {
        auto it = cores_.find(ev.core_id);
        if (it == cores_.end()) {
            it = cores_.emplace(std::piecewise_construct,
                                std::forward_as_tuple(ev.core_id),
                                std::forward_as_tuple(ev.core_id, shared_.cfg)).first;
        }
        return stepImpl(it->second, shared_, ev);
    }

    IFetchResult stepIFetch(uint32_t core_id, uint64_t cl_byte_addr)
    {
        auto it = cores_.find(core_id);
        if (it == cores_.end()) {
            it = cores_.emplace(std::piecewise_construct,
                                std::forward_as_tuple(core_id),
                                std::forward_as_tuple(core_id, shared_.cfg)).first;
        }
        return stepIFetchImpl(it->second, shared_, core_id, cl_byte_addr);
    }

    void applyEvict(uint32_t core_id, uint64_t cl_byte_addr, int cache_level)
    {
        auto it = cores_.find(core_id);
        if (it == cores_.end()) return;
        applyEvictImpl(it->second, shared_, cl_byte_addr, cache_level);
    }

    void applyPrefetch(uint32_t core_id, uint64_t cl_byte_addr, int cache_level)
    {
        auto it = cores_.find(core_id);
        if (it == cores_.end()) return;
        applyPrefetchImpl(it->second, shared_, cl_byte_addr, cache_level);
    }

    SharedState &shared() { return shared_; }
    CoreLocal &core(uint32_t cid)
    {
        auto it = cores_.find(cid);
        if (it == cores_.end()) {
            it = cores_.emplace(std::piecewise_construct,
                                std::forward_as_tuple(cid),
                                std::forward_as_tuple(cid, shared_.cfg)).first;
        }
        return it->second;
    }

private:
    SharedState shared_;
    std::unordered_map<uint32_t, CoreLocal> cores_;
};

} // namespace mesi_ref
