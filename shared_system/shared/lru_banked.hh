// lru_banked.hh —— Oracle / ref_sim 共用的微架构组件实现：
//   - BankedSetAssocLRU：N-bank × M-set × W-way 组关联 LRU
//   - TlbSim：TLB（按 VPN）
//   - PageWalkSim：x86 多级页表合成访问，喂给 LRU 链
//   - MshrTracker：outstanding miss 表（A2 coalescing）
//
// 所有类按 UarchProfile 实参化，禁止硬编码任何容量。
//
// 同源约束：本头同时被 tao_trace.cc 与 simulator.hpp include；
// 任何变更必须保持 oracle ↔ ref_sim 在相同 UarchProfile 下 bit-exact。

#pragma once

#include <algorithm>
#include <cstdint>
#include <cstring>
#include <functional>
#include <list>
#include <unordered_map>
#include <unordered_set>
#include <vector>

#include "uarch_profile.hh"

namespace tao_uarch {

// ====================== BankedSetAssocLRU ======================
//
// bank_index = (cl_addr >> bank_select_low_bit_minus_line_bits) & (num_banks - 1)
//   注意 cl_addr 是已经右移 cacheline_bits 后的"行号"。
//   bank_select_low_bit 是字节级位（如 ruby num_l3_banks=4, bank_select_low_bit=6
//   表示从第 6 位开始选 bank），因此映射到行号空间需要减去 cacheline_bits。
//
// set_index 在 (cl_addr >> bank_bits) 之后再取 sets_per_bank 取模。
// way LRU 在每 (bank, set) 内部独立维护。

class BankedSetAssocLRU {
public:
    BankedSetAssocLRU() = default;

    void configure(const CacheCfg& cfg) {
        cfg_ = cfg;
        num_banks_ = cfg.num_banks;
        if (num_banks_ == 0) num_banks_ = 1;
        sets_per_bank_ = (uint32_t)cfg.sets_per_bank();
        ways_ = cfg.assoc;
        bank_bits_ = ilog2(num_banks_);
        line_bits_ = cfg.cacheline_bits();
        // bank 选位字节 → 行号位：byte bit b → line bit (b - line_bits_)
        if (cfg.bank_select_low_bit < line_bits_) {
            bank_shift_in_lineaddr_ = 0;
        } else {
            bank_shift_in_lineaddr_ = cfg.bank_select_low_bit - line_bits_;
        }
        sets_.assign(num_banks_,
                     std::vector<SetState>(sets_per_bank_));
    }

    // 输入 cl_byte_addr 是字节地址，内部右移 line_bits_ 得到行号。
    // 返回 hit/miss；evicted_byte_addr 在 miss 且发生驱逐时填入被驱逐的字节地址。
    bool touch(uint64_t cl_byte_addr, int64_t* evicted_byte_addr = nullptr) {
        uint64_t line = cl_byte_addr >> line_bits_;
        uint32_t bank = bankOf(line);
        uint32_t set  = setOf(line, bank);
        SetState& ss  = sets_[bank][set];
        // 查 way
        for (auto it = ss.order.begin(); it != ss.order.end(); ++it) {
            if (*it == line) {
                ss.order.erase(it);
                ss.order.push_front(line);
                return true;
            }
        }
        // miss
        ss.order.push_front(line);
        int64_t ev_line = -1;
        if (ss.order.size() > ways_) {
            ev_line = (int64_t)ss.order.back();
            ss.order.pop_back();
        }
        if (evicted_byte_addr) {
            *evicted_byte_addr =
                (ev_line < 0) ? -1 : (ev_line << line_bits_);
        }
        return false;
    }

    bool contains(uint64_t cl_byte_addr) const {
        uint64_t line = cl_byte_addr >> line_bits_;
        uint32_t bank = bankOf(line);
        uint32_t set  = setOf(line, bank);
        const SetState& ss = sets_[bank][set];
        for (uint64_t l : ss.order)
            if (l == line) return true;
        return false;
    }

