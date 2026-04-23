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

    cfg.dtlb.entries = get_u32("tlb.dtlb_entries", cfg.dtlb.entries);
    cfg.dtlb.associativity = get_u32("tlb.dtlb_associativity", cfg.dtlb.associativity);
    cfg.dtlb.latency = get_u32("tlb.dtlb_latency", cfg.dtlb.latency);

    cfg.stlb.entries = get_u32("tlb.stlb_entries", cfg.stlb.entries);
    cfg.stlb.associativity = get_u32("tlb.stlb_associativity", cfg.stlb.associativity);
    cfg.stlb.latency = get_u32("tlb.stlb_latency", cfg.stlb.latency);

    cfg.mem.size_mb = get_u32("memory.size_mb", cfg.mem.size_mb);
    cfg.mem.latency = get_u32("memory.latency", cfg.mem.latency);
    cfg.mem.page_size_kb = get_u32("memory.page_size_kb", cfg.mem.page_size_kb);
    cfg.mem.page_walk_latency = get_u32("memory.page_walk_latency", cfg.mem.page_walk_latency);

    return cfg;
}

MicroArchConfig MicroArchConfig::get_host_preset() {
    MicroArchConfig cfg;
    
    // Skylake/Cascade Lake SP Server parameters (fallback defaults)
    cfg.bp_type = "bimodal";
    cfg.bp_size = 4096;

    cfg.fetch_width = 6;
    cfg.decode_width = 6;
    cfg.rename_width = 6;
    cfg.dispatch_width = 6;
    cfg.issue_width = 8;
    cfg.retire_width = 4;

    // Default Execution Ports
    cfg.num_alu_ports = 4;
    cfg.num_load_ports = 2;
    cfg.num_store_ports = 2;
    cfg.num_branch_ports = 2;

    cfg.rob_size = 224;
    cfg.lq_size = 72;
    cfg.sq_size = 56;
    cfg.iq_size = 97;
    
    cfg.branch_mispredict_penalty = 16;
    
    cfg.l1i = {32, 8, 64, 4};
    cfg.l1d = {32, 8, 64, 4};
    cfg.l2  = {1024, 16, 64, 14};
    cfg.l3  = {32768, 16, 64, 70};
    
    cfg.itlb = {128, 4, 1};
    cfg.dtlb = {64, 4, 1};
    cfg.stlb = {1536, 12, 7};

    cfg.mem = {16384, 100, 4, 50}; // 16GB, 100 cycles mem latency, 4KB page, 50 cycles page walk
    
    return cfg;
}

} // namespace minesim