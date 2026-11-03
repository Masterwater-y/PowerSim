// Fixed-input diagnostic. Never consumes gem5 timing and never feeds results
// into the production simulator. CSV is diagnostics.projected_dram_path output.
#include "fastsim/config.hpp"
#include "fastsim/mixed_dram.hpp"
#include <algorithm>
#include <array>
#include <charconv>
#include <chrono>
#include <fstream>
#include <iostream>
#include <stdexcept>
#include <string>
#include <string_view>
#include <unordered_map>
#include <vector>

namespace {
struct Row {
    std::uint64_t phase, batch, arrival, line, core, sequence, ordinal, command, response;
    bool write;
};
std::uint64_t number(std::string_view value) {
    std::uint64_t result = 0;
    const auto parsed = std::from_chars(value.data(), value.data() + value.size(), result);
    if (parsed.ec != std::errc{} || parsed.ptr != value.data() + value.size())
        throw std::runtime_error("invalid integer in projected DRAM CSV");
    return result;
}
Row parse(const std::string& line) {
    std::array<std::string_view, 10> fields;
    std::size_t start = 0;
    for (std::size_t i = 0; i < fields.size(); ++i) {
        const auto end = line.find(',', start);
        if ((end == std::string::npos) != (i + 1 == fields.size()))
            throw std::runtime_error("invalid projected DRAM CSV column count");
        fields[i] = std::string_view(line).substr(start,
            end == std::string::npos ? end : end - start);
        start = end + 1;
    }
    if (fields[2] != "R" && fields[2] != "W")
        throw std::runtime_error("invalid projected DRAM request kind");
    Row r{number(fields[0]), number(fields[1]), number(fields[3]), number(fields[4]),
          number(fields[5]), number(fields[6]), number(fields[7]), number(fields[8]),
          number(fields[9]), fields[2] == "W"};
    if (r.phase > 1 || (!r.write && (r.command < r.arrival || r.response < r.command)))
        throw std::runtime_error("invalid projected DRAM stage order");
    return r;
}
}

int main(int argc, char** argv) {
    try {
        if (argc != 5) throw std::runtime_error(
            "usage: replay_projected_dram CONFIG INPUT.csv OUTPUT.csv SUMMARY.json");
        const auto config = fastsim::load_simulator_config(argv[1]);
        auto mixed = fastsim::make_mixed_dram_config(config.dram);
        if (config.dram.frfcfs_topology_scaled_window) {
            const auto lanes = std::uint64_t(config.cores) *
                config.dram.ranks_per_channel / config.dram.channels;
            const auto cap = config.dram.frfcfs_selection_window ?
                config.dram.frfcfs_selection_window : config.dram.read_buffer_size;
            mixed.dram.frfcfs_selection_window = static_cast<std::uint32_t>(
                std::min<std::uint64_t>(cap, lanes > 1 ? lanes - 1 : 1));
        }
        fastsim::MixedDramController controller(mixed, config.llc.line_size);
        std::ifstream input(argv[2]);
        std::ofstream output(argv[3]), summary(argv[4]);
        if (!input || !output || !summary) throw std::runtime_error("cannot open replay files");
        std::string line;
        std::getline(input, line);
        if (line != "phase,batch,kind,arrival,line,core,sequence,ordinal,command,response")
            throw std::runtime_error("unsupported projected DRAM CSV header");
        output << "phase,batch,core,sequence,ordinal,arrival,old_command,old_response,"
                  "effective_arrival,admission,selection,new_command,new_response\n";
        std::vector<fastsim::MixedDramRequest> requests;
        std::unordered_map<std::uint64_t, Row> reads;
        std::uint64_t id = 0, batch = 0, phase = 0, rows = 0, returned = 0, ns = 0;
        std::uint64_t measured_reads = 0, old_wait = 0, new_wait = 0;
        bool have_batch = false;
        const auto flush = [&] {
            if (requests.empty()) return;
            const auto started = std::chrono::steady_clock::now();
            const auto completions = controller.reserve_projected_batch(requests);
            ns += static_cast<std::uint64_t>(std::chrono::duration_cast<std::chrono::nanoseconds>(
                std::chrono::steady_clock::now() - started).count());
            for (const auto& done : completions) {
                if (done.id.kind != fastsim::DramServiceKind::kRead) continue;
                const auto& r = reads.at(done.id.sequence);
                output << r.phase << ',' << r.batch << ',' << r.core << ',' << r.sequence << ','
                    << r.ordinal << ',' << r.arrival << ',' << r.command << ',' << r.response << ','
                    << done.arrival << ',' << done.admission << ',' << done.selection << ','
                    << done.command << ',' << done.response << '\n';
                if (r.phase) {
                    ++measured_reads;
                    old_wait += r.command - r.arrival;
                    new_wait += done.command - r.arrival;
                }
                reads.erase(done.id.sequence);
                ++returned;
            }
            if (!reads.empty()) throw std::runtime_error("projected batch lost a read");
            requests.clear();
        };
        while (std::getline(input, line)) {
            const auto r = parse(line);
            if (have_batch && (r.batch < batch || r.phase < phase))
                throw std::runtime_error("non-monotonic phase/batch");
            if (have_batch && r.batch != batch) flush();
            if (have_batch && r.batch == batch && r.phase != phase)
                throw std::runtime_error("phase changes within batch");
            batch = r.batch; phase = r.phase; have_batch = true; ++rows; ++id;
            requests.push_back({{0, id, 0, r.write ? fastsim::DramServiceKind::kWriteback :
                                                  fastsim::DramServiceKind::kRead}, r.arrival, r.line});
            if (!r.write) reads.emplace(id, r);
        }
        if (!input.eof()) throw std::runtime_error("projected CSV read failed");
        flush();
        const auto s = controller.stats();
        if (s.submitted != rows || s.admitted != rows || returned != s.reads_serviced ||
            s.reads_serviced + s.writes_serviced + s.pending_writes != rows)
            throw std::runtime_error("projected service conservation failure");
        summary << "{\n\"contract\":\"projected-batch-reservation-v1\","
            << "\n\"production_feedback\":false,\n\"requests\":" << rows
            << ",\n\"reads\":" << s.reads_serviced << ",\n\"writes_serviced\":" << s.writes_serviced
            << ",\n\"pending_writes\":" << s.pending_writes
            << ",\n\"batches\":" << s.projected_batches
            << ",\n\"effective_selection_window\":" << mixed.dram.frfcfs_selection_window
            << ",\n\"late_arrivals\":" << s.projected_late_arrivals
            << ",\n\"late_cycles\":" << s.projected_late_cycles
            << ",\n\"events\":" << s.projected_events
            << ",\n\"scanned_entries\":" << s.projected_scanned_entries
            << ",\n\"controller_wall_ns\":" << ns
            << ",\n\"measurement_reads\":" << measured_reads
            << ",\n\"measurement_old_command_wait\":" << old_wait
            << ",\n\"measurement_new_command_wait\":" << new_wait << "\n}\n";
        output.flush(); summary.flush();
        if (!output || !summary) throw std::runtime_error("replay output write failed");
        std::cout << "replayed " << rows << " requests; " << returned << " reads; "
                  << s.pending_writes << " retained writes\n";
    } catch (const std::exception& e) {
        std::cerr << e.what() << '\n';
        return 1;
    }
}
