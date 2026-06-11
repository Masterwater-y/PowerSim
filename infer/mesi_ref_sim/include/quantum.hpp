// quantum.hpp —— Quantum-based parallel coherence 拆分
//
// 对应方案文档 tao_cpu_sim/docs/04-quantum-parallel-coherence.md：
//   §9.1   ref_sim 拆分为 LocalRefSim（per-core 私有侧）+ Coordinator（全局共享侧）
//   §7A.5  LocalRefSim::probe / commit + Coordinator::reconcile / snapshot 接口
//   §9.1.1 Snapshot 增量协议（LineDelta / LlcSetDelta / version）
//
// B.1 阶段目标：声明骨架 + Phase 1（probe）/ Phase 2（reconcile）数据结构，
//   保留旧 simulator.hpp::Simulator（Python A 阶段 PyRefSim 仍沿用），新增
//   PyLocalRefSim/PyCoordinator 暴露面在 python_module.cc 完成。
//
// 设计要点：
// 1. 私有侧字段 = 只依赖本核 LRU/MSHR/TLB/Walker 的字段（path_class 中的
//    L1/L2 命中、d_mshr_depth、dtlb_hit、d_walker_levels、d_walker_dram_misses、
//    d_bank_id、d_llc_set_lru_pos 中本核可见部分）。
// 2. 共享侧字段 = mesi_before / sharer_bucket / owner_dist / dirty_owner /
//    inval_fanout / same_line_recent / d_llc_set_residency（LLC 跨核共享）/
//    d_llc_set_lru_pos（LLC，跨核 LRU）/ path_class 中的 LLC_HIT / DRAM /
//    REMOTE_HIT_* / WB_REQUIRED 段。
// 3. probe() 返回 OracleResult 中：私有字段直接填本核计算结果（commit 前已正确）；
//    共享字段填 LocalRefSim 在 Phase 1 时持有的 *snapshot 视图* 估算值，并在
//    PendingProbe::oracle_ref 上挂引用，由 Coordinator 在 Phase 2 严格全序回放时
//    覆盖（双轨制：Phase 1 估算 + Phase 2 权威）。
// 4. commit() 真改本地 LRU/MSHR/TLB/Walker；Coordinator::reconcile 真改 D-MESI
//    dir / L3 / L3-i / i_lines_ / recent_line_count_。
//
#pragma once

#include <cstdint>
#include <memory>
#include <mutex>
#include <unordered_map>
#include <unordered_set>
#include <vector>

#include "lru_banked.hh"
#include "simulator.hpp"   // 复用 LineMesi / DSideOracle / IFetchResult / CohAction
#include "uarch_profile.hh"

namespace mesi_ref {
namespace quantum {

// ====================== 共享接口结构 ======================

// 与 §9.1.1 一致：core 在 commit 时对单个 line 的提议（is_store / sharers /
// state 转移由 Coordinator 决定，core 只报 raw event）。
struct LineDelta {
    uint64_t cl;
    uint32_t core_id;
    uint64_t seq;
    bool     is_store;
    bool     is_ifetch;   // i-side 走 i_lines_ + l3_i_，d-side 走 lines_ + l3_
};

// LLC set 维度的提议：哪个 line 在 commit 时需要 touch（更新 LRU）。
// 与 LineDelta 等价语义，但单独成结构以便 Coordinator 优化批量 LRU touch。
struct LlcSetDelta {
    uint64_t cl;
    bool     is_ifetch;
};

// 私有 + 共享侧 oracle 的合一容器（Phase 1 由 LocalRefSim 填私有侧，
// Phase 2 由 Coordinator 回填共享侧）。共享字段名按 docs/04 §7A.5。
struct OracleResult {
    // ---- D 侧 ----
    DSideOracle d;
    // ---- I 侧（仅 ifetch 走，d-side 默认 0）----
    Simulator::IFetchResult i;
    // ---- 元数据 ----
    bool     is_ifetch     = false;
    uint64_t cl            = 0;
    uint32_t core_id       = 0;
    uint64_t seq           = 0;
    bool     is_store      = false;
    // 双轨标记：Phase 1 私有侧 OK；Phase 2 由 Coordinator 改写 shared 字段后置 true
    bool     shared_authoritative = false;
};

// 全局 + per-core 计数器（CounterSnapshot）。每 quantum 边界由 Coordinator
// 聚合后写 report.json：l1d_miss / l2_miss / llc_miss / cha_remote / inval_fanout / ...
struct CounterSnapshot {
    // 全局
    uint64_t llc_hits         = 0;
    uint64_t llc_misses       = 0;
    uint64_t cha_remote_clean = 0;  // REMOTE_HIT_CLEAN 计数（CHA 性质）
    uint64_t cha_remote_dirty = 0;
    uint64_t wb_required      = 0;
    uint64_t inval_fanout_sum = 0;  // 累计 store 引发的失效目标数
    // per-core（key=core_id）
    std::unordered_map<uint32_t, uint64_t> l1d_hits;
    std::unordered_map<uint32_t, uint64_t> l1d_misses;
    std::unordered_map<uint32_t, uint64_t> l2_hits;
    std::unordered_map<uint32_t, uint64_t> l2_misses;
    std::unordered_map<uint32_t, uint64_t> l1i_hits;
    std::unordered_map<uint32_t, uint64_t> l1i_misses;
    std::unordered_map<uint32_t, uint64_t> dtlb_misses;
    std::unordered_map<uint32_t, uint64_t> itlb_misses;
    std::unordered_map<uint32_t, uint64_t> walker_dram_misses;
    // PMU 语义计数（对齐 scripts/pmu_report.py）
    uint64_t pmu_l1d_loads                = 0;
    uint64_t pmu_l1d_stores               = 0;
    uint64_t pmu_l1d_load_misses          = 0;
    uint64_t pmu_l1d_store_misses         = 0;
    uint64_t pmu_l2_misses                = 0;
    uint64_t pmu_llc_load_misses          = 0;
    uint64_t pmu_llc_store_misses         = 0;
    uint64_t pmu_cha_requests_reads       = 0;
    uint64_t pmu_cha_requests_writes      = 0;
    uint64_t pmu_cha_tor_inserts_ia_miss_drd = 0;
    uint64_t pmu_cha_dir_lookup_snp       = 0;
    uint64_t pmu_cha_core_snp_any_one     = 0;
};

// 前置声明
class Coordinator;

// ====================== LocalRefSim ======================
//
// 单核私有 façade。D.0 后真私有：直接持 CoreLocal（L1d/L1i/L2/L2_i/dtlb/itlb/
// MSHR），probe()/commit() 调 stepImpl(local_, coord->shared(), ev)，
// 不再走 Coordinator::sim_ 中转。共享侧字段仍由 stepImpl 落 SharedState
// （即 D.0 还没分 probe 只读 / commit 才写两轨；那是 D.1 工作）。
//
class LocalRefSim {
public:
    LocalRefSim(Coordinator *coord, uint32_t core_id);

