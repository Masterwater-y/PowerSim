#include "fastsim/dram_address.hpp"
#include "fastsim/mixed_dram.hpp"
#include "fastsim/simulator.hpp"
#include <stdexcept>
#include <vector>

namespace {
void require(bool value, const char* text) {
    if (!value) throw std::runtime_error(text);
}
}
void test_dram_address() {
    using namespace fastsim;
    constexpr std::uint64_t G = 1ull << 30;
    DramConfig d;
    d.size_bytes = 32 * G; d.channels = 16;
    require(dram_contains_address(d, 3 * G), "legacy contiguous map");
    require(!dram_contains_address(d, 32 * G), "legacy capacity limit");
    auto a = dram_decode_line(d, 17, 64);
    require(a.channel == 1 && a.local_line == 1, "legacy striping unchanged");
    d.memory_layout = "x86-low3g-high4g-v1";
    validate_dram_layout(d, 64);
    require(dram_contains_address(d, 0) && dram_contains_address(d, 3 * G - 1), "low endpoints");
    require(!dram_contains_address(d, 3 * G) && !dram_contains_address(d, 4 * G - 1), "IO hole excluded");
    require(dram_contains_address(d, 4 * G) && dram_contains_address(d, 33 * G - 1), "high endpoints");
    require(!dram_contains_address(d, 33 * G), "high end excluded");
    require(dram_physical_from_offset(d, 3 * G) == 4 * G, "packed offset skips hole");
    for (std::uint64_t line : {0ull, 7ull, 8ull, 1024ull, 8192ull}) {
        auto lo = dram_decode_line(d, line, 64);
        auto hi = dram_decode_line(d, 4 * G / 64 + line, 64);
        require(lo.channel == line % 8 && hi.channel == lo.channel + 8 &&
                lo.local_line == line / 8 && hi.local_line == lo.local_line, "regional controller decode");
    }
    d.scheduler = "fcfs";
    const std::vector<testing::DramScheduleRequest> requests{
        {1000, 0, 0}, {1000, 4 * G / 64, 1},
        {1000, 8, 2}, {1000, 4 * G / 64 + 8, 3},
        {2000, 33 * G / 64 - 1, 4}};
    const auto result = testing::run_dram_schedule_probe(d, 64, requests, 8);
    require(result.completions[0] == result.completions[1] &&
            result.completions[2] == result.completions[3] &&
            result.row_hits[2] && result.row_hits[3], "independent regional calendars and local row hits");
    bool rejected = false;
    try { testing::run_dram_schedule_probe(d, 64, {{0, 3 * G / 64, 0}}, 8); }
    catch (const std::out_of_range&) { rejected = true; }
    require(rejected, "normal controller rejects hole");
    MixedDramController mixed(make_mixed_dram_config(d), 64);
    mixed.submit({{0, 0, 0, DramServiceKind::kRead}, 1000, 0});
    mixed.submit({{0, 1, 0, DramServiceKind::kRead}, 1000, 4 * G / 64});
    mixed.submit({{0, 2, 0, DramServiceKind::kRead}, 1000, 33 * G / 64 - 1});
    const auto done = mixed.advance(100000);
    require(done.size() == 3 && done[0].response == done[1].response, "mixed regional independence/high end");
    rejected = false;
    try { mixed.submit({{0, 3, 0, DramServiceKind::kRead}, 100000, 3 * G / 64}); }
    catch (const std::exception&) { rejected = true; }
    require(rejected, "mixed controller rejects hole");
    SimulatorConfig config;
    config.dram = d;
    config.dram.memory_layout = "x86-low3g-high4g-v1";
    for (std::uint64_t page = 0; page < 100; ++page) {
        const auto modeled = modeled_instruction_physical_address(config, 17, page * 4096 + 37);
        const auto base = std::uint64_t{1} << (config.instruction_physical_address_bits - 1);
        require(modeled >= base && dram_contains_address(d, modeled - base) &&
                (modeled & 4095) == 37, "synthetic instruction placement skips hole");
    }
    require(!dram_contains_span(d, 3 * G - 1, 4 * G), "span crossing hole rejected");
    require(!dram_contains_line(d, ~std::uint64_t{0}, 64), "line multiplication overflow rejected");
    d.memory_layout = "typo";
    rejected = false;
    try { validate_dram_layout(d, 64); } catch (const std::invalid_argument&) { rejected = true; }
    require(rejected, "unknown layout rejected");
    d.memory_layout = "x86-low3g-high4g-v1"; d.channels = 1;
    rejected = false;
    try { validate_dram_layout(d, 64); } catch (const std::invalid_argument&) { rejected = true; }
    require(rejected, "split channels required");
}
