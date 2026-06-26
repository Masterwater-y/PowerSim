// LLMSim shared-system driver.
//
// Input: JSONL memory events sorted by the predicted global memory order.
// The simulator keeps one MESI/cache/TLB/MSHR state for the whole stream, so
// state naturally crosses LLMSim windows. PMU snapshots can be emitted at any
// time by inserting {"event_type":"snapshot"} or by using --snapshot-interval.

#include <algorithm>
#include <array>
#include <cstdint>
#include <cstdlib>
#include <fstream>
#include <iostream>
#include <memory>
#include <string>
#include <unordered_map>
#include <utility>
#include <vector>

#include "quantum.hpp"
#include "uarch_profile.hh"

using mesi_ref::quantum::Coordinator;
using mesi_ref::quantum::CounterSnapshot;
using mesi_ref::quantum::LocalRefSim;

// Speculative-warming knob: which cl addresses to feed into LocalRefSim::
// warmL1dOnly. See docs/share_system_upgrade_plan.md §P1 for the design
// rationale; on functional-only input we can only emulate wrong-path loads
// indirectly via the committed mem stream plus mispredicted-branch markers.
enum class WarmModel : uint8_t {
    None = 0,         // No speculative warming (baseline).
    NextLine = 1,     // After every committed load, touch L1d at cl+64.
    MispredShadow = 2 // On mispred markers, replay recent loads' cl+64.
};

// Offset policy for mispred-shadow: how to derive the shadow cacheline from a
// remembered load. ``Fixed64`` keeps the original next-line offset (+64). The
// other modes consult ``StrideDetector`` (see below) trained online from the
// per-core demand load stream.
enum class ShadowStrideMode : uint8_t {
    Fixed64 = 0,     // shadow_cl = remembered_cl + 64 (back-compat default).
    Stride = 1,      // shadow_cl = remembered_cl + learned_stride (no fallback).
    Auto = 2,        // Use learned stride if confidence high; else fall back +64.
};

// Per-core ring buffer of recent load cachelines (mispred-shadow only).
struct LoadHistory {
    static constexpr std::size_t kCap = 16;
    std::array<uint64_t, kCap> buf{};
    std::size_t head = 0;
    std::size_t size = 0;
    void push(uint64_t cl)
    {
        buf[head] = cl;
        head = (head + 1) % kCap;
        if (size < kCap) ++size;
    }
};

// Lightweight per-core stride detector trained online on the committed demand
// load stream. Uses a single 2-bit saturating confidence counter and one
// candidate stride (PC-less, single-table). When confidence >= kConfThresh the
// learned stride is considered reliable. ``stride`` is in bytes between
// cachelines (a positive or negative int64). Default offset on cold/low-conf
// is left to the caller (``ShadowStrideMode``).
struct StrideDetector {
    static constexpr uint8_t kConfMax = 3;     // 2-bit saturating
    static constexpr uint8_t kConfThresh = 2;  // need >=2 consecutive matches
    uint64_t last_cl = 0;
    int64_t stride = 0;
    uint8_t conf = 0;
    bool primed = false;

    void observe(uint64_t cl)
    {
        if (!primed) {
            last_cl = cl;
            primed = true;
            return;
        }
        const int64_t s = int64_t(cl) - int64_t(last_cl);
        last_cl = cl;
        if (s == 0) return;  // zero-stride doesn't train (avoid same-line loop)
        if (s == stride) {
            if (conf < kConfMax) ++conf;
        } else {
            if (conf > 0) {
                --conf;
            } else {
                stride = s;
            }
        }
    }

    // Returns (stride_bytes, has_confidence).
    std::pair<int64_t, bool> learned() const
    {
        return {stride, conf >= kConfThresh && stride != 0};
    }
};