    void invalidate(uint64_t cl_byte_addr) {
        uint64_t line = cl_byte_addr >> line_bits_;
        uint32_t bank = bankOf(line);
        uint32_t set  = setOf(line, bank);
        SetState& ss  = sets_[bank][set];
        for (auto it = ss.order.begin(); it != ss.order.end(); ++it) {
            if (*it == line) { ss.order.erase(it); return; }
        }
    }

    uint32_t numBanks() const { return num_banks_; }
    uint32_t setsPerBank() const { return sets_per_bank_; }
    uint32_t ways() const { return ways_; }
    // P0-A: 暴露 (cl_byte_addr -> bank_id) 用于 d_bank_id / i_bank_id 字段输出。
    //   口径与内部 bankOf() 完全一致（同一行号 → 同一 bank），保证
    //   ref_sim 与 gem5 oracle bit-exact。
    uint32_t bankIdOf(uint64_t cl_byte_addr) const {
        if (num_banks_ == 1) return 0;
        uint64_t line = cl_byte_addr >> line_bits_;
        return (uint32_t)((line >> bank_shift_in_lineaddr_)
                          & (num_banks_ - 1));
    }
    // V10.3 A 字段：探针 (cl_byte_addr -> set_residency, lru_pos)。
    //   *不修改* LRU 链状态——仅查询。所以可在 oracle/refsim 任意位置
    //   调用，且两端用相同公式 → bit-exact。
    //
    //   set_residency: 当前 (bank, set) 内有效 way 的数量（0..ways_）
    //   lru_pos:       当前 cl 在该 set LRU 链中的位置：
    //                    0 = MRU (最近)
    //                    ways_-1 = 链尾 (最老的)
    //                    ways_ = miss（cl 不在 set 中）
    //
    //   注意：在 touch() 之前调用 → 反映"访问前"的 set 状态，
    //   这是我们希望的语义（特征是"输入"，不是 promote 后的结果）。
    void peekSetState(uint64_t cl_byte_addr,
                      uint32_t* out_residency,
                      uint32_t* out_lru_pos) const {
        if (sets_.empty()) {
            if (out_residency) *out_residency = 0;
            if (out_lru_pos)   *out_lru_pos   = ways_;
            return;
        }
        uint64_t line = cl_byte_addr >> line_bits_;
        uint32_t bank = bankOf(line);
        uint32_t set  = setOf(line, bank);
        const SetState& ss = sets_[bank][set];
        uint32_t res = (uint32_t)ss.order.size();
        uint32_t pos = ways_;  // miss 默认
        uint32_t i = 0;
        for (uint64_t l : ss.order) {
            if (l == line) { pos = i; break; }
            ++i;
        }
        if (out_residency) *out_residency = res;
        if (out_lru_pos)   *out_lru_pos   = pos;
    }
    // configured() 用于上层做 lazy init 判断。num_banks_ 默认 1（用于
    //   未显式 configure 时不立刻 div-by-zero），故不能用 numBanks()==0；
    //   sets_ 是 configure() 后才被 resize 的 vector，empty 即未配置。
    bool configured() const { return !sets_.empty(); }

private:
    struct SetState {
        std::list<uint64_t> order;  // MRU at front, line addrs (cacheline-shifted)
    };
    CacheCfg cfg_{};
    uint32_t num_banks_ = 1;
    uint32_t sets_per_bank_ = 1;
    uint32_t ways_ = 1;
    uint32_t line_bits_ = 6;
    uint32_t bank_shift_in_lineaddr_ = 0;
    uint32_t bank_bits_ = 0;
    std::vector<std::vector<SetState>> sets_;

