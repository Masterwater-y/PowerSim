#include <iostream>
#include <chrono>
#include <sstream>
#include <vector>
#include "trace/TraceReader.h"
#include "core/Config.h"
#include "core/InstDecoder.h"
#include "core/MemoryHierarchy.h"
#include "core/IntervalCore.h"

using namespace minesim;

namespace {
constexpr const char* kDefaultConfigPath = "config/sapphire_rapids.cfg";

std::vector<uint8_t> parse_hex_bytes(const std::string& hex) {
    std::string compact;
    compact.reserve(hex.size());
    for (char c : hex) {
        if (c != ' ' && c != ':') compact.push_back(c);
    }
    if (compact.size() % 2 != 0) {
        throw std::runtime_error("hex marker length must be even");
    }
    std::vector<uint8_t> out;
    out.reserve(compact.size() / 2);
    for (size_t i = 0; i < compact.size(); i += 2) {
        unsigned int v = 0;
        std::stringstream ss;
        ss << std::hex << compact.substr(i, 2);
        ss >> v;
        out.push_back(static_cast<uint8_t>(v));
    }
    return out;
}

}

int main(int argc, char** argv) {
    if (argc < 2) {
        std::cerr << "Usage: " << argv[0] << " <trace_file.gz> [num_instructions]"
                  << " [--roi-begin-encoding HEX --roi-end-encoding HEX]\n";
        return 1;
    }

    std::string trace_file = argv[1];
    int num_insts = -1;
    std::vector<std::string> extra_args;
    int argi = 2;
    if (argi < argc) {
        std::string maybe_limit = argv[argi];
        if (!maybe_limit.empty() && maybe_limit[0] != '-') {
            num_insts = std::stoi(maybe_limit);
            ++argi;
        }
    }
    for (; argi < argc; ++argi) {
        extra_args.emplace_back(argv[argi]);
    }

    bool roi_mode = false;
    std::vector<uint8_t> roi_begin_encoding;
    std::vector<uint8_t> roi_end_encoding;
    for (size_t i = 0; i < extra_args.size(); ++i) {
        if (extra_args[i] == "--roi-begin-encoding" && i + 1 < extra_args.size()) {
            roi_begin_encoding = parse_hex_bytes(extra_args[++i]);
            roi_mode = true;
        } else if (extra_args[i] == "--roi-end-encoding" && i + 1 < extra_args.size()) {
            roi_end_encoding = parse_hex_bytes(extra_args[++i]);
            roi_mode = true;
        }
    }
    if (roi_mode && (roi_begin_encoding.empty() || roi_end_encoding.empty())) {
        std::cerr << "ROI mode requires both --roi-begin-encoding and --roi-end-encoding\n";
        return 1;
    }
    
    // Load and print microarchitecture configuration
    auto config = MicroArchConfig::load_from_file(kDefaultConfigPath);
    std::cout << "--- MicroArchitecture Configuration (Loaded from " << kDefaultConfigPath << ") ---\n";
    std::cout << "ROB Size: " << config.rob_size << "\n";
    std::cout << "Issue Width: " << config.issue_width << "\n";
    std::cout << "Load Queue: " << config.lq_size << ", Store Queue: " << config.sq_size << "\n";
    std::cout << "L1D Cache: " << config.l1d.size_kb << "KB, Latency: " << config.l1d.latency << " cycles\n";
    std::cout << "L2 Cache: " << config.l2.size_kb << "KB, Latency: " << config.l2.latency << " cycles\n";
    std::cout << "---------------------------------------------------------------------\n\n";

    MemoryHierarchy mem(config);
    IntervalCore core(config, &mem);
    InstDecoder decoder;

    TraceReader reader(trace_file);

    Instruction inst;
    uint64_t count = 0;
    uint64_t roi_begin_count = 0;
    uint64_t roi_end_count = 0;
    bool stats_enabled = !roi_mode;
    bool roi_started = !roi_mode;
    bool roi_finished = false;
    
    auto start_time = std::chrono::high_resolution_clock::now();

    while ((num_insts == -1 || count < static_cast<uint64_t>(num_insts)) &&
           reader.get_next_instruction(inst)) {
        if (roi_mode && !roi_started && inst.encoding == roi_begin_encoding) {
            core.reset_stats();
            mem.reset_stats();
            stats_enabled = true;
            roi_started = true;
            roi_begin_count = count;
            continue;
        }
        if (roi_mode && roi_started && inst.encoding == roi_end_encoding) {
            roi_end_count = count;
            roi_finished = true;
            break;
        }

        core.step(inst, &decoder);
        count++;
        
        if (stats_enabled && count % 100000 == 0) {
            std::cout << "Simulated " << count << " instructions...\n";
        }
    }
    
    core.finish();
    
    auto end_time = std::chrono::high_resolution_clock::now();
    std::chrono::duration<double> elapsed = end_time - start_time;
    double elapsed_sec = elapsed.count();
    double mips = (count / 1000000.0) / elapsed_sec;

    std::cout << "\n--- Simulation Finished ---\n";
    std::cout << "Total macro-instructions read: " << count << "\n";
    if (roi_mode) {
        std::cout << "ROI Begin Seen:        " << (roi_started ? "yes" : "no") << "\n";
        std::cout << "ROI End Seen:          " << (roi_finished ? "yes" : "no") << "\n";
        std::cout << "ROI Begin Trace Index: " << roi_begin_count << "\n";
        std::cout << "ROI End Trace Index:   " << roi_end_count << "\n";
    }
    std::cout << "Simulation Time: " << elapsed_sec << " seconds\n";
    std::cout << "Simulation Speed: " << mips << " MIPS (Million Instructions Per Second)\n";
    
    core.print_stats();
    mem.print_stats();

    return 0;
}
