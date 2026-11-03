// Read-only production decoder/calendar probe. No gem5 timing is consumed.
#include "fastsim/config.hpp"
#include "fastsim/dram_address.hpp"
#include "fastsim/simulator.hpp"
#include <algorithm>
#include <fstream>
#include <iostream>
#include <numeric>
#include <stdexcept>
#include <string>
#include <vector>

int main(int argc, char** argv) {
    try {
        if (argc != 4) throw std::runtime_error(
            "usage: probe_dram_mechanisms CONFIG ADDRESSES.txt OUTPUT.json");
        const auto config = fastsim::load_simulator_config(argv[1]);
        const auto& d = config.dram;
        const auto line_size = config.llc.line_size;
        std::ifstream input(argv[2]);
        if (!input) throw std::runtime_error("cannot read probe addresses");
        std::vector<std::uint64_t> addresses, lines;
        std::uint64_t address;
        while (input >> address) {
            if (address % line_size) throw std::runtime_error("unaligned probe address");
            addresses.push_back(address); lines.push_back(address / line_size);
        }
        if (!input.eof()) throw std::runtime_error("invalid probe address");
        const auto decoded = fastsim::testing::run_dram_address_probe(d, line_size, lines);
        std::ofstream output(argv[3]);
        if (!output) throw std::runtime_error("cannot write probe output");
        output << "{\"line_size\":" << line_size << ",\"addresses\":[";
        for (std::size_t i = 0; i < addresses.size(); ++i) {
            if (i) output << ',';
            const auto& a = decoded[i];
            output << "{\"address\":" << addresses[i] << ",\"decoded\":{\"channel\":"
                << a.channel << ",\"rank\":" << a.rank << ",\"bank\":" << a.bank
                << ",\"bank_group\":" << a.bank_group << ",\"row\":" << a.row << "}}";
        }
        output << "],\"scenarios\":[";
        const std::uint64_t channels = fastsim::dram_is_split(d) ? d.channels / 2 : d.channels;
        const auto columns = d.row_bytes / line_size;
        const auto bank_stride = columns * channels;
        const auto row_stride = bank_stride * d.banks_per_channel * d.ranks_per_channel;
        bool first = true;
        for (const std::string name : {"same_row", "alternating_rows", "grouped_rows",
                                      "independent_banks", "independent_channels"}) {
            for (const bool sparse : {false, true}) {
                for (unsigned variant = 0; variant < 4; ++variant) {
                    auto candidate = d;
                    if (variant == 2) candidate.frfcfs_full_queue_page_policy = true;
                    if (variant == 3) candidate.frfcfs_row_cap_single_precharge = true;
                    const unsigned window = variant == 1 ? 1 : d.read_buffer_size;
                    const std::uint64_t spacing = sparse
                        ? std::uint64_t(d.t_rp) + d.t_rcd + d.t_cl + d.burst_cycles : 1;
                    std::vector<fastsim::testing::DramScheduleRequest> requests;
                    for (unsigned i = 0; i < 64; ++i) {
                        std::uint64_t line = i % columns;
                        if (name == "same_row") line *= channels;
                        if (name == "alternating_rows") line = (i % 2) * row_stride + (i / 2) * channels;
                        if (name == "grouped_rows") line = (i / 32) * row_stride + (i % 32) * channels;
                        if (name == "independent_banks") line = (i % d.banks_per_channel) * bank_stride + (i / d.banks_per_channel) * channels;
                        if (name == "independent_channels") line = i % channels + (i / channels) * channels;
                        requests.push_back({1000 + i * spacing, line, i});
                    }
                    const auto r = fastsim::testing::run_dram_schedule_probe(
                        candidate, line_size, requests, window);
                    if (r.completions.size() != requests.size())
                        throw std::runtime_error("probe request conservation failure");
                    std::uint64_t wait = 0;
                    for (std::size_t i = 0; i < requests.size(); ++i) {
                        if (r.command_cycles[i] < requests[i].arrival)
                            throw std::runtime_error("probe command before arrival");
                        wait += r.command_cycles[i] - requests[i].arrival;
                    }
                    if (!first) output << ',';
                    first = false;
                    output << "{\"name\":\"" << name << "\",\"sparse\":" << (sparse ? "true" : "false")
                        << ",\"variant\":\"" << (variant == 0 ? "baseline" : variant == 1 ? "window_one" :
                                                      variant == 2 ? "full_page_queue" : "single_cap_precharge")
                        << "\",\"requests\":" << requests.size() << ",\"command_wait_sum\":" << wait
                        << ",\"row_hits\":" << std::accumulate(r.row_hits.begin(), r.row_hits.end(), 0ull)
                        << ",\"last_response\":" << *std::max_element(r.completions.begin(), r.completions.end())
                        << ",\"max_candidates\":" << r.max_selection_candidates
                        << ",\"max_admitted\":" << r.max_admitted_pending
                        << ",\"row_cap_precharges\":" << r.row_cap_precharges
                        << ",\"adaptive_precharges\":" << r.adaptive_precharges << '}';
                }
            }
        }
        output << "],\"write_parameter_activation\":[";
        // Force a real canonical write drain. A changed recovery/turnaround
        // constraint should be observable on a suitable direction/conflict
        // stream; equality records a coverage gap, never a fitted correction.
        std::vector<fastsim::testing::DramControllerProbeEvent> write_stream;
        for (unsigned i = 0; i <= d.write_buffer_size; ++i)
            write_stream.push_back({1000 + i, (i % 2) * row_stride, true});
        for (unsigned i = 0; i < d.min_reads_per_switch + 2; ++i)
            write_stream.push_back({2000 + i, (i % 2) * row_stride, false});
        const auto canonical = fastsim::testing::run_dram_controller_probe(d, line_size, write_stream);
        if (!canonical.writes_drained) throw std::runtime_error("write activation probe did not drain");
        const std::vector<std::pair<const char*, std::uint32_t fastsim::DramConfig::*>> fields{
            {"t_cwl", &fastsim::DramConfig::t_cwl}, {"t_rcd_wr", &fastsim::DramConfig::t_rcd_wr},
            {"t_ccd_l_wr", &fastsim::DramConfig::t_ccd_l_wr}, {"t_rtw", &fastsim::DramConfig::t_rtw},
            {"t_wtr", &fastsim::DramConfig::t_wtr}, {"t_wtr_l", &fastsim::DramConfig::t_wtr_l},
            {"t_wr", &fastsim::DramConfig::t_wr}};
        for (std::size_t i = 0; i < fields.size(); ++i) {
            if (i) output << ',';
            auto changed = d;
            changed.*fields[i].second = 2 * std::max(1u, d.*fields[i].second);
            const auto other = fastsim::testing::run_dram_controller_probe(changed, line_size, write_stream);
            output << "{\"field\":\"" << fields[i].first << "\",\"baseline\":" << d.*fields[i].second
                << ",\"perturbed\":" << changed.*fields[i].second
                << ",\"writes_drained\":" << canonical.writes_drained
                << ",\"read_responses_identical\":" << (canonical.completions == other.completions ? "true" : "false")
                << ",\"last_read_response\":" << canonical.completions.back()
                << ",\"perturbed_last_read_response\":" << other.completions.back() << '}';
        }
        output << "],\"configuration\":{\"size_bytes\":" << d.size_bytes
            << ",\"memory_layout\":\"" << d.memory_layout << "\",\"scheduler\":\"" << d.scheduler << '"';
        const std::vector<std::pair<const char*, std::uint32_t fastsim::DramConfig::*>> numeric{
            {"channels", &fastsim::DramConfig::channels},
            {"ranks_per_channel", &fastsim::DramConfig::ranks_per_channel},
            {"banks_per_channel", &fastsim::DramConfig::banks_per_channel},
            {"bank_groups_per_rank", &fastsim::DramConfig::bank_groups_per_rank},
            {"row_bytes", &fastsim::DramConfig::row_bytes},
            {"t_cl", &fastsim::DramConfig::t_cl}, {"t_rcd", &fastsim::DramConfig::t_rcd},
            {"t_rp", &fastsim::DramConfig::t_rp}, {"t_ras", &fastsim::DramConfig::t_ras},
            {"t_rtp", &fastsim::DramConfig::t_rtp}, {"t_rrd", &fastsim::DramConfig::t_rrd},
            {"t_rrd_l", &fastsim::DramConfig::t_rrd_l}, {"t_xaw", &fastsim::DramConfig::t_xaw},
            {"t_ccd_l", &fastsim::DramConfig::t_ccd_l}, {"t_cs", &fastsim::DramConfig::t_cs},
            {"burst_cycles", &fastsim::DramConfig::burst_cycles},
            {"frontend_latency", &fastsim::DramConfig::frontend_latency},
            {"backend_latency", &fastsim::DramConfig::backend_latency},
            {"activation_limit", &fastsim::DramConfig::activation_limit},
            {"read_buffer_size", &fastsim::DramConfig::read_buffer_size},
            {"write_buffer_size", &fastsim::DramConfig::write_buffer_size},
            {"write_high_threshold_percent", &fastsim::DramConfig::write_high_threshold_percent},
            {"write_low_threshold_percent", &fastsim::DramConfig::write_low_threshold_percent},
            {"min_reads_per_switch", &fastsim::DramConfig::min_reads_per_switch},
            {"min_writes_per_switch", &fastsim::DramConfig::min_writes_per_switch},
            {"frfcfs_selection_window", &fastsim::DramConfig::frfcfs_selection_window},
            {"frfcfs_passes", &fastsim::DramConfig::frfcfs_passes},
            {"frfcfs_arrival_bucket_cycles", &fastsim::DramConfig::frfcfs_arrival_bucket_cycles},
            {"max_accesses_per_row", &fastsim::DramConfig::max_accesses_per_row}};
        for (const auto& f : numeric) output << ",\"" << f.first << "\":" << d.*f.second;
        for (const auto& f : fields) output << ",\"" << f.first << "\":" << d.*f.second;
        output << ",\"t_refi_ticks\":" << d.t_refi_ticks
            << ",\"t_rfc_ticks\":" << d.t_rfc_ticks
            << ",\"refresh_ticks_per_cycle\":" << d.refresh_ticks_per_cycle
            << ",\"refresh_start_tick\":" << d.refresh_start_tick;
        const std::vector<std::pair<const char*, bool fastsim::DramConfig::*>> flags{
            {"refresh_enabled", &fastsim::DramConfig::refresh_enabled},
            {"separate_write_queue", &fastsim::DramConfig::separate_write_queue},
            {"frfcfs_topology_scaled_window", &fastsim::DramConfig::frfcfs_topology_scaled_window},
            {"frfcfs_full_queue_page_policy", &fastsim::DramConfig::frfcfs_full_queue_page_policy},
            {"frfcfs_row_cap_single_precharge", &fastsim::DramConfig::frfcfs_row_cap_single_precharge},
            {"frfcfs_causal_selection", &fastsim::DramConfig::frfcfs_causal_selection}};
        for (const auto& f : flags) output << ",\"" << f.first << "\":" << (d.*f.second ? "true" : "false");
        output << "}}\n";
        output.flush();
        if (!output) throw std::runtime_error("probe output write failed");
    } catch (const std::exception& e) {
        std::cerr << e.what() << '\n';
        return 1;
    }
}
