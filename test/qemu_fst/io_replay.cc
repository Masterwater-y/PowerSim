#include <cstdio>
#include <filesystem>
#include <iostream>
#include <memory>
#include <stdexcept>
#include <string>
#include <vector>

#include "fastsim/simulator.hpp"

using namespace fastsim;

static void
require(bool value, const char *message)
{
    if (!value) {
        throw std::runtime_error(message);
    }
}

int
main(int argc, char **argv)
{
    try {
        if (argc != 2) {
            return 2;
        }
        std::filesystem::create_directories(argv[1]);
        const std::string path =
            std::string(argv[1]) + "/io-domain.fst";
        {
            BinaryTraceWriter writer(path, 0);
            TraceRecord ram;
            ram.pc = 0x1000;
            writer.append(ram);

            TraceRecord mmio;
            mmio.pc = 0x1004;
            mmio.address = 0xfec00000;
            mmio.size = 4;
            mmio.flags = kRetires | kLoad | kPhysicalAddress;
            writer.append(mmio, nullptr, {}, AccessDomain::kMmio);

            TraceRecord pio;
            pio.pc = 0x1008;
            pio.address = 0x80;
            pio.size = 1;
            pio.flags = kRetires | kLoad;
            writer.append(pio, nullptr, {}, AccessDomain::kPio);
            writer.close();
        }

        BinaryTraceSource input(path);
        TraceRecord record;
        require(
            input.next(record) &&
                input.current_access_domain() == AccessDomain::kRam &&
                input.next(record) &&
                input.current_access_domain() == AccessDomain::kMmio &&
                input.next(record) &&
                input.current_access_domain() == AccessDomain::kPio &&
                !input.next(record),
            "FST I/O companion lost sparse record domains");

        SimulatorConfig config;
        config.core_model = "interval_weave";
        config.allow_mmio_escape = true;
        config.strict_physical_address = true;
        config.require_virtual_page_token = true;
        std::vector<std::unique_ptr<TraceSource>> traces;
        traces.push_back(std::make_unique<BinaryTraceSource>(path));
        Simulator simulator(config, std::move(traces));
        const auto total = simulator.run().total_core();
        require(
            total.retired_uops == 3 && total.memory_uops == 2 &&
                total.memory_accesses == 0 &&
                total.mmio_escape_accesses == 2 &&
                total.unknown_addresses == 0 &&
                total.page_fault_untracked_accesses == 0,
            "I/O replay entered the RAM hierarchy");

        std::remove((path + ".io").c_str());
        bool rejected = false;
        try {
            BinaryTraceSource missing(path);
            (void)missing;
        } catch (const std::runtime_error&) {
            rejected = true;
        }
        require(rejected, "FST bit 7 without .io did not fail closed");
        std::remove(path.c_str());
        return 0;
    } catch (const std::exception &error) {
        std::cerr << error.what() << '\n';
        return 1;
    }
}