    static uint32_t ilog2(uint32_t v) {
        uint32_t r = 0;
        while (v > 1) { v >>= 1; ++r; }
        return r;
    }
    uint32_t bankOf(uint64_t line) const {
        if (num_banks_ == 1) return 0;
        return (uint32_t)((line >> bank_shift_in_lineaddr_)
                          & (num_banks_ - 1));
    }
    uint32_t setOf(uint64_t line, uint32_t bank) const {
        // 把 bank 选位剔除后再 mod sets_per_bank
        uint64_t reduced = (line >> (bank_shift_in_lineaddr_ + bank_bits_))
                           << bank_shift_in_lineaddr_;
        // 保留 bank 选位下方部分（属于 set/offset 区域）
        uint64_t lo = line & ((1ull << bank_shift_in_lineaddr_) - 1);
        uint64_t set_addr = reduced | lo;
        return (uint32_t)(set_addr % sets_per_bank_);
    }
};


// ====================== TlbSim ======================
//
// VPN = byte_addr >> page_size_bits
// 内部就用 BankedSetAssocLRU 复用：把 TLB 看作 entries=lines, banks=1，
// page_size_bits 取代 cacheline_bits。

class TlbSim {
public:
    void configure(const TlbCfg& tcfg, uint32_t page_size_bits) {
        page_size_bits_ = page_size_bits;
        // 构造 CacheCfg 喂给 BankedSetAssocLRU
        CacheCfg c;
        c.size_b   = (uint64_t)tcfg.entries; // 每 "line" = 1 entry
        c.assoc    = tcfg.assoc;
        c.line_b   = 1;                       // 用 1 表示"按 entry"
        c.num_banks = 1;
        c.bank_select_low_bit = 0;
        c.policy = "lru";
        // 但 BankedSetAssocLRU 假设 cacheline_bits 由 line_b 推出（=0），
        // 我们改为内部直接管理 VPN（不复用 BankedSetAssocLRU），
        // 因为 line_b=1 不是 2 的幂会让 cacheline_bits 计算异常。
        // → 自维护一个 set-assoc LRU。
        entries_ = tcfg.entries;
        assoc_   = tcfg.assoc;
        sets_    = entries_ / assoc_;
        order_.assign(sets_, std::list<uint64_t>{});
    }

    // 返回 hit
    bool translate(uint64_t byte_addr) {
        uint64_t vpn = byte_addr >> page_size_bits_;
        uint64_t set_idx = (sets_ == 0) ? 0 : (vpn % sets_);
        auto& ord = order_[set_idx];
        for (auto it = ord.begin(); it != ord.end(); ++it) {
            if (*it == vpn) {
                ord.erase(it);
                ord.push_front(vpn);
                return true;
            }
        }
        ord.push_front(vpn);
        if (ord.size() > assoc_) ord.pop_back();
        return false;
    }

    uint32_t pageSizeBits() const { return page_size_bits_; }
    uint32_t entries() const { return entries_; }

private:
    uint32_t page_size_bits_ = 12;
    uint32_t entries_ = 0;
    uint32_t assoc_ = 0;
    uint32_t sets_ = 0;
    std::vector<std::list<uint64_t>> order_;
};


// ====================== MshrTracker ======================
//
// 维护 outstanding 表：cacheline byte_addr → last_seen_seq。
// 第一次出现 → unique miss，记 1；后续在 capacity / window 内出现 → coalesced。
// 完成事件 → erase。

class MshrTracker {
public:
    void configure(uint32_t capacity, uint32_t window_seq = 0) {
        capacity_ = capacity;
        window_seq_ = window_seq;
        outstanding_.clear();
    }

    // arrival 时调用。返回 true 表示这是新 unique miss；false 表示 coalesced。
    bool insert(uint64_t cl_byte_addr, uint64_t seq) {
        // 先按 capacity / window 清扫
        evictStale(seq);
        auto it = outstanding_.find(cl_byte_addr);
        if (it != outstanding_.end()) {
            it->second = seq;  // 更新 last_seen
            return false;
        }
        outstanding_[cl_byte_addr] = seq;
        return true;
    }

    // 完成事件：移出 outstanding。
    void retire(uint64_t cl_byte_addr) {
        outstanding_.erase(cl_byte_addr);
    }

    bool contains(uint64_t cl_byte_addr) const {
        return outstanding_.count(cl_byte_addr) != 0;
    }

    size_t size() const { return outstanding_.size(); }

private:
    uint32_t capacity_ = 0;     // 0 = 无容量限制（仅 window）
    uint32_t window_seq_ = 0;   // 0 = 不基于 seq 窗口
    std::unordered_map<uint64_t, uint64_t> outstanding_;

