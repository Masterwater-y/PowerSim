#include <cstdint>
#include <deque>
#include <cstring>
#include <memory>
#include <string>
#include <unordered_map>
#include <vector>

#include <pybind11/numpy.h>
#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

#include "quantum.hpp"
#include "simulator.hpp"
#include "uarch_profile.hh"

namespace py = pybind11;

namespace {

// ====================== E.2：WindowState（C++ port of OnlineWindowFeatures）======================
//
// 严格按位对齐 driver/windowed_features.py：
//   - win64       : 长度 64，每个元素 (mem_count, br_count, cl, pc, bank_id)
//   - win256_cl   : 长度 256，单 cl
//   - win1024_cl  : 长度 1024，单 cl
//   - win256_dram : 长度 256，(dram_bank, dram_row)
// derive() 不改状态；update()/trim() 改状态。
//
struct WindowState {
    struct Ev64 { bool mem; bool br; uint64_t cl; uint64_t pc; uint32_t bank; };
    struct EvDram { uint32_t bank; uint64_t row; };

    std::deque<Ev64>         win64;
    std::deque<uint64_t>     win256_cl;
    std::deque<uint64_t>     win1024_cl;
    std::deque<EvDram>       win256_dram;

    std::unordered_map<uint64_t, uint32_t> cl_count64;
    std::unordered_map<uint64_t, uint32_t> pc_count64;
    std::unordered_map<uint32_t, uint32_t> bank_count64;
    std::unordered_map<uint64_t, uint32_t> cl_count256;
    std::unordered_map<uint64_t, uint32_t> cl_count1024;
    std::unordered_map<uint32_t, uint32_t> dram_bank_count;
    std::unordered_map<uint64_t, uint32_t> dram_row_count;
    std::unordered_map<uint64_t, uint64_t> last_cl_pos;

    int64_t  last_branch_pos = -1;
    uint64_t pos             = 0;
    uint32_t mem_count64     = 0;
    uint32_t br_count64      = 0;

    static constexpr uint32_t DRAM_BANKS = 16;  // 与 Python 默认一致
    static constexpr uint64_t ROW_BYTES  = 8192;

    static int bitlen_minus1(uint64_t x) {
        if (x == 0) return 0;
        // floor(log2(x)) for x>=1, equiv to (x.bit_length()-1) in Python
        int n = 0;
        while (x > 1) { x >>= 1; ++n; }
        return n;
    }

    // derive_before_update: 不改状态，写入 12 个 key 到 dict。
    // cl = cacheline_paddr (用于 win64/256/1024 key)；pa = paddr (用于 dram bank/row)。
    void deriveInto(py::dict &out, bool is_load, bool is_store, bool is_atomic,
                    uint64_t cl, uint64_t pc, uint32_t bk, uint64_t pa) const {
        bool cur_mem = is_load || is_store || is_atomic;
        uint32_t dram_bank = uint32_t((pa >> 6) & (DRAM_BANKS - 1));
        uint64_t row_idx   = ROW_BYTES > 0 ? (pa / ROW_BYTES) : 0;

        auto safe_get = [](const auto &m, auto k) -> uint32_t {
            auto it = m.find(k);
            return it == m.end() ? 0u : it->second;
        };

        uint32_t mem_density = mem_count64 < 32767 ? mem_count64 : 32767;
        uint32_t br_density  = br_count64  < 32767 ? br_count64  : 32767;
        uint32_t unique64    = uint32_t(cl_count64.size());
        if (unique64 > 32767) unique64 = 32767;
        uint32_t unique256   = uint32_t(cl_count256.size());
        if (unique256 > 255)  unique256 = 255;
        uint32_t unique1024  = uint32_t(cl_count1024.size());
        if (unique1024 > 2047) unique1024 = 2047;
        uint32_t pc_freq     = std::min<uint32_t>(safe_get(pc_count64, pc), 32767);
        uint32_t bank_conf   = cur_mem ? std::min<uint32_t>(safe_get(bank_count64, bk), 32767) : 0;
        uint32_t dram_bank_f = cur_mem ? std::min<uint32_t>(safe_get(dram_bank_count, dram_bank), 255) : 0;
        uint32_t dram_row_f  = cur_mem ? std::min<uint32_t>(safe_get(dram_row_count, row_idx), 255) : 0;

        uint32_t reuse_log = 15;
        if (cur_mem) {
            auto it = last_cl_pos.find(cl);
            if (it != last_cl_pos.end()) {
                uint64_t dist = pos - it->second;
                int v = dist > 0 ? bitlen_minus1(dist) : 0;
                reuse_log = uint32_t(v < 0 ? 0 : (v > 15 ? 15 : v));
            }
        }
        uint32_t branch_log = 15;
        if (last_branch_pos >= 0) {
            uint64_t dist = pos - uint64_t(last_branch_pos);
            int v = dist > 0 ? bitlen_minus1(dist) : 0;
            branch_log = uint32_t(v < 0 ? 0 : (v > 15 ? 15 : v));
        }

        out["mem_density_W64"]          = mem_density;
        out["branch_density_W64"]       = br_density;
        out["unique_cl_W64"]            = unique64;
        out["pc_freq_W64"]              = pc_freq;
        out["bank_conflict_W64"]        = bank_conf;
        out["unique_cl_W256"]           = unique256;
        out["unique_cl_W1024"]          = unique1024;
        out["dram_bank_id"]             = uint32_t(dram_bank < 15 ? dram_bank : 15);
        out["dram_bank_freq_W256"]      = dram_bank_f;
        out["dram_row_freq_W256"]       = dram_row_f;
        out["cl_reuse_dist_log"]        = reuse_log;
        out["time_since_last_branch_log"] = branch_log;
    }

    static void decMap(std::unordered_map<uint64_t, uint32_t> &m, uint64_t k) {
        auto it = m.find(k);
        if (it == m.end()) return;
        if (it->second <= 1) m.erase(it); else --it->second;
    }
    static void decMap32(std::unordered_map<uint32_t, uint32_t> &m, uint32_t k) {
        auto it = m.find(k);
        if (it == m.end()) return;
        if (it->second <= 1) m.erase(it); else --it->second;
    }

