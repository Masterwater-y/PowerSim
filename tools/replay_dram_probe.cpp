// Offline diagnostic only. Input times and DRAM timing parameters must use
// the same units (cycles or ticks). Never called by the production simulator.
// Build against a FASTSIM_ENABLE_TEST_HOOKS library, for example:
// c++ -std=c++17 -O2 -DFASTSIM_ENABLE_TEST_HOOKS -Iinclude \
//   tools/replay_dram_probe.cpp build/libfastsim_lib.a -lpthread -o tmp/dram-probe
#include "fastsim/config.hpp"
#include "fastsim/simulator.hpp"

#include <fstream>
#include <iostream>
#include <stdexcept>
#include <string>
#include <vector>
#include <sstream>

int main(int argc, char** argv) {
    try {
        if (argc != 5) {
            throw std::runtime_error(
                "usage: dram-probe CONFIG REQUESTS ordered|frfcfs WINDOW\n"
                "REQUESTS: decimal time, cache-line number, ordinal per line");
        }
        const auto config = fastsim::load_simulator_config(argv[1]);
        std::ifstream input(argv[2]);
        if (!input) throw std::runtime_error("cannot open requests");
        std::vector<fastsim::testing::DramScheduleRequest> requests;
        fastsim::testing::DramScheduleRequest request;
        std::vector<bool> writes;
        std::string text;
        while (std::getline(input,text)) {
            if(text.empty())continue;
            std::istringstream row(text);std::string kind="R",extra;
            if(!(row>>request.arrival>>request.line>>request.ordinal))throw std::runtime_error("malformed request row");
            row>>kind;
            if((kind!="R" && kind!="W") || (row>>extra))throw std::runtime_error("malformed request kind");
            requests.push_back(request);
            writes.push_back(kind=="W");
        }
        if (!input.eof()) throw std::runtime_error("malformed request row");
        const std::string mode(argv[3]);
        if (mode == "ordered" || mode == "mixed") {
            std::vector<fastsim::testing::DramControllerProbeEvent> events;
            for (std::size_t i=0;i<requests.size();++i) events.push_back({requests[i].arrival,requests[i].line,writes[i]});
            const auto result = fastsim::testing::run_dram_controller_probe(
                config.dram, config.l1d.line_size, events);
            // This probe is read-only with fixed CL. The model enforces
            // tBURST at command issue, so data-bus time equals command+CL.
            const std::uint64_t tail = config.dram.t_cl + config.dram.burst_cycles +
                config.dram.frontend_latency + config.dram.backend_latency;
            for (std::size_t i = 0; i < requests.size(); ++i) {
                if(writes[i])continue; // enqueue acknowledgment is not DRAM write completion
                std::cout << requests[i].ordinal << ' ' << result.completions[i] - tail
                          << ' ' << result.completions[i] << " -1\n";
            }
            std::cerr << "write_enqueues=" << result.write_enqueues << " writes_drained=" << result.writes_drained
                      << " pending_writes=" << result.pending_final << '\n';
        } else if (mode == "frfcfs") {
            const auto window = std::stoul(argv[4]);
            if (!window) throw std::runtime_error("window must be positive");
            const auto result = fastsim::testing::run_dram_schedule_probe(
                config.dram, config.l1d.line_size, requests, window);
            for (const auto& rank:result.refresh)
                std::cerr << "refresh controller=" << rank.controller << " rank=" << rank.rank
                          << " preparations=" << rank.preparations << " precharged=" << rank.precharged_ranks
                          << " idle_skipped=" << rank.idle_skipped << " next=" << rank.next_prepare_tick
                          << " available=" << rank.available_tick << '\n';
            for (std::size_t i = 0; i < requests.size(); ++i) {
                std::cout << requests[i].ordinal << ' ' << result.command_cycles[i]
                          << ' ' << result.completions[i] << ' '
                          << unsigned(result.row_hits[i]) << '\n';
            }
        } else {
            throw std::runtime_error("unknown mode");
        }
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return 1;
    }
}
