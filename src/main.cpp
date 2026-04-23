#include <iostream>
#include <chrono>
#include "trace/TraceReader.h"
#include "core/Config.h"
#include "core/InstDecoder.h"
#include "core/MemoryHierarchy.h"
#include "core/IntervalCore.h"

using namespace minesim;

int main(int argc, char** argv) {
    if (argc < 2) {
        std::cerr << "Usage: " << argv[0] << " <trace_file.gz> [num_instructions]\n";
        return 1;
    }

    std::string trace_file = argv[1];
    int num_insts = (argc > 2) ? std::stoi(argv[2]) : -1;
    
    // Load and print microarchitecture configuration
    auto config = MicroArchConfig::load_from_file("config/cascade_lake.cfg");
    std::cout << "--- MicroArchitecture Configuration (Loaded from config/cascade_lake.cfg) ---\n";
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
    
    auto start_time = std::chrono::high_resolution_clock::now();

    while ((num_insts == -1 || count < num_insts) && reader.get_next_instruction(inst)) {
        core.step(inst, &decoder);
        count++;
        
        if (count % 100000 == 0) {
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
    std::cout << "Simulation Time: " << elapsed_sec << " seconds\n";
    std::cout << "Simulation Speed: " << mips << " MIPS (Million Instructions Per Second)\n";
    
    core.print_stats();
    mem.print_stats();

    return 0;
}
