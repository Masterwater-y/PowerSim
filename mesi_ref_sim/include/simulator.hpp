// MESI_Three_Level reference simulator —— V9.5 状态机版本
//
// 设计要点:
//   1. 输入: gem5 probe 输出的 mem_events.jsonl（commit-tick 全序）
//      每条只读 (seq, core_id, thread_id, cacheline_addr, is_store, size)
//      i-side：event_type="ifetch" 行也消费，更新 l1i/l2/l3 LRU。
//   2. 配置: 从 uarch_profile.json (schema v2) 读取 L1D/L1I/L2/L3 容量 +
//      assoc + banks + TLB + walker + MSHR，与 oracle 同源。
//   3. 状态机: 复刻 SLICC MESI_Three_Level-{L0,L1,L2}.sm 的"最终态"投影，
//      不实现 transient state / network buffer / stall —— 因为输入序列已
//      隐式承载所有 timing 决策的全序结果。
//   4. 输出: coh_pred 与 probe 的 coh_oracle 逐条对比，期望 0-diff（17/17 字段）。
//
// CoherenceAction 编号必须与 gem5/src/cpu/o3/probe/tao_trace.hh 完全一致:
//   0=UNKNOWN 1=L1_HIT 2=REMOTE_HIT_CLEAN 3=REMOTE_HIT_DIRTY
//   4=LLC_HIT 5=DRAM   6=WB_REQUIRED      7=L2_HIT
//
#pragma once

#include <cstdint>
#include <cstdio>
#include <fstream>
#include <list>
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

// MESI 简化状态机: I=0, S=1, E=2, M=3
struct LineMesi {
    uint8_t state = 0;        // I
    int32_t owner_core = -1;  // 仅 E/M 时有效
    std::unordered_set<uint32_t> sharers;
};

// V9.5 数据侧 oracle 的完整字段集（与 gem5 探针 deriveSharedAttrFromLineState
// bit-exact 同口径）。step() 返回此 struct，main.cc 按 commit 行 JSON 输出。
//   - mesi_before：本核访问该 line *之前* 的本核视角 MESI
//   - coh_oracle ：CohAction（旧返回值）
//   - sharer_bucket / owner_dist / dirty_owner / path_class / inval_fanout /
//     same_line_recent / oracle_source 与 gem5 tao_trace.cc 同公式
struct DSideOracle {
    uint8_t  mesi_before        = 0; // 0=I/UNK, 1=S, 2=E, 3=M
    uint8_t  coh_oracle         = 0; // CohAction
    uint8_t  sharer_bucket      = 0; // bucketCount(sharers-self): 0/1/2/3-7/8+
    uint8_t  owner_dist         = 0; // 0=self/none, 2=other_owner（4-core 单 tile 占位）
    uint8_t  dirty_owner        = 0; // (state==M && owner!=self) ? 1 : 0
    uint8_t  path_class         = 0; // 0=L1,1=L2,2=LLC,3=NoC,4=DRAM
    uint8_t  inval_fanout       = 0; // store ? bucketCount(sharers-self) : 0
    uint8_t  same_line_recent   = 0; // clip(recent_line_count_[cl], 0..3)
    uint8_t  oracle_source      = 1; // 1=fallback (ref_sim 走 line-state 推断
                                     //   等价于 gem5 deriveSharedAttrFromLineState)
};