    void update(bool is_load, bool is_store, bool is_atomic, bool is_branch,
                uint64_t cl, uint64_t pc, uint32_t bk, uint64_t pa) {
        bool cur_mem = is_load || is_store || is_atomic;
        uint32_t dram_bank = uint32_t((pa >> 6) & (DRAM_BANKS - 1));
        uint64_t row_idx   = ROW_BYTES > 0 ? (pa / ROW_BYTES) : 0;

        win64.push_back(Ev64{cur_mem, is_branch, cl, pc, bk});
        if (cur_mem) ++mem_count64;
        if (is_branch) ++br_count64;
        ++pc_count64[pc];
        if (cur_mem) {
            ++cl_count64[cl];
            last_cl_pos[cl] = pos;
            ++bank_count64[bk];
            win256_cl.push_back(cl);
            ++cl_count256[cl];
            win1024_cl.push_back(cl);
            ++cl_count1024[cl];
            win256_dram.push_back(EvDram{dram_bank, row_idx});
            ++dram_bank_count[dram_bank];
            ++dram_row_count[row_idx];
        }
        if (is_branch) last_branch_pos = int64_t(pos);

        // _trim
        while (win64.size() > 64) {
            Ev64 ev = win64.front();
            win64.pop_front();
            if (ev.mem) --mem_count64;
            if (ev.br) --br_count64;
            decMap(pc_count64, ev.pc);
            if (ev.mem) {
                decMap(cl_count64, ev.cl);
                decMap32(bank_count64, ev.bank);
            }
        }
        while (win256_cl.size() > 256) {
            uint64_t ev = win256_cl.front();
            win256_cl.pop_front();
            decMap(cl_count256, ev);
        }
        while (win1024_cl.size() > 1024) {
            uint64_t ev = win1024_cl.front();
            win1024_cl.pop_front();
            decMap(cl_count1024, ev);
        }
        while (win256_dram.size() > 256) {
            EvDram ev = win256_dram.front();
            win256_dram.pop_front();
            decMap32(dram_bank_count, ev.bank);
            decMap(dram_row_count, ev.row);
        }
        ++pos;
    }
};

py::dict dsideToDict(const mesi_ref::DSideOracle &d) {
    py::dict o;
    o["mesi_before"] = unsigned(d.mesi_before);
    o["coh_oracle"] = unsigned(d.coh_oracle);
    o["sharer_bucket"] = unsigned(d.sharer_bucket);
    o["owner_dist"] = unsigned(d.owner_dist);
    o["dirty_owner"] = unsigned(d.dirty_owner);
    o["path_class"] = unsigned(d.path_class);
    o["inval_fanout"] = unsigned(d.inval_fanout);
    o["same_line_recent"] = unsigned(d.same_line_recent);
    o["oracle_source"] = unsigned(d.oracle_source);
    o["d_mshr_depth"] = unsigned(d.d_mshr_depth);
    o["dtlb_hit"] = unsigned(d.dtlb_hit);
    o["d_walker_levels"] = unsigned(d.d_walker_levels);
    o["d_walker_dram_misses"] = unsigned(d.d_walker_dram_misses);
    o["d_bank_id"] = unsigned(d.d_bank_id);
    o["d_llc_set_residency"] = unsigned(d.d_llc_set_residency);
    o["d_llc_set_lru_pos"] = unsigned(d.d_llc_set_lru_pos);
    return o;
}

py::dict isideToDict(const mesi_ref::IFetchResult &r) {
    py::dict o;
    o["i_path_class"] = unsigned(r.i_path_class);
    o["i_coh_oracle"] = unsigned(r.i_coh_oracle);
    o["i_mesi_before"] = unsigned(r.i_mesi_before);
    o["i_oracle_source"] = 0;
    o["i_mshr_depth"] = unsigned(r.i_mshr_depth);
    o["itlb_hit"] = unsigned(r.itlb_hit);
    o["i_walker_levels"] = unsigned(r.i_walker_levels);
    o["i_walker_dram_misses"] = unsigned(r.i_walker_dram_misses);
    o["i_bank_id"] = unsigned(r.i_bank_id);
    o["i_llc_set_residency"] = unsigned(r.i_llc_set_residency);
    o["i_llc_set_lru_pos"] = unsigned(r.i_llc_set_lru_pos);
    return o;
}

mesi_ref::IFetchResult zeroIFetchResult() {
    return mesi_ref::IFetchResult{};
}

class PyRefSim {
public:
    explicit PyRefSim(const std::string &uarch_profile_path)
        : cfg_(tao_uarch::UarchProfile::load(uarch_profile_path)),
          sim_(std::make_unique<mesi_ref::Simulator>(cfg_)) {}

    py::dict on_ifetch(uint32_t core_id, uint64_t macro_pc_cl) {
        return isideToDict(sim_->stepIFetch(core_id, macro_pc_cl));
    }

    py::dict on_mem_access(uint32_t core_id,
                           uint64_t paddr,
                           bool is_store,
                           uint16_t size,
                           uint64_t seq = 0,
                           uint32_t thread_id = 0) {
        mesi_ref::Simulator::Event ev;
        ev.seq = seq;
        ev.core_id = core_id;
        ev.thread_id = thread_id;
        ev.cacheline_addr = paddr & ~uint64_t(63);
        ev.is_store = is_store;
        ev.size = size;
        return dsideToDict(sim_->step(ev));
    }

    py::dict on_request(uint32_t core_id,
                        uint64_t paddr,
                        bool is_store,
                        uint16_t size,
                        uint64_t seq = 0,
                        uint32_t thread_id = 0) {
        return on_mem_access(core_id, paddr, is_store, size, seq, thread_id);
    }

    void on_commit(uint32_t, uint64_t = 0) {
        // Current deploy-side ref_sim state is advanced by ifetch/request.
        // Commit is kept as a stable API hook for future scheduler integration.
    }

private:
    tao_uarch::UarchProfile cfg_;
    std::unique_ptr<mesi_ref::Simulator> sim_;
};

// ====================== Quantum-based 暴露面（B.1 / B.2） ======================
//
// PyCoordinator 负责持有 mesi_ref::quantum::Coordinator；
// PyLocalRefSim 通过 core_id 引用 PyCoordinator 内部对应的 LocalRefSim。
// 二者一起替代 PyRefSim，作为 driver Phase B 的真 speculative 后端。
// 旧 PyRefSim 仍然保留，driver 在 Phase A 路径上仍然可用。
//

class PyCoordinator;

class PyLocalRefSim {
public:
    PyLocalRefSim(PyCoordinator *pc, uint32_t core_id);

    py::dict on_ifetch_speculative(uint64_t macro_pc_cl);
    py::dict on_mem_access_speculative(uint64_t paddr, bool is_store,
                                       uint16_t size, uint64_t seq,
                                       uint32_t thread_id);
    void commit_speculative(uint64_t paddr, bool is_store, uint16_t size,
                            uint64_t seq, uint32_t thread_id);
    void commit_ifetch_speculative(uint64_t macro_pc_cl);

    // E.1：每核每 quantum 一次 pybind 调用。
    //   fields: list[tuple(macro_pc, paddr, cacheline_paddr,
    //                      is_load, is_store, is_atomic, is_branch,
    //                      size, micro_seq, thread_id)]
    //   返回: list[dict]，每个 dict 是 i_attrs + d_attrs + win_attrs 合并；
    //         非 mem 时 d 字段全 0（等价于原 D_ZERO 模板）。
    //   E.2：win_attrs 由 C++ 端 WindowState::deriveInto 计算（不改状态）。
    //   当前 functional_parquet 输入下，driver 不再维护 i-side 状态机；
    //   因此 i_attrs 统一返回 0 值占位，不再调用 probeIFetch。
    py::list batch_probe(py::list fields);

