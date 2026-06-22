#include "core/Config.h"
#include <fstream>
#include <sstream>
#include <iostream>
#include <unordered_map>
#include <algorithm>

namespace minesim {

// Helper to trim whitespace
static std::string trim(const std::string& s) {
    auto start = s.find_first_not_of(" \t\r\n");
    auto end = s.find_last_not_of(" \t\r\n");
    if (start == std::string::npos) return "";
    return s.substr(start, end - start + 1);
}

MicroArchConfig MicroArchConfig::load_from_file(const std::string& filename) {
    MicroArchConfig cfg = get_host_preset(); // Fallback
    std::ifstream file(filename);
    if (!file.is_open()) {
        std::cerr << "Warning: Could not open config file " << filename << ". Using defaults.\n";
        return cfg;
    }

    std::unordered_map<std::string, std::string> kv;
    std::string line;
    std::string current_section = "";

    while (std::getline(file, line)) {
        line = trim(line);
        if (line.empty() || line[0] == '#' || line[0] == ';') continue;
        
        if (line[0] == '[' && line.back() == ']') {
            current_section = trim(line.substr(1, line.size() - 2)) + ".";
            continue;
        }

        auto eq_pos = line.find('=');
        if (eq_pos != std::string::npos) {
            std::string key = current_section + trim(line.substr(0, eq_pos));
            std::string val = trim(line.substr(eq_pos + 1));
            kv[key] = val;
        }
    }

    auto get_u32 = [&](const std::string& key, uint32_t default_val) -> uint32_t {
        if (kv.find(key) != kv.end()) {
            return std::stoul(kv[key]);
        }
        return default_val;
    };

    auto get_str = [&](const std::string& key, const std::string& default_val) -> std::string {
        if (kv.find(key) != kv.end()) {
            return kv[key];
        }
        return default_val;
    };

    cfg.bp_type = get_str("branch_predictor.type", cfg.bp_type);
    cfg.bp_size = get_u32("branch_predictor.size", cfg.bp_size);
    cfg.bp_history_bits = get_u32("branch_predictor.history_bits", cfg.bp_history_bits);

    cfg.fetch_width = get_u32("core.fetch_width", cfg.fetch_width);
    cfg.decode_width = get_u32("core.decode_width", cfg.decode_width);
    cfg.rename_width = get_u32("core.rename_width", cfg.rename_width);
    cfg.dispatch_width = get_u32("core.dispatch_width", cfg.dispatch_width);
    cfg.issue_width = get_u32("core.issue_width", cfg.issue_width);
    cfg.retire_width = get_u32("core.retire_width", cfg.retire_width);

    cfg.num_alu_ports = get_u32("core.num_alu_ports", cfg.num_alu_ports);
    cfg.num_load_ports = get_u32("core.num_load_ports", cfg.num_load_ports);
    cfg.num_store_ports = get_u32("core.num_store_ports", cfg.num_store_ports);
    cfg.num_branch_ports = get_u32("core.num_branch_ports", cfg.num_branch_ports);

    cfg.rob_size = get_u32("core.rob_size", cfg.rob_size);
    cfg.lq_size = get_u32("core.lq_size", cfg.lq_size);
    cfg.sq_size = get_u32("core.sq_size", cfg.sq_size);
    cfg.iq_size = get_u32("core.iq_size", cfg.iq_size);
    cfg.branch_mispredict_penalty = get_u32("core.branch_mispredict_penalty", cfg.branch_mispredict_penalty);

    cfg.l1i.size_kb = get_u32("cache.l1i_size_kb", cfg.l1i.size_kb);
    cfg.l1i.associativity = get_u32("cache.l1i_associativity", cfg.l1i.associativity);
    cfg.l1i.line_size = get_u32("cache.l1i_line_size", cfg.l1i.line_size);
    cfg.l1i.latency = get_u32("cache.l1i_latency", cfg.l1i.latency);

    cfg.l1d.size_kb = get_u32("cache.l1d_size_kb", cfg.l1d.size_kb);
    cfg.l1d.associativity = get_u32("cache.l1d_associativity", cfg.l1d.associativity);
    cfg.l1d.line_size = get_u32("cache.l1d_line_size", cfg.l1d.line_size);
    cfg.l1d.latency = get_u32("cache.l1d_latency", cfg.l1d.latency);

    cfg.l2.size_kb = get_u32("cache.l2_size_kb", cfg.l2.size_kb);
    cfg.l2.associativity = get_u32("cache.l2_associativity", cfg.l2.associativity);
    cfg.l2.line_size = get_u32("cache.l2_line_size", cfg.l2.line_size);
    cfg.l2.latency = get_u32("cache.l2_latency", cfg.l2.latency);

    cfg.l3.size_kb = get_u32("cache.l3_size_kb", cfg.l3.size_kb);
    cfg.l3.associativity = get_u32("cache.l3_associativity", cfg.l3.associativity);
    cfg.l3.line_size = get_u32("cache.l3_line_size", cfg.l3.line_size);
    cfg.l3.latency = get_u32("cache.l3_latency", cfg.l3.latency);

    cfg.itlb.entries = get_u32("tlb.itlb_entries", cfg.itlb.entries);
    cfg.itlb.associativity = get_u32("tlb.itlb_associativity", cfg.itlb.associativity);
    cfg.itlb.latency = get_u32("tlb.itlb_latency", cfg.itlb.latency);

    cfg.dtlb_4k.entries = get_u32("tlb.dtlb_4k_entries", cfg.dtlb_4k.entries);
    cfg.dtlb_4k.associativity = get_u32("tlb.dtlb_4k_associativity", cfg.dtlb_4k.associativity);
    cfg.dtlb_4k.latency = get_u32("tlb.dtlb_4k_latency", cfg.dtlb_4k.latency);

    cfg.dtlb_2m.entries = get_u32("tlb.dtlb_2m_entries", cfg.dtlb_2m.entries);
    cfg.dtlb_2m.associativity = get_u32("tlb.dtlb_2m_associativity", cfg.dtlb_2m.associativity);
    cfg.dtlb_2m.latency = get_u32("tlb.dtlb_2m_latency", cfg.dtlb_2m.latency);

    cfg.dtlb_1g.entries = get_u32("tlb.dtlb_1g_entries", cfg.dtlb_1g.entries);
    cfg.dtlb_1g.associativity = get_u32("tlb.dtlb_1g_associativity", cfg.dtlb_1g.associativity);
    cfg.dtlb_1g.latency = get_u32("tlb.dtlb_1g_latency", cfg.dtlb_1g.latency);

    cfg.stlb.entries = get_u32("tlb.stlb_entries", cfg.stlb.entries);
    cfg.stlb.associativity = get_u32("tlb.stlb_associativity", cfg.stlb.associativity);
    cfg.stlb.latency = get_u32("tlb.stlb_latency", cfg.stlb.latency);

    cfg.mem.size_mb = get_u32("memory.size_mb", cfg.mem.size_mb);
    cfg.mem.latency = get_u32("memory.latency", cfg.mem.latency);
    cfg.mem.default_page_size_kb = get_u32("memory.default_page_size_kb", cfg.mem.default_page_size_kb);
    cfg.mem.thp_page_size_kb = get_u32("memory.thp_page_size_kb", cfg.mem.thp_page_size_kb);
    if (kv.find("memory.thp_min_vaddr") != kv.end()) {
        cfg.mem.thp_min_vaddr = std::stoull(kv["memory.thp_min_vaddr"], nullptr, 0);
    }
    cfg.mem.page_walk_latency = get_u32("memory.page_walk_latency", cfg.mem.page_walk_latency);

    // Experimental switches
    auto get_bool = [&](const std::string& key, bool default_val) -> bool {
        auto it = kv.find(key);
        if (it == kv.end()) return default_val;
        std::string v = it->second;
        std::transform(v.begin(), v.end(), v.begin(), ::tolower);
        return (v == "1" || v == "true" || v == "yes" || v == "on");
    };
    cfg.experimental.enable_mshr = get_bool("experimental.enable_mshr", cfg.experimental.enable_mshr);
    cfg.experimental.enable_dram_bw = get_bool("experimental.enable_dram_bw", cfg.experimental.enable_dram_bw);
    cfg.experimental.enable_l2_bw = get_bool("experimental.enable_l2_bw", cfg.experimental.enable_l2_bw);
    cfg.experimental.enable_l3_bw = get_bool("experimental.enable_l3_bw", cfg.experimental.enable_l3_bw);
    cfg.experimental.enable_partial_stlf = get_bool("experimental.enable_partial_stlf", cfg.experimental.enable_partial_stlf);
    cfg.experimental.enable_sq_drain_block = get_bool("experimental.enable_sq_drain_block", cfg.experimental.enable_sq_drain_block);
    cfg.experimental.mshr_capacity = get_u32("experimental.mshr_capacity", cfg.experimental.mshr_capacity);
    cfg.experimental.num_dram_channels = get_u32("experimental.num_dram_channels", cfg.experimental.num_dram_channels);
    cfg.experimental.l2_burst_cycles = get_u32("experimental.l2_burst_cycles", cfg.experimental.l2_burst_cycles);
    cfg.experimental.l3_burst_cycles = get_u32("experimental.l3_burst_cycles", cfg.experimental.l3_burst_cycles);
    cfg.experimental.dram_burst_cycles = get_u32("experimental.dram_burst_cycles", cfg.experimental.dram_burst_cycles);
    cfg.experimental.partial_stlf_penalty = get_u32("experimental.partial_stlf_penalty", cfg.experimental.partial_stlf_penalty);
    cfg.experimental.enable_next_line_prefetcher = get_bool("experimental.enable_next_line_prefetcher", cfg.experimental.enable_next_line_prefetcher);
    cfg.experimental.next_line_prefetch_distance = get_u32("experimental.next_line_prefetch_distance", cfg.experimental.next_line_prefetch_distance);
    cfg.experimental.direct_target_miss_visibility_pct =
        get_u32("experimental.direct_target_miss_visibility_pct",
                cfg.experimental.direct_target_miss_visibility_pct);
    if (cfg.experimental.direct_target_miss_visibility_pct > 100) {
        cfg.experimental.direct_target_miss_visibility_pct = 100;
    }
    cfg.experimental.backend_stall_visibility_pct =
        get_u32("experimental.backend_stall_visibility_pct",
                cfg.experimental.backend_stall_visibility_pct);
    if (cfg.experimental.backend_stall_visibility_pct > 100) {
        cfg.experimental.backend_stall_visibility_pct = 100;
    }
    cfg.experimental.branch_flush_memory_overlap_pct =
        get_u32("experimental.branch_flush_memory_overlap_pct",
                cfg.experimental.branch_flush_memory_overlap_pct);
    if (cfg.experimental.branch_flush_memory_overlap_pct > 100) {
        cfg.experimental.branch_flush_memory_overlap_pct = 100;
    }
    cfg.experimental.enable_mcw_stats =
        get_bool("experimental.enable_mcw_stats",
                 cfg.experimental.enable_mcw_stats);
    cfg.experimental.enable_mcw_timing =
        get_bool("experimental.enable_mcw_timing",
                 cfg.experimental.enable_mcw_timing);
    cfg.experimental.mcw_window_size =
        get_u32("experimental.mcw_window_size",
                cfg.experimental.mcw_window_size);
    if (cfg.experimental.mcw_window_size == 0) {
        cfg.experimental.mcw_window_size = 1;
    }

    return cfg;
}

MicroArchConfig MicroArchConfig::get_host_preset() {
    MicroArchConfig cfg;
    
    // Sapphire Rapids server parameters (fallback defaults).
    cfg.bp_type = "bimodal";
    cfg.bp_size = 4096;
    cfg.bp_history_bits = 0;

    cfg.fetch_width = 6;
    cfg.decode_width = 6;
    cfg.rename_width = 6;
    cfg.dispatch_width = 6;
    cfg.issue_width = 8;
    cfg.retire_width = 8;

    // Default Execution Ports
    cfg.num_alu_ports = 4;
    cfg.num_load_ports = 2;
    cfg.num_store_ports = 2;
    cfg.num_branch_ports = 2;

    cfg.rob_size = 224;
    cfg.lq_size = 72;
    cfg.sq_size = 56;
    cfg.iq_size = 97;
    
    cfg.branch_mispredict_penalty = 18;
    
    cfg.l1i = {32, 8, 64, 4};
    cfg.l1d = {48, 12, 64, 4};   // SPR 8457C: 48 KB / 12-way
    cfg.l2  = {2048, 16, 64, 14}; // SPR: 2 MB / 16-way
    cfg.l3  = {32768, 16, 64, 70};

    cfg.itlb   = {128, 8, 1};   // SPR ITLB: 128-entry / 8-way
    cfg.dtlb_4k = {96, 4, 1};    // SPR 4K-DTLB: 96-entry / 4-way
    cfg.dtlb_2m = {32, 4, 1};    // SPR 2M-DTLB: 32-entry / 4-way
    cfg.dtlb_1g = {8, 8, 1};     // SPR 1G-DTLB: 8-entry / fully assoc -> approximate as 8-way
    cfg.stlb   = {2048, 16, 7}; // SPR STLB: 2048-entry / 16-way (4K/2M unified)

    // 16 GB main memory; default 4 KB pages; vaddrs >= 0x1_0000_0000 (4 GB) are
    // mapped using THP (2 MB) to approximate Linux Transparent Huge Pages.
    cfg.mem = {16384, 100, 4, 2048, 0x100000000ULL, 50};
    
    return cfg;
}

} // namespace minesim