// MESI_Three_Level 状态机仿真器（最终态投影）
//   - LRU/MSHR/TLB/Walker 全部由 UarchProfile 配置，禁止 hardcode。
class Simulator {
public:
    explicit Simulator(const tao_uarch::UarchProfile &cfg) : cfg_(cfg)
    {
        // 配置每核 L1D / L1I / L2 + 共享 L3
        for (uint32_t c = 0; c < cfg_.num_cores; ++c) {
            l1d_[c].configure(cfg_.l1d);
            l1i_[c].configure(cfg_.l1i);
            l2_[c].configure(cfg_.l2);
            l2_i_[c].configure(cfg_.l2);
            dtlb_[c].configure(cfg_.dtlb, cfg_.walker.page_size_bits);
            itlb_[c].configure(cfg_.itlb, cfg_.walker.page_size_bits);
            l1d_mshr_[c].configure(cfg_.mshr.l1d);
            l1i_mshr_[c].configure(cfg_.mshr.l1d);
        }
        l3_.configure(cfg_.l3);
        l3_i_.configure(cfg_.l3);
        walker_.configure(cfg_.walker);
        i_walker_.configure(cfg_.walker);
    }

    struct Event {
        uint64_t seq;
        uint32_t core_id;
        uint32_t thread_id;
        uint64_t cacheline_addr;  // byte addr 对齐到 cacheline
        bool     is_store;
        uint16_t size;
    };