    // 返回包含私有 + 共享字段的 OracleResult（D.0 阶段共享字段就是
    // stepImpl 算出来的权威值，shared_authoritative=true）。
    OracleResult probe(uint64_t paddr, bool is_store, uint16_t size,
                       uint64_t seq, uint32_t thread_id);
    OracleResult probeIFetch(uint64_t vaddr_cl);

    // Commit is retained as a stable Python/API hook. D.5a mutates shared state
    // directly in probe via atomic/bank-locked structures, so commit only
    // returns metadata for compatibility.
    LineDelta commit(uint64_t paddr, bool is_store, uint16_t size,
                     uint64_t seq, uint32_t thread_id);
    LineDelta commitIFetch(uint64_t vaddr_cl);

    uint32_t coreId() const { return core_id_; }
    CoreLocal &local() { return local_; }

private:
    Coordinator *coord_;
    uint32_t core_id_;
    CoreLocal local_;   // D.0：每核私有 L1d/L1i/L2/L2_i/dtlb/itlb/MSHR
};

// ====================== Coordinator ======================
//
// D.0 后：内部持一份 SharedState（cfg/lines/i_lines/l3/l3_i/walker/
// recent_line_count），不再持中心化 Simulator。LocalRefSim 直接通过
// shared() 引用访问共享字段。
//
class Coordinator {
public:
    explicit Coordinator(const tao_uarch::UarchProfile &cfg);

    // 由 LocalRefSim 转发的核心入口。返回完整 OracleResult。
    OracleResult stepForCore(uint32_t core_id, uint64_t paddr, bool is_store,
                             uint16_t size, uint64_t seq, uint32_t thread_id);
    OracleResult stepIFetchForCore(uint32_t core_id, uint64_t vaddr_cl);

    // 全序 reconcile：D.0 stub —— deltas 已被 stepImpl 时实时执行，
    // 这里仅做一致性断言（debug 模式）。D.2 替换为真排序回放。
    void reconcile(const std::vector<LineDelta> &deltas,
                   std::vector<OracleResult> &results);

    // 拉取并清空所有 counters（global + per-core）。
    void drainCounters(CounterSnapshot &dst);

    // D.0：counters 累加供 LocalRefSim 直接调用（避免再走 stepForCore 转发）。
    void accumulateD(uint32_t core_id, const DSideOracle &d, bool is_store);
    void accumulateI(uint32_t core_id, const IFetchResult &r);

    SharedState &shared() { return shared_; }
    const tao_uarch::UarchProfile &cfg() const { return shared_.cfg; }

private:
    CounterSnapshot &counterSlotForCore(uint32_t core_id);

    SharedState shared_;
    std::vector<CounterSnapshot> pending_by_core_;
    std::mutex pending_mu_;
};

}  // namespace quantum
}  // namespace mesi_ref