    // F.1: mock/label 热路径专用 POD 接口。输入是 SoA ndarray 切片，
    // 不构造 tuple/dict；返回每行 d_bank_id（非 mem 为 0）。ckpt 模型路径
    // 仍走 batch_probe 取完整 oracle/win dict。
    py::array_t<uint32_t> batch_probe_pod(
        py::array_t<uint64_t, py::array::c_style | py::array::forcecast> macro_pc,
        py::array_t<uint64_t, py::array::c_style | py::array::forcecast> paddr,
        py::array_t<uint64_t, py::array::c_style | py::array::forcecast> cl_paddr,
        py::array_t<uint8_t, py::array::c_style | py::array::forcecast> is_load,
        py::array_t<uint8_t, py::array::c_style | py::array::forcecast> is_store,
        py::array_t<uint8_t, py::array::c_style | py::array::forcecast> is_atomic,
        py::array_t<uint8_t, py::array::c_style | py::array::forcecast> is_branch,
        py::array_t<uint16_t, py::array::c_style | py::array::forcecast> size,
        py::array_t<int64_t, py::array::c_style | py::array::forcecast> micro_seq,
        py::array_t<int32_t, py::array::c_style | py::array::forcecast> thread_id);

    // E.2: 一次性 commit K 条 win.update。bool 数组与 batch_probe 的
    // fields 顺序一致；committed[i]=true 表示第 i 行被 phase1c 提交，
    // 需要进入 win 累积；false 表示该行为 unconsumed，跳过。
    void batch_window_update(py::list fields, py::list committed_mask);

    // E.6: 一次性执行 phase1c 的 deadline walk + ReferenceClock + win.update。
    // fields11: list[tuple(batch_probe 10 fields + d_bank_id_post_probe)]
    // 返回 dict:
    //   consumed, fetch_clock, ready_clock, fetch_clock_base,
    //   macro_inc, uop_inc, max_abs_fetch_diff, events
    // events 元素为 (t, core_id, seq, payload)，payload 是 json dict 或 56B bin。
    py::dict commit_quantum(py::list fields11, py::list fetch_lats,
                            py::list exec_lats, py::list mispreds,
                            py::list is_microop, py::list is_last_microop,
                            py::list label_fetch_ticks,
                            double first_fetch_tick,
                            double fetch_clock, double ready_clock,
                            double fetch_clock_base,
                            uint64_t committed_this_quantum,
                            uint32_t delta_t, bool label_driven,
                            bool emit_bin);

    // F.1b: commit_quantum 的 ndarray/POD 版本，避免 Python 构造 fields11 /
    // fetch_lats / exec_lats / ... 小 list。仅 mock/label fast path 使用。
    py::dict commit_quantum_pod(
        py::array_t<uint64_t, py::array::c_style | py::array::forcecast> macro_pc,
        py::array_t<uint64_t, py::array::c_style | py::array::forcecast> paddr,
        py::array_t<uint64_t, py::array::c_style | py::array::forcecast> cl_paddr,
        py::array_t<uint8_t, py::array::c_style | py::array::forcecast> is_load,
        py::array_t<uint8_t, py::array::c_style | py::array::forcecast> is_store,
        py::array_t<uint8_t, py::array::c_style | py::array::forcecast> is_atomic,
        py::array_t<uint8_t, py::array::c_style | py::array::forcecast> is_branch,
        py::array_t<int64_t, py::array::c_style | py::array::forcecast> micro_seq,
        py::array_t<int32_t, py::array::c_style | py::array::forcecast> thread_id,
        py::array_t<uint32_t, py::array::c_style | py::array::forcecast> d_bank_id,
        py::array_t<double, py::array::c_style | py::array::forcecast> fetch_lats,
        py::array_t<double, py::array::c_style | py::array::forcecast> exec_lats,
        py::array_t<double, py::array::c_style | py::array::forcecast> mispreds,
        py::array_t<uint8_t, py::array::c_style | py::array::forcecast> is_microop,
        py::array_t<uint8_t, py::array::c_style | py::array::forcecast> is_last_microop,
        py::array_t<double, py::array::c_style | py::array::forcecast> label_fetch_ticks,
        double first_fetch_tick,
        double fetch_clock, double ready_clock, double fetch_clock_base,
        uint64_t committed_this_quantum,
        uint32_t delta_t, bool label_driven,
        bool emit_bin);

    uint32_t core_id() const { return core_id_; }

private:
    PyCoordinator *pc_;
    uint32_t core_id_;
    std::unique_ptr<mesi_ref::quantum::LocalRefSim> backend_;
    // functional_parquet 无法重建真实 ifetch 流；driver 侧 i_attrs 统一走
    // 0 值占位，不再维护 i-cache 状态机。
    mesi_ref::IFetchResult zero_i_ = zeroIFetchResult();
    // E.2: per-core 滑动窗特征状态（取代 driver 端 OnlineWindowFeatures）。
    WindowState win_;
};

class PyCoordinator {
public:
    explicit PyCoordinator(const std::string &uarch_profile_path)
        : cfg_(tao_uarch::UarchProfile::load(uarch_profile_path)),
          coord_(std::make_unique<mesi_ref::quantum::Coordinator>(cfg_)) {}

    std::shared_ptr<PyLocalRefSim> local(uint32_t core_id) {
        auto it = locals_.find(core_id);
        if (it != locals_.end()) return it->second;
        auto p = std::make_shared<PyLocalRefSim>(this, core_id);
        locals_[core_id] = p;
        // 同步在 quantum::Coordinator 上没有 per-core 注册的概念（中心化
        // simulator 通过 core_id 隐式分槽），所以这里只缓存 façade。
        return p;
    }

    // 所有 deltas 已被 stepForCore 实时执行；这里仅做 stub，转给 coord_。
    void reconcile(py::list /*deltas*/, py::list /*results*/) {
        // D.3：reconcile 纯 C++（overlay flush），整段释放 GIL。
        py::gil_scoped_release rel;
        std::vector<mesi_ref::quantum::LineDelta> ds;
        std::vector<mesi_ref::quantum::OracleResult> rs;
        coord_->reconcile(ds, rs);
    }