    // V9.5：返回完整 DSideOracle（含 8 个数据侧字段）；同时更新内部 line/LRU。
    // 与 gem5 探针 deriveSharedAttrFromLineState bit-exact 同口径。
    DSideOracle step(const Event &ev)
    {
        const uint64_t cl = ev.cacheline_addr & ~uint64_t(63);
        const uint32_t cid = ev.core_id;
        auto &line = lines_[cl];

        DSideOracle out;

        // ---- (1) mesi_before：本核访问 line *之前* 的本核视角 MESI ----
        // 与 gem5 tao_trace.cc:600-609 同公式（owner==self → ls.mesi；
        // sharers.count(self) → 1=S；其他 → 0=I/UNK）。
        if (line.state == 0) {
            out.mesi_before = 0;
        } else if (line.owner_core == int32_t(cid)) {
            out.mesi_before = line.state;
        } else if (line.sharers.count(cid)) {
            out.mesi_before = 1;
        } else {
            out.mesi_before = 0;
        }

        // ---- (2) sharer_bucket：bucketCount(sharers - self) ----
        size_t sc = line.sharers.size();
        if (line.sharers.count(cid)) sc = (sc > 0) ? sc - 1 : 0;
        out.sharer_bucket = bucketCount(sc);

        // ---- (3) dirty_owner / owner_dist ----
        bool other_owns = (line.owner_core >= 0 &&
                           line.owner_core != int32_t(cid));
        out.dirty_owner = (line.state == 3 && other_owns) ? 1 : 0;
        out.owner_dist  = (line.owner_core < 0)
                              ? 0
                              : (other_owns ? 2 : 0);

        // ---- (4) coh + path_class（先看跨核冲突，再看 LRU 命中层级） ----
        // 与 gem5 tao_trace.cc:625-659 完全同公式
        bool l1_hit = l1d_[cid].contains(cl);
        bool l2_hit = l2_[cid].contains(cl);
        bool l3_hit = l3_.contains(cl);

        CohAction coh = CohAction::UNKNOWN;
        uint8_t   pc  = 0;
        bool resolved = false;
        if (ev.is_store && (other_owns || sc > 0)) {
            coh = CohAction::WB_REQUIRED;
            pc  = 3; // NoC
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

        // ---- (5) inval_fanout：store ? bucketCount(sharers-self) : 0 ----
        out.inval_fanout = ev.is_store ? bucketCount(sc) : 0;

        // ---- (6) same_line_recent：先读再 +1，clip 0..3 ----
        // 与 gem5 探针的全局 recent_line_count_ 同口径
        uint32_t rc = recent_line_count_[cl];
        out.same_line_recent = (rc >= 3) ? 3 : uint8_t(rc);
        recent_line_count_[cl] = rc + 1;

        // ---- (7) oracle_source 固定 0=packet ----
        out.oracle_source = 0;

        // ---- LRU 更新（发生在判定之后，以保证下一 event 看到的是新 LRU）
        l1d_[cid].touch(cl);
        l2_[cid].touch(cl);
        l3_.touch(cl);

        // ---- MSHR：不影响 coh 判定，仅维护 outstanding 表
        l1d_mshr_[cid].insert(cl, ev.seq);
        l1d_mshr_[cid].retire(cl);

        // ---- dTLB miss → walker 触发 PT 访问，让 LRU 与 oracle 对齐
        if (!dtlb_[cid].translate(ev.cacheline_addr)) {
            walker_.walk(ev.cacheline_addr,
                         l1d_[cid], l2_[cid], l3_);
        }

        // ---- MESI 状态转移（最终态投影）
        if (ev.is_store) {
            line.sharers.clear();
            line.owner_core = int32_t(cid);
            line.state = 3;
        } else {
            if (line.state == 0) {
                line.owner_core = int32_t(cid);
                line.state = 2; // E
                line.sharers.clear();
                line.sharers.insert(cid);
            } else if (line.state == 3) {
                line.sharers.insert(line.owner_core);
                line.sharers.insert(cid);
                line.owner_core = -1;
                line.state = 1;
            } else if (line.state == 2) {
                if (line.owner_core != int32_t(cid)) {
                    line.sharers.insert(line.owner_core);
                    line.sharers.insert(cid);
                    line.owner_core = -1;
                    line.state = 1;
                } else {
                    line.sharers.insert(cid);
                }
            } else {
                line.sharers.insert(cid);
            }
        }
        return out;
    }

    // V9.5 i-cache 事件：来自 ifetch 行，仅更新 l1i/l2/l3 LRU + ITLB + walker。
    //   返回 i_path_class（0=L1I,1=L2,2=LLC,3=NoC,4=DRAM）。
    struct IFetchResult {
        uint8_t i_path_class = 0;
        uint8_t i_coh_oracle = 0;
        uint8_t i_mesi_before = 0;
    };
    IFetchResult stepIFetch(uint32_t core_id, uint64_t cl_byte_addr)
    {
        // V10：cl_byte_addr 来自 mem_events.ifetch.cacheline_addr_v（vaddr 域）。
        //   全部走 i-side 独立视图（l1i_/l2_i_/l3_i_/i_lines_/i_walker_/itlb_），
        //   与 d-side paddr 视图严格隔离，避免跨域污染。
        const uint64_t cl = cl_byte_addr & ~uint64_t(63);
        IFetchResult r;
        bool l1i_hit = l1i_[core_id].contains(cl);
        bool l2_hit  = l2_i_[core_id].contains(cl);
        bool l3_hit  = l3_i_.contains(cl);
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
        // mesi_before：i-side 独立 line state（vaddr 域）。
        auto it = i_lines_.find(cl);
        if (it != i_lines_.end()) {
            const LineMesi &ls = it->second;
            if (ls.owner_core == int32_t(core_id))
                r.i_mesi_before = ls.state;
            else if (ls.sharers.count(core_id))
                r.i_mesi_before = 1;
            else
                r.i_mesi_before = 0;
        }
        l1i_[core_id].touch(cl);
        l2_i_[core_id].touch(cl);
        l3_i_.touch(cl);
        l1i_mshr_[core_id].insert(cl, /*seq=*/0);
        l1i_mshr_[core_id].retire(cl);
        if (!itlb_[core_id].translate(cl_byte_addr)) {
            i_walker_.walk(cl_byte_addr,
                           l1i_[core_id], l2_i_[core_id], l3_i_);
        }
        // i-side line state 状态机（fetch=只读 → I→E）
        {
            LineMesi &ls = i_lines_[cl];
            if (ls.state == 0) {
                ls.state = 2;
                ls.owner_core = int32_t(core_id);
                ls.sharers.clear();
                ls.sharers.insert(core_id);
            } else {
                ls.sharers.insert(core_id);
                if (ls.sharers.size() >= 2) {
                    ls.state = 1;
                    ls.owner_core = -1;
                }
            }
        }
        return r;
    }

    // V4：静默 cache 替换（来自 gem5 Ruby CacheMemory::deallocate hook）
    //   cache_level 0=L1D 1=L2 2=LLC 4=L1I（V9.5 新增）
    void applyEvict(uint32_t core_id, uint64_t cl_byte_addr, int cache_level)
    {
        const uint64_t cl = cl_byte_addr & ~uint64_t(63);
        switch (cache_level) {
        case 0:
            if (auto it = l1d_.find(core_id); it != l1d_.end())
                it->second.invalidate(cl);
            break;
        case 1:
            if (auto it = l2_.find(core_id); it != l2_.end())
                it->second.invalidate(cl);
            break;
        case 2:
            l3_.invalidate(cl);
            break;
        case 4:
            if (auto it = l1i_.find(core_id); it != l1i_.end())
                it->second.invalidate(cl);
            break;
        default: break;
        }
    }

    // V4：硬件预取填回（来自 RubyPrefetcherProxy::notifyPfFill）
    void applyPrefetch(uint32_t core_id, uint64_t cl_byte_addr,
                       int cache_level)
    {
        const uint64_t cl = cl_byte_addr & ~uint64_t(63);
        switch (cache_level) {
        case 0:
            if (auto it = l1d_.find(core_id); it != l1d_.end())
                it->second.touch(cl);
            break;
        case 1:
            if (auto it = l2_.find(core_id); it != l2_.end())
                it->second.touch(cl);
            break;
        case 2:
            l3_.touch(cl);
            break;
        case 4:
            if (auto it = l1i_.find(core_id); it != l1i_.end())
                it->second.touch(cl);
            break;
        default: break;
        }
    }

private:
    tao_uarch::UarchProfile cfg_;
    std::unordered_map<uint64_t, LineMesi> lines_;
    std::unordered_map<uint32_t, tao_uarch::BankedSetAssocLRU> l1d_;
    std::unordered_map<uint32_t, tao_uarch::BankedSetAssocLRU> l1i_;
    std::unordered_map<uint32_t, tao_uarch::BankedSetAssocLRU> l2_;
    tao_uarch::BankedSetAssocLRU l3_;
    std::unordered_map<uint32_t, tao_uarch::TlbSim> dtlb_;
    std::unordered_map<uint32_t, tao_uarch::TlbSim> itlb_;
    std::unordered_map<uint32_t, tao_uarch::MshrTracker> l1d_mshr_;
    std::unordered_map<uint32_t, tao_uarch::MshrTracker> l1i_mshr_;
    tao_uarch::PageWalkSim walker_;

    // V10 i-side 独立视图（vaddr 域）：与 gem5 oracle 端 i-side 视图 1:1 对齐。
    //   d-side 使用 paddr key（来自 mem_events.commit/request），
    //   i-side 使用 vaddr key（来自 mem_events.ifetch.cacheline_addr_v）。
    //   各持一份 L2/L3/walker/lines，避免跨域污染。
    std::unordered_map<uint32_t, tao_uarch::BankedSetAssocLRU> l2_i_;
    tao_uarch::BankedSetAssocLRU l3_i_;
    tao_uarch::PageWalkSim i_walker_;
    std::unordered_map<uint64_t, LineMesi> i_lines_;

    // V9.5：与 gem5 探针 recent_line_count_ 同语义，全局（非 per-core）
    // 计数器；先读再 +1，clip 至 0..3，作为 same_line_recent 字段。
    std::unordered_map<uint64_t, uint32_t> recent_line_count_;

    // 与 gem5 tao_trace.cc:bucketCount 完全同公式：0/1/2/3-7/8+
    static uint8_t bucketCount(size_t n)
    {
        if (n == 0) return 0;
        if (n == 1) return 1;
        if (n == 2) return 2;
        if (n <= 7) return 3;
        return 4;
    }
};

} // namespace mesi_ref