namespace {

bool hasKey(const std::string &line, const char *key)
{
    const std::string k = std::string("\"") + key + "\":";
    return line.find(k) != std::string::npos;
}

uint64_t getU64(const std::string &line, const char *key, uint64_t def = 0)
{
    const std::string k = std::string("\"") + key + "\":";
    auto p = line.find(k);
    if (p == std::string::npos) return def;
    p += k.size();
    while (p < line.size() && (line[p] == ' ' || line[p] == '"')) ++p;
    char *end = nullptr;
    uint64_t v = std::strtoull(line.c_str() + p, &end, 10);
    if (end == line.c_str() + p) return def;
    return v;
}

std::string getStr(const std::string &line, const char *key,
                   const char *def = "")
{
    const std::string k = std::string("\"") + key + "\":\"";
    auto p = line.find(k);
    if (p == std::string::npos) return def;
    p += k.size();
    auto e = line.find('"', p);
    if (e == std::string::npos) return def;
    return line.substr(p, e - p);
}

void addMap(std::unordered_map<uint32_t, uint64_t> &dst,
            const std::unordered_map<uint32_t, uint64_t> &src)
{
    for (const auto &kv : src) dst[kv.first] += kv.second;
}

void addCounters(CounterSnapshot &dst, const CounterSnapshot &src)
{
    dst.llc_hits += src.llc_hits;
    dst.llc_misses += src.llc_misses;
    dst.cha_remote_clean += src.cha_remote_clean;
    dst.cha_remote_dirty += src.cha_remote_dirty;
    dst.wb_required += src.wb_required;
    dst.inval_fanout_sum += src.inval_fanout_sum;

    addMap(dst.l1d_hits, src.l1d_hits);
    addMap(dst.l1d_misses, src.l1d_misses);
    addMap(dst.l2_hits, src.l2_hits);
    addMap(dst.l2_misses, src.l2_misses);
    addMap(dst.l1i_hits, src.l1i_hits);
    addMap(dst.l1i_misses, src.l1i_misses);
    addMap(dst.dtlb_misses, src.dtlb_misses);
    addMap(dst.itlb_misses, src.itlb_misses);
    addMap(dst.walker_dram_misses, src.walker_dram_misses);

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

void printMap(std::ostream &os,
              const std::unordered_map<uint32_t, uint64_t> &m)
{
    os << "{";
    bool first = true;
    for (const auto &kv : m) {
        if (!first) os << ",";
        first = false;
        os << "\"" << kv.first << "\":" << kv.second;
    }
    os << "}";
}

double safeRate(uint64_t num, uint64_t den)
{
    return den ? double(num) / double(den) : 0.0;
}

void emitSnapshot(std::ostream &os, const CounterSnapshot &s,
                  uint64_t events, const char *reason)
{
    const uint64_t loads = s.pmu_l1d_loads;
    const uint64_t stores = s.pmu_l1d_stores;
    const uint64_t mem_ops = loads + stores;
    const uint64_t llc_misses =
        s.pmu_llc_load_misses + s.pmu_llc_store_misses;
    const uint64_t l1d_misses =
        s.pmu_l1d_load_misses + s.pmu_l1d_store_misses;

    os << "{\"event_type\":\"pmu_snapshot\""
       << ",\"reason\":\"" << reason << "\""
       << ",\"events\":" << events
       << ",\"cache\":{\"llc_hits\":" << s.llc_hits
       << ",\"llc_misses\":" << s.llc_misses
       << ",\"l1d_hits\":";
    printMap(os, s.l1d_hits);
    os << ",\"l1d_misses\":";
    printMap(os, s.l1d_misses);
    os << ",\"l2_hits\":";
    printMap(os, s.l2_hits);
    os << ",\"l2_misses\":";
    printMap(os, s.l2_misses);
    os << "},\"uncore\":{\"cha_remote_clean\":" << s.cha_remote_clean
       << ",\"cha_remote_dirty\":" << s.cha_remote_dirty
       << ",\"wb_required\":" << s.wb_required
       << ",\"inval_fanout_sum\":" << s.inval_fanout_sum
       << "},\"tlb\":{\"dtlb_misses\":";
    printMap(os, s.dtlb_misses);
    os << ",\"itlb_misses\":";
    printMap(os, s.itlb_misses);
    os << ",\"walker_dram_misses\":";
    printMap(os, s.walker_dram_misses);
    os << "},\"pmu\":{\"l1d.loads\":" << loads
       << ",\"l1d.stores\":" << stores
       << ",\"l1d.load_misses\":" << s.pmu_l1d_load_misses
       << ",\"l1d.store_misses\":" << s.pmu_l1d_store_misses
       << ",\"l2.misses\":" << s.pmu_l2_misses
       << ",\"llc.load_misses\":" << s.pmu_llc_load_misses
       << ",\"llc.store_misses\":" << s.pmu_llc_store_misses
       << ",\"cha.requests.reads\":" << s.pmu_cha_requests_reads
       << ",\"cha.requests.writes\":" << s.pmu_cha_requests_writes
       << ",\"cha.tor_inserts.ia_miss_drd\":"
       << s.pmu_cha_tor_inserts_ia_miss_drd
       << ",\"cha.dir_lookup.snp\":" << s.pmu_cha_dir_lookup_snp
       << ",\"cha.core_snp.any_one\":" << s.pmu_cha_core_snp_any_one
       << "},\"rates\":{\"mr_llc\":" << safeRate(llc_misses, mem_ops)
       << ",\"mr_l1d\":" << safeRate(l1d_misses, mem_ops)
       << ",\"mr_l1d_ld\":"
       << safeRate(s.pmu_l1d_load_misses, loads)
       << ",\"mr_l1d_st\":"
       << safeRate(s.pmu_l1d_store_misses, stores)
       << ",\"remote_hit_per_mem\":"
       << safeRate(s.cha_remote_clean + s.cha_remote_dirty, mem_ops)
       << ",\"wb_required_per_mem\":"
       << safeRate(s.wb_required, mem_ops)
       << "}}\n";
}

uint64_t eventAddress(const std::string &line)
{
    if (hasKey(line, "paddr")) return getU64(line, "paddr");
    if (hasKey(line, "cacheline_paddr")) return getU64(line, "cacheline_paddr");
    return getU64(line, "cacheline_addr");
}

}  // namespace

int main(int argc, char **argv)
{
    if (argc < 4) {
        std::cerr
            << "Usage: " << argv[0]
            << " <uarch_profile.json> <mem_events.jsonl> <snapshots.jsonl> "
               "[--snapshot-interval=N]\n";
        return 2;
    }

    uint64_t snapshot_interval = 0;
    std::string per_op_path;
    WarmModel warm_model = WarmModel::None;
    std::size_t mispred_depth_K = LoadHistory::kCap;  // default = full ring
    ShadowStrideMode shadow_stride_mode = ShadowStrideMode::Fixed64;
    for (int i = 4; i < argc; ++i) {
        std::string a(argv[i]);
        const std::string p = "--snapshot-interval=";
        const std::string q = "--emit-per-op=";
        const std::string w = "--warm-model=";
        const std::string k = "--mispred-depth-K=";
        const std::string s = "--shadow-stride-mode=";
        if (a.rfind(p, 0) == 0) {
            snapshot_interval = std::strtoull(a.c_str() + p.size(), nullptr, 10);
        } else if (a.rfind(q, 0) == 0) {
            per_op_path = a.substr(q.size());
        } else if (a.rfind(w, 0) == 0) {
            const std::string v = a.substr(w.size());
            if (v == "none") warm_model = WarmModel::None;
            else if (v == "nextline") warm_model = WarmModel::NextLine;
            else if (v == "mispred-shadow") warm_model = WarmModel::MispredShadow;
            else {
                std::cerr << "[shared_system] unknown --warm-model: " << v
                          << " (expected none|nextline|mispred-shadow)\n";
                return 2;
            }
        } else if (a.rfind(k, 0) == 0) {
            const uint64_t v = std::strtoull(a.c_str() + k.size(), nullptr, 10);
            mispred_depth_K = std::size_t(v > LoadHistory::kCap
                                          ? LoadHistory::kCap : v);
        } else if (a.rfind(s, 0) == 0) {
            const std::string v = a.substr(s.size());
            if (v == "fixed64") shadow_stride_mode = ShadowStrideMode::Fixed64;
            else if (v == "stride") shadow_stride_mode = ShadowStrideMode::Stride;
            else if (v == "auto") shadow_stride_mode = ShadowStrideMode::Auto;
            else {
                std::cerr << "[shared_system] unknown --shadow-stride-mode: "
                          << v << " (expected fixed64|stride|auto)\n";
                return 2;
            }
        }
    }

    // P0 diagnostic: optional per-op oracle decision sink. Default off so the
    // normal PMU path is byte-for-byte unchanged. When enabled, each committed
    // d-side mem op echoes (core_id, micro_seq, path_class, coh) so an external
    // script can join against the gem5 tao_trace ground-truth path_class.
    std::ofstream per_op_file;
    std::ostream *per_op = nullptr;
    if (!per_op_path.empty()) {
        per_op_file.open(per_op_path);
        if (!per_op_file) {
            std::cerr << "[shared_system] failed to open per-op sink: "
                      << per_op_path << "\n";
            return 3;
        }
        per_op = &per_op_file;
    }

    tao_uarch::UarchProfile cfg = tao_uarch::UarchProfile::load(argv[1]);
    Coordinator coord(cfg);
    std::unordered_map<uint32_t, std::unique_ptr<LocalRefSim>> locals;

    auto local = [&](uint32_t cid) -> LocalRefSim& {
        auto it = locals.find(cid);
        if (it != locals.end()) return *it->second;
        auto p = std::make_unique<LocalRefSim>(&coord, cid);
        LocalRefSim *raw = p.get();
        locals.emplace(cid, std::move(p));
        return *raw;
    };

    std::ifstream fin_file;
    std::ofstream fout_file;
    std::istream *fin = &std::cin;
    std::ostream *fout = &std::cout;
    if (std::string(argv[2]) != "-") {
        fin_file.open(argv[2]);
        fin = &fin_file;
    }
    if (std::string(argv[3]) != "-") {
        fout_file.open(argv[3]);
        fout = &fout_file;
    }
    if (!(*fin) || !(*fout)) {
        std::cerr << "[shared_system] failed to open input/output\n";
        return 3;
    }

    CounterSnapshot cumulative;
    uint64_t events = 0;
    uint64_t events_at_last_emit = 0;
    std::unordered_map<uint32_t, LoadHistory> load_hist;
    std::unordered_map<uint32_t, StrideDetector> stride_det;
    // Resolve the offset to use for a given remembered cacheline under the
    // configured ``shadow_stride_mode``. Returns ``(offset_bytes, ok)``; when
    // ``ok=false`` the caller should skip the shadow warm-up.
    auto shadowOffset = [&](uint32_t cid) -> std::pair<int64_t, bool> {
        switch (shadow_stride_mode) {
        case ShadowStrideMode::Fixed64:
            return {int64_t(64), true};
        case ShadowStrideMode::Stride: {
            auto it = stride_det.find(cid);
            if (it == stride_det.end()) return {0, false};
            auto [s, ok] = it->second.learned();
            return {s, ok};
        }
        case ShadowStrideMode::Auto: {
            auto it = stride_det.find(cid);
            if (it == stride_det.end()) return {int64_t(64), true};
            auto [s, ok] = it->second.learned();
            return ok ? std::pair<int64_t, bool>{s, true}
                      : std::pair<int64_t, bool>{int64_t(64), true};
        }
        }
        return {int64_t(64), true};
    };
    std::string line;
    while (std::getline(*fin, line)) {
        if (line.empty() || line[0] != '{') continue;
        const std::string et = getStr(line, "event_type", "mem");
        if (et == "roi_begin") {
            // Pre-ROI warmup: discard counter deltas accumulated during the
            // warmup prefix so that downstream PMU snapshots reflect only ROI
            // activity. Cache/TLB/MSHR/coherence state in Coordinator::shared_
            // and per-core LocalRefSim::local_ is intentionally preserved.
            CounterSnapshot delta;
            coord.drainCounters(delta);
            cumulative = CounterSnapshot{};
            emitSnapshot(*fout, cumulative, events, "roi_begin");
            fout->flush();
            events_at_last_emit = events;
            continue;
        }
        if (et == "snapshot" || et == "window_end") {
            CounterSnapshot delta;
            coord.drainCounters(delta);
            addCounters(cumulative, delta);
            emitSnapshot(*fout, cumulative, events, et.c_str());
            fout->flush();
            events_at_last_emit = events;
            continue;
        }
        if (et == "mispred") {
            if (warm_model == WarmModel::MispredShadow && mispred_depth_K > 0) {
                // Replay the most recent ``mispred_depth_K`` load cachelines'
                // shadow line as L1d-only warm-ups. The shadow offset comes
                // from ``shadowOffset`` which honors --shadow-stride-mode
                // (fixed64 | stride | auto). The ring buffer push order is
                // FIFO; the most recent entry sits at (head - 1) mod kCap.
                const uint32_t core_id = uint32_t(getU64(line, "core_id"));
                auto it = load_hist.find(core_id);
                if (it != load_hist.end()) {
                    const LoadHistory &h = it->second;
                    auto [off, ok] = shadowOffset(core_id);
                    if (ok) {
                        const std::size_t take = std::min(mispred_depth_K, h.size);
                        for (std::size_t k = 0; k < take; ++k) {
                            const std::size_t idx =
                                (h.head + LoadHistory::kCap - 1 - k) %
                                LoadHistory::kCap;
                            local(core_id).warmL1dOnly(
                                uint64_t(int64_t(h.buf[idx]) + off));
                        }
                    }
                }
            }
            // mispred markers carry no addr/size; never advance ``events``.
            continue;
        }
        if (et == "ifetch") {
            const uint32_t core_id = uint32_t(getU64(line, "core_id"));
            const uint64_t cl = eventAddress(line);
            local(core_id).probeIFetch(cl);
            ++events;
        } else {
            const bool is_load = getU64(line, "is_load") != 0;
            const bool is_store = getU64(line, "is_store") != 0;
            const bool is_atomic = getU64(line, "is_atomic") != 0;
            if (!is_load && !is_store && !is_atomic && et != "request"
                && et != "commit" && et != "mem") {
                continue;
            }
            const uint32_t core_id = uint32_t(getU64(line, "core_id"));
            const uint64_t paddr = eventAddress(line);
            const uint16_t size = uint16_t(getU64(line, "size", 8));
            const uint64_t seq = getU64(line, "seq", events);
            const uint32_t thread_id = uint32_t(getU64(line, "thread_id", core_id));
            auto res = local(core_id).probe(paddr, is_store || is_atomic, size, seq, thread_id);
            if (per_op) {
                const uint64_t micro_seq = getU64(line, "micro_seq", seq);
                (*per_op) << "{\"core_id\":" << core_id
                          << ",\"micro_seq\":" << micro_seq
                          << ",\"seq\":" << seq
                          << ",\"is_load\":" << (is_load ? 1 : 0)
                          << ",\"is_store\":" << ((is_store || is_atomic) ? 1 : 0)
                          << ",\"path_class\":" << unsigned(res.d.path_class)
                          << ",\"coh\":" << unsigned(res.d.coh_oracle)
                          << ",\"cl\":" << res.cl
                          << "}\n";
            }
            if (warm_model == WarmModel::NextLine && is_load) {
                auto [off, ok] = shadowOffset(core_id);
                if (ok) {
                    local(core_id).warmL1dOnly(
                        uint64_t(int64_t(res.cl) + off));
                }
            }
            if (warm_model == WarmModel::MispredShadow && is_load) {
                load_hist[core_id].push(res.cl);
            }
            if (is_load && shadow_stride_mode != ShadowStrideMode::Fixed64) {
                // Train the per-core stride detector on the committed demand
                // load stream. ``observe`` runs before the next mispred event
                // consumes ``shadowOffset``.
                stride_det[core_id].observe(res.cl);
            }
            ++events;
        }

        if (snapshot_interval && events - events_at_last_emit >= snapshot_interval) {
            CounterSnapshot delta;
            coord.drainCounters(delta);
            addCounters(cumulative, delta);
            emitSnapshot(*fout, cumulative, events, "interval");
            fout->flush();
            events_at_last_emit = events;
        }
    }

    CounterSnapshot delta;
    coord.drainCounters(delta);
    addCounters(cumulative, delta);
    if (events != events_at_last_emit) {
        emitSnapshot(*fout, cumulative, events, "final");
        fout->flush();
    }
    return 0;
}