    py::dict drain_counters() {
        mesi_ref::quantum::CounterSnapshot s;
        coord_->drainCounters(s);
        py::dict o;
        o["llc_hits"]         = py::int_(s.llc_hits);
        o["llc_misses"]       = py::int_(s.llc_misses);
        o["cha_remote_clean"] = py::int_(s.cha_remote_clean);
        o["cha_remote_dirty"] = py::int_(s.cha_remote_dirty);
        o["wb_required"]      = py::int_(s.wb_required);
        o["inval_fanout_sum"] = py::int_(s.inval_fanout_sum);
        auto pack_pc = [&](const std::unordered_map<uint32_t, uint64_t> &m) {
            py::dict d;
            for (auto &kv : m) d[py::int_(kv.first)] = py::int_(kv.second);
            return d;
        };
        o["l1d_hits"]            = pack_pc(s.l1d_hits);
        o["l1d_misses"]          = pack_pc(s.l1d_misses);
        o["l2_hits"]             = pack_pc(s.l2_hits);
        o["l2_misses"]           = pack_pc(s.l2_misses);
        o["l1i_hits"]            = pack_pc(s.l1i_hits);
        o["l1i_misses"]          = pack_pc(s.l1i_misses);
        o["dtlb_misses"]         = pack_pc(s.dtlb_misses);
        o["itlb_misses"]         = pack_pc(s.itlb_misses);
        o["walker_dram_misses"]  = pack_pc(s.walker_dram_misses);
        py::dict pmu;
        pmu["l1d.loads"] = py::int_(s.pmu_l1d_loads);
        pmu["l1d.stores"] = py::int_(s.pmu_l1d_stores);
        pmu["l1d.load_misses"] = py::int_(s.pmu_l1d_load_misses);
        pmu["l1d.store_misses"] = py::int_(s.pmu_l1d_store_misses);
        pmu["l2.misses"] = py::int_(s.pmu_l2_misses);
        pmu["llc.load_misses"] = py::int_(s.pmu_llc_load_misses);
        pmu["llc.store_misses"] = py::int_(s.pmu_llc_store_misses);
        pmu["cha.requests.reads"] = py::int_(s.pmu_cha_requests_reads);
        pmu["cha.requests.writes"] = py::int_(s.pmu_cha_requests_writes);
        pmu["cha.tor_inserts.ia_miss_drd"] = py::int_(s.pmu_cha_tor_inserts_ia_miss_drd);
        pmu["cha.dir_lookup.snp"] = py::int_(s.pmu_cha_dir_lookup_snp);
        pmu["cha.core_snp.any_one"] = py::int_(s.pmu_cha_core_snp_any_one);
        o["pmu"] = std::move(pmu);
        return o;
    }