    void evictStale(uint64_t seq) {
        // 按 capacity 限：直接 prune 最旧
        if (capacity_ > 0 && outstanding_.size() >= capacity_) {
            // 找最小 last_seen 直接弹一个
            auto victim = outstanding_.end();
            uint64_t oldest = UINT64_MAX;
            for (auto it = outstanding_.begin();
                 it != outstanding_.end(); ++it) {
                if (it->second < oldest) {
                    oldest = it->second;
                    victim = it;
                }
            }
            if (victim != outstanding_.end()) outstanding_.erase(victim);
        }
        // window：清掉超过 window_seq 的
        if (window_seq_ > 0) {
            for (auto it = outstanding_.begin();
                 it != outstanding_.end(); ) {
                if (seq > it->second && seq - it->second > window_seq_)
                    it = outstanding_.erase(it);
                else
                    ++it;
            }
        }
    }
};


// ====================== PageWalkSim ======================
//
// 当 TLB miss 时，按 cfg.walker.levels 合成 N 个 page-table 物理 cacheline 访问，
// 喂给 (l1d_lru, l2_lru, l3_lru) 链。每级 PT 的伪物理地址：
//   pt_pa(level, vpn) = splitmix64(vpn ^ (level * 0xCC9E2D51u))
// 最终命中层级以 PageWalkResult 返回。
//
// 不建模 PWC；如果 cfg.pwc_entries > 0 则在前面再加一层近似命中。

class PageWalkSim {
public:
    struct WalkResult {
        uint32_t levels = 0;       // 实际发了几次访问
        uint32_t hit_l1d = 0;
        uint32_t hit_l2  = 0;
        uint32_t hit_l3  = 0;
        uint32_t miss_dram = 0;    // 走到 DRAM 的次数（即 LLC miss）
    };

    void configure(const WalkerCfg& cfg) {
        cfg_ = cfg;
    }

    // 对每核共用一份，参数传入 per-core l1d / l2 / shared l3。
    WalkResult walk(uint64_t byte_addr,
                    BankedSetAssocLRU& l1d,
                    BankedSetAssocLRU& l2,
                    BankedSetAssocLRU& l3) {
        return walkWithL3(byte_addr, l1d, l2, l3);
    }

    template <typename L3Like>
    WalkResult walkWithL3(uint64_t byte_addr,
                          BankedSetAssocLRU& l1d,
                          BankedSetAssocLRU& l2,
                          L3Like& l3) {
        WalkResult r;
        uint64_t vpn = byte_addr >> cfg_.page_size_bits;
        for (uint32_t lvl = 0; lvl < cfg_.levels; ++lvl) {
            uint64_t pa = synthPtAddr(vpn, lvl);
            ++r.levels;
            // 依次查 L1D → L2 → L3
            if (l1d.touch(pa)) { ++r.hit_l1d; continue; }
            if (l2.touch(pa))  { ++r.hit_l2;  continue; }
            if (l3.touch(pa))  { ++r.hit_l3;  continue; }
            ++r.miss_dram;
        }
        return r;
    }

    const WalkerCfg& cfg() const { return cfg_; }

private:
    WalkerCfg cfg_{};

    // splitmix64 hash
    static uint64_t splitmix64(uint64_t x) {
        x += 0x9E3779B97F4A7C15ull;
        x = (x ^ (x >> 30)) * 0xBF58476D1CE4E5B9ull;
        x = (x ^ (x >> 27)) * 0x94D049BB133111EBull;
        return x ^ (x >> 31);
    }
    uint64_t synthPtAddr(uint64_t vpn, uint32_t level) const {
        // 不同 level 用不同盐，保证落入不同 cacheline
        uint64_t salt = (uint64_t)level * 0xCC9E2D51u;
        uint64_t h = splitmix64(vpn ^ salt);
        // 对齐到 64B
        return h & ~uint64_t(63);
    }
};

} // namespace tao_uarch
