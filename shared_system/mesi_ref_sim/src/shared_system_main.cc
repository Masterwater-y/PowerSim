// LLMSim shared-system driver.
//
// Input: JSONL memory events sorted by the predicted global memory order.
// The simulator keeps one MESI/cache/TLB/MSHR state for the whole stream, so
// state naturally crosses LLMSim windows. PMU snapshots can be emitted at any
// time by inserting {"event_type":"snapshot"} or by using --snapshot-interval.

#include <cstdint>
#include <cstdlib>
#include <fstream>
#include <iostream>
#include <memory>
#include <string>
#include <unordered_map>

#include "quantum.hpp"
#include "uarch_profile.hh"

using mesi_ref::quantum::Coordinator;
using mesi_ref::quantum::CounterSnapshot;
using mesi_ref::quantum::LocalRefSim;

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
    for (int i = 4; i < argc; ++i) {
        std::string a(argv[i]);
        const std::string p = "--snapshot-interval=";
        if (a.rfind(p, 0) == 0) {
            snapshot_interval = std::strtoull(a.c_str() + p.size(), nullptr, 10);
        }
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
    std::string line;
    while (std::getline(*fin, line)) {
        if (line.empty() || line[0] != '{') continue;
        const std::string et = getStr(line, "event_type", "mem");
        if (et == "snapshot" || et == "window_end") {
            CounterSnapshot delta;
            coord.drainCounters(delta);
            addCounters(cumulative, delta);
            emitSnapshot(*fout, cumulative, events, et.c_str());
            fout->flush();
            events_at_last_emit = events;
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
            local(core_id).probe(paddr, is_store || is_atomic, size, seq, thread_id);
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