    mesi_ref::quantum::Coordinator *coord() { return coord_.get(); }

private:
    tao_uarch::UarchProfile cfg_;
    std::unique_ptr<mesi_ref::quantum::Coordinator> coord_;
    std::unordered_map<uint32_t, std::shared_ptr<PyLocalRefSim>> locals_;
};

// PyLocalRefSim 方法实现（在 PyCoordinator 之后，因为依赖其 coord()）。
inline PyLocalRefSim::PyLocalRefSim(PyCoordinator *pc, uint32_t core_id)
    : pc_(pc), core_id_(core_id),
      backend_(std::make_unique<mesi_ref::quantum::LocalRefSim>(
          pc->coord(), core_id)) {}

inline py::dict PyLocalRefSim::on_ifetch_speculative(uint64_t macro_pc_cl) {
    (void)macro_pc_cl;
    return isideToDict(zero_i_);
}

inline py::dict PyLocalRefSim::on_mem_access_speculative(uint64_t paddr,
                                                         bool is_store,
                                                         uint16_t size,
                                                         uint64_t seq,
                                                         uint32_t thread_id) {
    // D.3：C++ 计算段释放 GIL；dict 构造仍在 GIL 下。
    mesi_ref::quantum::OracleResult r;
    {
        py::gil_scoped_release rel;
        r = backend_->probe(paddr, is_store, size, seq, thread_id);
    }
    return dsideToDict(r.d);
}

inline void PyLocalRefSim::commit_speculative(uint64_t paddr, bool is_store,
                                              uint16_t size, uint64_t seq,
                                              uint32_t thread_id) {
    // D.3：commit 是纯 C++，整段释放 GIL。
    py::gil_scoped_release rel;
    backend_->commit(paddr, is_store, size, seq, thread_id);
}

inline void PyLocalRefSim::commit_ifetch_speculative(uint64_t macro_pc_cl) {
    (void)macro_pc_cl;
}

// ====================== E.1：批量 probe ======================
//
// 把 driver phase1a_probe 的 K 次 pybind 调用合并成单次调用。GIL 在
// 计算段释放，dict 构造重新持锁。当前 functional_parquet 输入下，driver
// 不再维护 i-side 状态机；i_attrs 固定为 0 值占位。
//
inline py::list PyLocalRefSim::batch_probe(py::list fields) {
    const std::size_t n = fields.size();

    // Step 1：在 GIL 下解析输入到 POD vector，避免在 release 块里访问 PyObject。
    struct In {
        uint64_t macro_pc;
        uint64_t paddr;
        uint64_t cl_paddr;
        bool     is_load;
        bool     is_store;
        bool     is_atomic;
        bool     is_branch;
        uint16_t size;
        uint64_t seq;
        uint32_t thread_id;
    };
    std::vector<In> in;
    in.reserve(n);
    for (const auto &item : fields) {
        py::tuple t = py::cast<py::tuple>(item);
        in.push_back(In{
            t[0].cast<uint64_t>(),
            t[1].cast<uint64_t>(),
            t[2].cast<uint64_t>(),
            t[3].cast<bool>(),
            t[4].cast<bool>(),
            t[5].cast<bool>(),
            t[6].cast<bool>(),
            t[7].cast<uint16_t>(),
            t[8].cast<uint64_t>(),
            t[9].cast<uint32_t>(),
        });
    }

    struct Out {
        bool has_d;
        mesi_ref::DSideOracle d;
        mesi_ref::IFetchResult i;
    };
    std::vector<Out> out(n);
    // E.2: window features 也在 release 段计算，与 oracle 一起返回。
    struct WinOut {
        uint32_t mem_density, br_density, unique64, pc_freq, bank_conf;
        uint32_t unique256, unique1024;
        uint32_t dram_bank_id, dram_bank_freq, dram_row_freq;
        uint32_t reuse_log, branch_log;
    };
    std::vector<WinOut> wins(n);

    // Step 2：纯 C++ 计算段，整段释放 GIL。
    {
        py::gil_scoped_release rel;
        for (std::size_t k = 0; k < n; ++k) {
            const In &x = in[k];
            (void)x.macro_pc;
            out[k].i = zero_i_;
            uint32_t bk = 0;
            if (x.is_load || x.is_store || x.is_atomic) {
                auto r = backend_->probe(x.paddr,
                                         x.is_store || x.is_atomic,
                                         x.size, x.seq, x.thread_id);
                out[k].d = r.d;
                out[k].has_d = true;
                bk = r.d.d_bank_id;
            } else {
                out[k].has_d = false;
            }
            // E.2: win features （不改 win_ 状态，等同 derive_before_update）。
            // Python 与 oracle 同步前后顺序：先 update prev cycle 完毕（commit）
            // 后才能 derive；本 batch_probe 仅 derive，update 在 phase1c 完毕后
            // 由 batch_window_update 调用。
            // 注意 win_ 在多次 derive 间不变，K 条 derive 都看到同一窗口快照。
            WinOut &w = wins[k];
            // 复用 deriveInto 但不写 dict —— 自己直接算一次（避免 GIL 内构造）。
            // 简化做法：直接写到一个临时 dict 不可行（需 GIL）。所以这里复制
            // 计算逻辑（与 deriveInto 一致），但只填 POD 结构。
            bool cur_mem = x.is_load || x.is_store || x.is_atomic;
            uint32_t dram_bank = uint32_t((x.paddr >> 6) & (WindowState::DRAM_BANKS - 1));
            uint64_t row_idx = WindowState::ROW_BYTES > 0 ? (x.paddr / WindowState::ROW_BYTES) : 0;
            auto get64 = [](const std::unordered_map<uint64_t, uint32_t> &m, uint64_t k_) {
                auto it = m.find(k_);
                return it == m.end() ? 0u : it->second;
            };
            auto get32 = [](const std::unordered_map<uint32_t, uint32_t> &m, uint32_t k_) {
                auto it = m.find(k_);
                return it == m.end() ? 0u : it->second;
            };
            w.mem_density = win_.mem_count64 < 32767 ? win_.mem_count64 : 32767;
            w.br_density  = win_.br_count64  < 32767 ? win_.br_count64  : 32767;
            uint32_t u64 = uint32_t(win_.cl_count64.size());
            w.unique64 = u64 > 32767 ? 32767 : u64;
            uint32_t u256 = uint32_t(win_.cl_count256.size());
            w.unique256 = u256 > 255 ? 255 : u256;
            uint32_t u1024 = uint32_t(win_.cl_count1024.size());
            w.unique1024 = u1024 > 2047 ? 2047 : u1024;
            uint32_t pcv = get64(win_.pc_count64, x.macro_pc);
            w.pc_freq = pcv > 32767 ? 32767 : pcv;
            uint32_t bkv = cur_mem ? get32(win_.bank_count64, bk) : 0;
            w.bank_conf = bkv > 32767 ? 32767 : bkv;
            uint32_t dbv = cur_mem ? get32(win_.dram_bank_count, dram_bank) : 0;
            w.dram_bank_freq = dbv > 255 ? 255 : dbv;
            uint32_t drv = cur_mem ? get64(win_.dram_row_count, row_idx) : 0;
            w.dram_row_freq = drv > 255 ? 255 : drv;
            w.dram_bank_id = dram_bank < 15 ? dram_bank : 15;
            w.reuse_log = 15;
            if (cur_mem) {
                auto it = win_.last_cl_pos.find(x.cl_paddr);
                if (it != win_.last_cl_pos.end()) {
                    uint64_t dist = win_.pos - it->second;
                    int v = dist > 0 ? WindowState::bitlen_minus1(dist) : 0;
                    w.reuse_log = uint32_t(v < 0 ? 0 : (v > 15 ? 15 : v));
                }
            }
            w.branch_log = 15;
            if (win_.last_branch_pos >= 0) {
                uint64_t dist = win_.pos - uint64_t(win_.last_branch_pos);
                int v = dist > 0 ? WindowState::bitlen_minus1(dist) : 0;
                w.branch_log = uint32_t(v < 0 ? 0 : (v > 15 ? 15 : v));
            }
            // 缓存 d_bank_id 让 phase1c 的 win.update 用同一个 bank id。
            (void)bk;
        }
    }

    // Step 3：在 GIL 下构造 dict 列表返回。
    py::list result(n);
    for (std::size_t k = 0; k < n; ++k) {
        py::dict o = isideToDict(out[k].i);
        if (out[k].has_d) {
            const auto &d = out[k].d;
            o["mesi_before"]          = unsigned(d.mesi_before);
            o["coh_oracle"]           = unsigned(d.coh_oracle);
            o["sharer_bucket"]        = unsigned(d.sharer_bucket);
            o["owner_dist"]           = unsigned(d.owner_dist);
            o["dirty_owner"]          = unsigned(d.dirty_owner);
            o["path_class"]           = unsigned(d.path_class);
            o["inval_fanout"]         = unsigned(d.inval_fanout);
            o["same_line_recent"]     = unsigned(d.same_line_recent);
            o["oracle_source"]        = unsigned(d.oracle_source);
            o["d_mshr_depth"]         = unsigned(d.d_mshr_depth);
            o["dtlb_hit"]             = unsigned(d.dtlb_hit);
            o["d_walker_levels"]      = unsigned(d.d_walker_levels);
            o["d_walker_dram_misses"] = unsigned(d.d_walker_dram_misses);
            o["d_bank_id"]            = unsigned(d.d_bank_id);
            o["d_llc_set_residency"]  = unsigned(d.d_llc_set_residency);
            o["d_llc_set_lru_pos"]    = unsigned(d.d_llc_set_lru_pos);
        } else {
            o["mesi_before"]          = 0u;
            o["coh_oracle"]           = 0u;
            o["sharer_bucket"]        = 0u;
            o["owner_dist"]           = 0u;
            o["dirty_owner"]          = 0u;
            o["path_class"]           = 0u;
            o["inval_fanout"]         = 0u;
            o["same_line_recent"]     = 0u;
            o["oracle_source"]        = 1u;
            o["d_mshr_depth"]         = 0u;
            o["dtlb_hit"]             = 0u;
            o["d_walker_levels"]      = 0u;
            o["d_walker_dram_misses"] = 0u;
            o["d_bank_id"]            = 0u;
            o["d_llc_set_residency"]  = 0u;
            o["d_llc_set_lru_pos"]    = 0u;
        }
        const WinOut &w = wins[k];
        o["mem_density_W64"]            = w.mem_density;
        o["branch_density_W64"]         = w.br_density;
        o["unique_cl_W64"]              = w.unique64;
        o["pc_freq_W64"]                = w.pc_freq;
        o["bank_conflict_W64"]          = w.bank_conf;
        o["unique_cl_W256"]             = w.unique256;
        o["unique_cl_W1024"]            = w.unique1024;
        o["dram_bank_id"]               = w.dram_bank_id;
        o["dram_bank_freq_W256"]        = w.dram_bank_freq;
        o["dram_row_freq_W256"]         = w.dram_row_freq;
        o["cl_reuse_dist_log"]          = w.reuse_log;
        o["time_since_last_branch_log"] = w.branch_log;
        result[k] = std::move(o);
    }
    return result;
}

// F.1：SoA/POD probe。当前不再维护 i-side 状态机；仅对 mem op 调
// backend_->probe 推进 d-side 共享状态。mock/label 路径不消费 i_attrs。
inline py::array_t<uint32_t> PyLocalRefSim::batch_probe_pod(
        py::array_t<uint64_t, py::array::c_style | py::array::forcecast> macro_pc,
        py::array_t<uint64_t, py::array::c_style | py::array::forcecast> paddr,
        py::array_t<uint64_t, py::array::c_style | py::array::forcecast> cl_paddr,
        py::array_t<uint8_t, py::array::c_style | py::array::forcecast> is_load,
        py::array_t<uint8_t, py::array::c_style | py::array::forcecast> is_store,
        py::array_t<uint8_t, py::array::c_style | py::array::forcecast> is_atomic,
        py::array_t<uint8_t, py::array::c_style | py::array::forcecast> is_branch,
        py::array_t<uint16_t, py::array::c_style | py::array::forcecast> size,
        py::array_t<int64_t, py::array::c_style | py::array::forcecast> micro_seq,
        py::array_t<int32_t, py::array::c_style | py::array::forcecast> thread_id) {
    auto pc = macro_pc.unchecked<1>();
    auto pa = paddr.unchecked<1>();
    auto cl = cl_paddr.unchecked<1>();
    auto ld = is_load.unchecked<1>();
    auto st = is_store.unchecked<1>();
    auto at = is_atomic.unchecked<1>();
    auto br = is_branch.unchecked<1>();
    auto sz = size.unchecked<1>();
    auto seq = micro_seq.unchecked<1>();
    auto tid = thread_id.unchecked<1>();
    const ssize_t n = pc.shape(0);
    if (pa.shape(0) != n || cl.shape(0) != n || ld.shape(0) != n ||
        st.shape(0) != n || at.shape(0) != n || br.shape(0) != n ||
        sz.shape(0) != n || seq.shape(0) != n || tid.shape(0) != n) {
        throw std::runtime_error("batch_probe_pod: input arrays length mismatch");
    }

    py::array_t<uint32_t> banks(n);
    auto bk_out = banks.mutable_unchecked<1>();
    {
        py::gil_scoped_release rel;
        for (ssize_t k = 0; k < n; ++k) {
            (void)pc(k);

            const bool is_mem = (ld(k) != 0) || (st(k) != 0) || (at(k) != 0);
            if (is_mem) {
                auto r = backend_->probe(pa(k), (st(k) != 0) || (at(k) != 0),
                                         sz(k), uint64_t(seq(k)),
                                         uint32_t(tid(k)));
                bk_out(k) = r.d.d_bank_id;
            } else {
                bk_out(k) = 0u;
            }
        }
    }
    return banks;
}

// E.2：phase1c 完成后调用，把已 commit 的行依序应用到 WindowState。
inline void PyLocalRefSim::batch_window_update(py::list fields,
                                                py::list committed_mask) {
    const std::size_t n = fields.size();
    struct In {
        uint64_t pc;
        uint64_t cl_paddr;
        uint64_t paddr;
        uint32_t bank;
        bool is_load, is_store, is_atomic, is_branch;
        bool committed;
    };
    std::vector<In> in;
    in.reserve(n);
    for (std::size_t k = 0; k < n; ++k) {
        py::tuple t = py::cast<py::tuple>(fields[k]);
        bool committed = committed_mask[k].cast<bool>();
        // tuple 与 batch_probe 同布局：
        // (macro_pc, paddr, cacheline_paddr, is_load, is_store, is_atomic,
        //  is_branch, size, micro_seq, thread_id, d_bank_id_post_probe)
        in.push_back(In{
            t[0].cast<uint64_t>(),
            t[2].cast<uint64_t>(),
            t[1].cast<uint64_t>(),
            t[10].cast<uint32_t>(),
            t[3].cast<bool>(),
            t[4].cast<bool>(),
            t[5].cast<bool>(),
            t[6].cast<bool>(),
            committed,
        });
    }
    {
        py::gil_scoped_release rel;
        for (const auto &x : in) {
            if (!x.committed) break;  // 与 phase1c 早退一致：commit 必须连续
            win_.update(x.is_load, x.is_store, x.is_atomic, x.is_branch,
                        x.cl_paddr, x.pc, x.bank, x.paddr);
        }
    }
}

template <typename T>
inline void append_raw(std::string &s, const T &v) {
    const char *p = reinterpret_cast<const char *>(&v);
    s.append(p, sizeof(T));
}

// E.6：phase1c 下沉。JSONL 路径仍返回 dict payload 交给 Python/orjson，
// BIN 路径返回固定 56B record: <uint32 core_id, uint32 thread_id,
// uint64 micro_seq, double fetch_clock, double ready_clock, double fetch_lat,
// double exec_lat, double mispred>。
inline py::dict PyLocalRefSim::commit_quantum(
        py::list fields11, py::list fetch_lats, py::list exec_lats,
        py::list mispreds, py::list is_microop, py::list is_last_microop,
        py::list label_fetch_ticks, double first_fetch_tick,
        double fetch_clock, double ready_clock, double fetch_clock_base,
        uint64_t committed_this_quantum, uint32_t delta_t,
        bool label_driven, bool emit_bin) {
    const std::size_t n = fields11.size();
    struct In {
        uint64_t macro_pc;
        uint64_t paddr;
        uint64_t cl_paddr;
        bool is_load, is_store, is_atomic, is_branch;
        uint16_t size;
        uint64_t seq;
        uint32_t thread_id;
        uint32_t bank;
        double fl, el, mp;
        bool is_microop, is_last_microop;
        double label_fetch_tick;
    };
    std::vector<In> in;
    in.reserve(n);
    for (std::size_t k = 0; k < n; ++k) {
        py::tuple t = py::cast<py::tuple>(fields11[k]);
        in.push_back(In{
            t[0].cast<uint64_t>(),
            t[1].cast<uint64_t>(),
            t[2].cast<uint64_t>(),
            t[3].cast<bool>(),
            t[4].cast<bool>(),
            t[5].cast<bool>(),
            t[6].cast<bool>(),
            t[7].cast<uint16_t>(),
            t[8].cast<uint64_t>(),
            t[9].cast<uint32_t>(),
            t[10].cast<uint32_t>(),
            fetch_lats[k].cast<double>(),
            exec_lats[k].cast<double>(),
            mispreds[k].cast<double>(),
            is_microop[k].cast<bool>(),
            is_last_microop[k].cast<bool>(),
            label_driven ? label_fetch_ticks[k].cast<double>() : 0.0,
        });
    }

    struct Ev {
        double t;
        uint32_t core_id;
        uint64_t seq;
        uint32_t thread_id;
        double fc, rc, fl, el, mp;
    };
    std::vector<Ev> events;
    events.reserve(n);
    std::size_t consumed = 0;
    uint64_t macro_inc = 0;
    double max_abs_fetch_diff = 0.0;
    const double deadline = fetch_clock_base + double(delta_t);

    {
        py::gil_scoped_release rel;
        for (std::size_t k = 0; k < n; ++k) {
            if (fetch_clock >= deadline && committed_this_quantum >= 1) {
                break;
            }
            const In &x = in[k];
            const double fl = x.fl > 0.0 ? x.fl : 0.0;
            const double el = x.el > 0.0 ? x.el : 0.0;
            fetch_clock += fl;
            const double ready_candidate = fetch_clock + el;
            ready_clock = ready_clock > ready_candidate ? ready_clock : ready_candidate;
            committed_this_quantum += 1;

            if (label_driven) {
                const double abs_fc = fetch_clock + first_fetch_tick;
                double diff = abs_fc - x.label_fetch_tick;
                if (diff < 0.0) diff = -diff;
                if (diff > max_abs_fetch_diff) max_abs_fetch_diff = diff;
            }
            if (!x.is_microop || x.is_last_microop) {
                macro_inc += 1;
            }
            events.push_back(Ev{
                fetch_clock + el, core_id_, x.seq, x.thread_id,
                fetch_clock, ready_clock, x.fl, x.el, x.mp
            });

            win_.update(x.is_load, x.is_store, x.is_atomic, x.is_branch,
                        x.cl_paddr, x.macro_pc, x.bank, x.paddr);
            consumed += 1;
        }
        const double dl = fetch_clock_base + double(delta_t);
        double slack = fetch_clock - dl;
        if (slack < 0.0) slack = 0.0;
        fetch_clock_base = dl + slack;
        committed_this_quantum = 0;
    }

    py::list py_events(events.size());
    for (std::size_t k = 0; k < events.size(); ++k) {
        const Ev &e = events[k];
        py::object payload;
        if (emit_bin) {
            std::string rec;
            rec.reserve(56);
            append_raw<uint32_t>(rec, e.core_id);
            append_raw<uint32_t>(rec, e.thread_id);
            append_raw<uint64_t>(rec, e.seq);
            append_raw<double>(rec, e.fc);
            append_raw<double>(rec, e.rc);
            append_raw<double>(rec, e.fl);
            append_raw<double>(rec, e.el);
            append_raw<double>(rec, e.mp);
            payload = py::bytes(rec);
        } else {
            py::dict d;
            d["core_id"] = e.core_id;
            d["thread_id"] = e.thread_id;
            d["micro_seq"] = e.seq;
            d["fetch_clock"] = e.fc;
            d["ready_clock"] = e.rc;
            d["fetch_lat"] = e.fl;
            d["exec_lat"] = e.el;
            d["mispred"] = e.mp;
            payload = std::move(d);
        }
        py_events[k] = py::make_tuple(e.t, e.core_id, e.seq, payload);
    }

    py::dict out;
    out["consumed"] = consumed;
    out["fetch_clock"] = fetch_clock;
    out["ready_clock"] = ready_clock;
    out["fetch_clock_base"] = fetch_clock_base;
    out["committed_this_quantum"] = committed_this_quantum;
    out["macro_inc"] = macro_inc;
    out["uop_inc"] = consumed;
    out["max_abs_fetch_diff"] = max_abs_fetch_diff;
    out["events"] = std::move(py_events);
    return out;
}

inline py::dict PyLocalRefSim::commit_quantum_pod(
        py::array_t<uint64_t, py::array::c_style | py::array::forcecast> macro_pc,
        py::array_t<uint64_t, py::array::c_style | py::array::forcecast> paddr,
        py::array_t<uint64_t, py::array::c_style | py::array::forcecast> cl_paddr,
        py::array_t<uint8_t, py::array::c_style | py::array::forcecast> is_load,
        py::array_t<uint8_t, py::array::c_style | py::array::forcecast> is_store,
        py::array_t<uint8_t, py::array::c_style | py::array::forcecast> is_atomic,
        py::array_t<uint8_t, py::array::c_style | py::array::forcecast> is_branch,
        py::array_t<int64_t, py::array::c_style | py::array::forcecast> micro_seq,
        py::array_t<int32_t, py::array::c_style | py::array::forcecast> thread_id,
        py::array_t<uint32_t, py::array::c_style | py::array::forcecast> d_bank_id,
        py::array_t<double, py::array::c_style | py::array::forcecast> fetch_lats,
        py::array_t<double, py::array::c_style | py::array::forcecast> exec_lats,
        py::array_t<double, py::array::c_style | py::array::forcecast> mispreds,
        py::array_t<uint8_t, py::array::c_style | py::array::forcecast> is_microop,
        py::array_t<uint8_t, py::array::c_style | py::array::forcecast> is_last_microop,
        py::array_t<double, py::array::c_style | py::array::forcecast> label_fetch_ticks,
        double first_fetch_tick,
        double fetch_clock, double ready_clock, double fetch_clock_base,
        uint64_t committed_this_quantum, uint32_t delta_t,
        bool label_driven, bool emit_bin) {
    auto pc = macro_pc.unchecked<1>();
    auto pa = paddr.unchecked<1>();
    auto cl = cl_paddr.unchecked<1>();
    auto ld = is_load.unchecked<1>();
    auto st = is_store.unchecked<1>();
    auto at = is_atomic.unchecked<1>();
    auto br = is_branch.unchecked<1>();
    auto seq = micro_seq.unchecked<1>();
    auto tid = thread_id.unchecked<1>();
    auto bank = d_bank_id.unchecked<1>();
    auto fls = fetch_lats.unchecked<1>();
    auto els = exec_lats.unchecked<1>();
    auto mps = mispreds.unchecked<1>();
    auto imo = is_microop.unchecked<1>();
    auto ilast = is_last_microop.unchecked<1>();
    auto labft = label_fetch_ticks.unchecked<1>();
    const ssize_t n = pc.shape(0);
    if (pa.shape(0) != n || cl.shape(0) != n || ld.shape(0) != n ||
        st.shape(0) != n || at.shape(0) != n || br.shape(0) != n ||
        seq.shape(0) != n || tid.shape(0) != n || bank.shape(0) != n ||
        fls.shape(0) != n || els.shape(0) != n || mps.shape(0) != n ||
        imo.shape(0) != n || ilast.shape(0) != n ||
        (label_driven && labft.shape(0) != n)) {
        throw std::runtime_error("commit_quantum_pod: input arrays length mismatch");
    }

    struct Ev {
        double t;
        uint32_t core_id;
        uint64_t seq;
        uint32_t thread_id;
        double fc, rc, fl, el, mp;
    };
    std::vector<Ev> events;
    events.reserve(static_cast<std::size_t>(n));
    std::size_t consumed = 0;
    uint64_t macro_inc = 0;
    double max_abs_fetch_diff = 0.0;
    const double deadline = fetch_clock_base + double(delta_t);

    {
        py::gil_scoped_release rel;
        for (ssize_t k = 0; k < n; ++k) {
            if (fetch_clock >= deadline && committed_this_quantum >= 1) {
                break;
            }
            const double raw_fl = fls(k);
            const double raw_el = els(k);
            const double fl = raw_fl > 0.0 ? raw_fl : 0.0;
            const double el = raw_el > 0.0 ? raw_el : 0.0;
            fetch_clock += fl;
            const double ready_candidate = fetch_clock + el;
            ready_clock = ready_clock > ready_candidate ? ready_clock : ready_candidate;
            committed_this_quantum += 1;

            if (label_driven) {
                const double abs_fc = fetch_clock + first_fetch_tick;
                double diff = abs_fc - labft(k);
                if (diff < 0.0) diff = -diff;
                if (diff > max_abs_fetch_diff) max_abs_fetch_diff = diff;
            }
            if (imo(k) == 0 || ilast(k) != 0) {
                macro_inc += 1;
            }
            events.push_back(Ev{
                fetch_clock + el, core_id_, uint64_t(seq(k)), uint32_t(tid(k)),
                fetch_clock, ready_clock, raw_fl, raw_el, mps(k)
            });

            win_.update(ld(k) != 0, st(k) != 0, at(k) != 0, br(k) != 0,
                        cl(k), pc(k), bank(k), pa(k));
            consumed += 1;
        }
        const double dl = fetch_clock_base + double(delta_t);
        double slack = fetch_clock - dl;
        if (slack < 0.0) slack = 0.0;
        fetch_clock_base = dl + slack;
        committed_this_quantum = 0;
    }

    py::list py_events(events.size());
    for (std::size_t k = 0; k < events.size(); ++k) {
        const Ev &e = events[k];
        py::object payload;
        if (emit_bin) {
            std::string rec;
            rec.reserve(56);
            append_raw<uint32_t>(rec, e.core_id);
            append_raw<uint32_t>(rec, e.thread_id);
            append_raw<uint64_t>(rec, e.seq);
            append_raw<double>(rec, e.fc);
            append_raw<double>(rec, e.rc);
            append_raw<double>(rec, e.fl);
            append_raw<double>(rec, e.el);
            append_raw<double>(rec, e.mp);
            payload = py::bytes(rec);
        } else {
            py::dict d;
            d["core_id"] = e.core_id;
            d["thread_id"] = e.thread_id;
            d["micro_seq"] = e.seq;
            d["fetch_clock"] = e.fc;
            d["ready_clock"] = e.rc;
            d["fetch_lat"] = e.fl;
            d["exec_lat"] = e.el;
            d["mispred"] = e.mp;
            payload = std::move(d);
        }
        py_events[k] = py::make_tuple(e.t, e.core_id, e.seq, payload);
    }

    py::dict out;
    out["consumed"] = consumed;
    out["fetch_clock"] = fetch_clock;
    out["ready_clock"] = ready_clock;
    out["fetch_clock_base"] = fetch_clock_base;
    out["committed_this_quantum"] = committed_this_quantum;
    out["macro_inc"] = macro_inc;
    out["uop_inc"] = consumed;
    out["max_abs_fetch_diff"] = max_abs_fetch_diff;
    out["events"] = std::move(py_events);
    return out;
}

}  // namespace

