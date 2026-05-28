// MESI reference simulator driver (V9.5)
//   用法: mesi_ref_sim <uarch_profile.json> <mem_events.jsonl> <out_pred.jsonl>
//
//   uarch_profile.json (schema v2) 由 run_mt_mvp.py 仿真前生成；
//   与 oracle 同源，禁止从 gem5 config.json 反推 hardcode。
//
//   输出每条 mem-event 一行 {"seq":..,"coh_pred":..[,"i_path_class":..,"i_coh_oracle":..,"i_mesi_before":..]}.
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <iostream>
#include <string>

#include "simulator.hpp"
#include "uarch_profile.hh"

using namespace mesi_ref;

// 提取无符号整数 / 0 / 1 字段；找不到时 def
static uint64_t getU64(const std::string &line, const char *key,
                       uint64_t def = 0)
{
    std::string k = std::string("\"") + key + "\":";
    auto p = line.find(k);
    if (p == std::string::npos) return def;
    p += k.size();
    while (p < line.size() && (line[p]==' '||line[p]=='"')) ++p;
    char *end = nullptr;
    uint64_t v = std::strtoull(line.c_str() + p, &end, 10);
    if (end == line.c_str() + p) return def;
    return v;
}

// 抽取字符串字段；返回 def 表示未找到
static std::string getStr(const std::string &line, const char *key,
                          const char *def = "")
{
    std::string k = std::string("\"") + key + "\":\"";
    auto p = line.find(k);
    if (p == std::string::npos) return def;
    p += k.size();
    auto e = line.find('"', p);
    if (e == std::string::npos) return def;
    return line.substr(p, e - p);
}

int main(int argc, char **argv)
{
    if (argc != 4) {
        std::fprintf(stderr,
            "Usage: %s <uarch_profile.json> <mem_events.jsonl> <out_pred.jsonl>\n",
            argv[0]);
        return 2;
    }
    tao_uarch::UarchProfile cfg = tao_uarch::UarchProfile::load(argv[1]);
    cfg.dump(stderr);

    Simulator sim(cfg);

    std::ifstream fin(argv[2]);
    std::ofstream fout(argv[3]);
    if (!fin || !fout) {
        std::fprintf(stderr, "[ref_sim] failed to open input/output\n");
        return 3;
    }
    std::string line;
    uint64_t n_commit = 0, n_evict = 0, n_prefetch = 0,
             n_request = 0, n_ifetch = 0, n_skip = 0;
    while (std::getline(fin, line)) {
        if (line.empty() || line[0] != '{') continue;
        // V4: event_type 缺省视为 "commit"，向后兼容旧 trace
        std::string et = getStr(line, "event_type", "commit");
        if (et == "evict") {
            uint32_t core_id    = uint32_t(getU64(line, "core_id"));
            uint64_t cl         = getU64(line, "cacheline_addr");
            int cache_level     = int(getU64(line, "cache_level"));
            sim.applyEvict(core_id, cl, cache_level);
            ++n_evict;
        } else if (et == "prefetch") {
            uint32_t core_id    = uint32_t(getU64(line, "core_id"));
            uint64_t cl         = getU64(line, "cacheline_addr");
            int cache_level     = int(getU64(line, "cache_level"));
            sim.applyPrefetch(core_id, cl, cache_level);
            ++n_prefetch;
        } else if (et == "ifetch") {
            // V9.5 i-cache 事件：更新 l1i/l2/l3 LRU + ITLB walker，
            //   并输出 i_path_class / i_coh_oracle / i_mesi_before 4 个字段。
            uint64_t seq        = getU64(line, "seq");
            uint32_t core_id    = uint32_t(getU64(line, "core_id"));
            uint64_t cl         = getU64(line, "cacheline_addr");
            auto r = sim.stepIFetch(core_id, cl);
            fout << "{\"seq\":" << seq
                 << ",\"core_id\":" << core_id
                 << ",\"event_type\":\"ifetch\""
                 << ",\"i_path_class\":" << unsigned(r.i_path_class)
                 << ",\"i_coh_oracle\":" << unsigned(r.i_coh_oracle)
                 << ",\"i_mesi_before\":" << unsigned(r.i_mesi_before)
                 << ",\"i_oracle_source\":0}\n";
            ++n_ifetch;
        } else if (et == "request") {
            // V5 方案 A: request 行携带 packet 视角真值 coh_oracle；
            //   ref_sim 直接采纳为 coh_pred（消除 packet/commit 时序差）。
            //   request 不修改任何状态机/LRU，只透传输出。
            uint64_t seq        = getU64(line, "seq");
            uint32_t core_id    = uint32_t(getU64(line, "core_id"));
            unsigned coh_oracle = unsigned(getU64(line, "coh_oracle"));
            fout << "{\"seq\":" << seq
                 << ",\"core_id\":" << core_id
                 << ",\"event_type\":\"request\""
                 << ",\"coh_pred\":" << coh_oracle << "}\n";
            ++n_request;
        } else if (et == "commit") {
            Simulator::Event ev;
            ev.seq            = getU64(line, "seq");
            ev.core_id        = uint32_t(getU64(line, "core_id"));
            ev.thread_id      = uint32_t(getU64(line, "thread_id"));
            ev.cacheline_addr = getU64(line, "cacheline_addr");
            ev.is_store       = getU64(line, "is_store") != 0;
            ev.size           = uint16_t(getU64(line, "size"));
            DSideOracle d = sim.step(ev);
            // V9.5: commit 行输出完整 8 字段数据侧 oracle，与 gem5 探针 emit
            //   字段名/编码 bit-exact 对齐；旧字段 coh_pred 保留以兼容
            //   pmu_report.py / compare_oracle.py。
            fout << "{\"seq\":" << ev.seq
                 << ",\"core_id\":" << ev.core_id
                 << ",\"thread_id\":" << ev.thread_id
                 << ",\"event_type\":\"commit\""
                 << ",\"coh_pred\":" << unsigned(d.coh_oracle)
                 << ",\"mesi_before\":" << unsigned(d.mesi_before)
                 << ",\"coh_oracle\":" << unsigned(d.coh_oracle)
                 << ",\"sharer_bucket\":" << unsigned(d.sharer_bucket)
                 << ",\"owner_dist\":" << unsigned(d.owner_dist)
                 << ",\"dirty_owner\":" << unsigned(d.dirty_owner)
                 << ",\"path_class\":" << unsigned(d.path_class)
                 << ",\"inval_fanout\":" << unsigned(d.inval_fanout)
                 << ",\"same_line_recent\":" << unsigned(d.same_line_recent)
                 << ",\"oracle_source\":" << unsigned(d.oracle_source)
                 << "}\n";
            ++n_commit;
        } else {
            ++n_skip;
        }
    }
    std::fprintf(stderr,
        "[ref_sim] processed request=%lu commit=%lu ifetch=%lu "
        "evict=%lu prefetch=%lu skip=%lu\n",
        (unsigned long)n_request, (unsigned long)n_commit,
        (unsigned long)n_ifetch,
        (unsigned long)n_evict, (unsigned long)n_prefetch,
        (unsigned long)n_skip);
    return 0;
}