PYBIND11_MODULE(ref_sim_py, m) {
    py::class_<PyRefSim>(m, "RefSim")
        .def(py::init<const std::string &>())
        .def("on_ifetch", &PyRefSim::on_ifetch,
             py::arg("core_id"), py::arg("macro_pc_cl"))
        .def("on_mem_access", &PyRefSim::on_mem_access,
             py::arg("core_id"), py::arg("paddr"), py::arg("is_store"),
             py::arg("size"), py::arg("seq") = 0,
             py::arg("thread_id") = 0)
        .def("on_request", &PyRefSim::on_request,
             py::arg("core_id"), py::arg("paddr"), py::arg("is_store"),
             py::arg("size"), py::arg("seq") = 0,
             py::arg("thread_id") = 0)
        .def("on_commit", &PyRefSim::on_commit,
             py::arg("core_id"), py::arg("seq") = 0);

    py::class_<PyLocalRefSim, std::shared_ptr<PyLocalRefSim>>(m, "LocalRefSim")
        .def("on_ifetch_speculative", &PyLocalRefSim::on_ifetch_speculative,
             py::arg("macro_pc_cl"))
        .def("on_mem_access_speculative",
             &PyLocalRefSim::on_mem_access_speculative,
             py::arg("paddr"), py::arg("is_store"), py::arg("size"),
             py::arg("seq") = 0, py::arg("thread_id") = 0)
        .def("commit_speculative", &PyLocalRefSim::commit_speculative,
             py::arg("paddr"), py::arg("is_store"), py::arg("size"),
             py::arg("seq") = 0, py::arg("thread_id") = 0)
        .def("commit_ifetch_speculative",
             &PyLocalRefSim::commit_ifetch_speculative,
             py::arg("macro_pc_cl"))
        .def("batch_probe", &PyLocalRefSim::batch_probe, py::arg("fields"))
        .def("batch_probe_pod", &PyLocalRefSim::batch_probe_pod,
             py::arg("macro_pc"), py::arg("paddr"), py::arg("cl_paddr"),
             py::arg("is_load"), py::arg("is_store"), py::arg("is_atomic"),
             py::arg("is_branch"), py::arg("size"), py::arg("micro_seq"),
             py::arg("thread_id"))
        .def("batch_window_update", &PyLocalRefSim::batch_window_update,
             py::arg("fields"), py::arg("committed_mask"))
        .def("commit_quantum", &PyLocalRefSim::commit_quantum,
             py::arg("fields11"), py::arg("fetch_lats"),
             py::arg("exec_lats"), py::arg("mispreds"),
             py::arg("is_microop"), py::arg("is_last_microop"),
             py::arg("label_fetch_ticks"), py::arg("first_fetch_tick"),
             py::arg("fetch_clock"), py::arg("ready_clock"),
             py::arg("fetch_clock_base"), py::arg("committed_this_quantum"),
             py::arg("delta_t"), py::arg("label_driven"),
             py::arg("emit_bin"))
        .def("commit_quantum_pod", &PyLocalRefSim::commit_quantum_pod,
             py::arg("macro_pc"), py::arg("paddr"), py::arg("cl_paddr"),
             py::arg("is_load"), py::arg("is_store"), py::arg("is_atomic"),
             py::arg("is_branch"), py::arg("micro_seq"), py::arg("thread_id"),
             py::arg("d_bank_id"), py::arg("fetch_lats"),
             py::arg("exec_lats"), py::arg("mispreds"),
             py::arg("is_microop"), py::arg("is_last_microop"),
             py::arg("label_fetch_ticks"), py::arg("first_fetch_tick"),
             py::arg("fetch_clock"), py::arg("ready_clock"),
             py::arg("fetch_clock_base"), py::arg("committed_this_quantum"),
             py::arg("delta_t"), py::arg("label_driven"),
             py::arg("emit_bin"))
        .def_property_readonly("core_id", &PyLocalRefSim::core_id);

    py::class_<PyCoordinator>(m, "Coordinator")
        .def(py::init<const std::string &>())
        .def("local", &PyCoordinator::local, py::arg("core_id"))
        .def("reconcile", &PyCoordinator::reconcile,
             py::arg("deltas"), py::arg("results"))
        .def("drain_counters", &PyCoordinator::drain_counters);
}
