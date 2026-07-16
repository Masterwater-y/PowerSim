// Differential oracle for TCSim v29. AddrRange operations come directly from
// the checked-out gem5 C++ header; the arithmetic below mirrors
// DRAMInterface::decodePacket for RoRaBaCoCh and the current Ruby topology.
#include <cstdint>
#include <iostream>
#include <vector>

#include "base/addr_range.hh"

namespace gem5 {
Logger &Logger::getPanic() { static Logger value("panic: "); return value; }
Logger &Logger::getFatal() { static Logger value("fatal: "); return value; }
Logger &Logger::getWarn() { static Logger value("warn: "); return value; }
Logger &Logger::getInfo() { static Logger value("info: "); return value; }
Logger &Logger::getHack() { static Logger value("hack: "); return value; }
} // namespace gem5

int main()
{
    constexpr uint64_t end = 4294967296ULL;
    constexpr uint64_t burst = 64;
    constexpr uint64_t columns = 128;
    constexpr uint64_t banks = 16;
    constexpr uint64_t ranks = 2;
    const std::vector<gem5::Addr> masks{64, 128, 256};
    std::vector<gem5::AddrRange> ranges;
    for (uint8_t channel = 0; channel < 8; ++channel)
        ranges.emplace_back(0, end, masks, channel);

    uint64_t address = 0;
    while (std::cin >> address) {
        int channel = -1;
        uint64_t controller_address = 0;
        for (int candidate = 0; candidate < 8; ++candidate) {
            if (ranges[candidate].contains(address)) {
                channel = candidate;
                controller_address = ranges[candidate].getOffset(address);
                break;
            }
        }
        if (channel < 0)
            return 3;
        uint64_t burst_address = controller_address / burst;
        uint64_t column = burst_address % columns;
        uint64_t value = burst_address / columns;
        uint64_t bank = value % banks;
        value /= banks;
        uint64_t rank = value % ranks;
        value /= ranks;
        // DRAMInterface derives rowsPerBank from the assigned controller
        // address-range capacity, not from device_size.
        uint64_t controller_capacity = ranges[channel].size();
        uint64_t rows = controller_capacity / (8192 * banks * ranks);
        uint64_t row = value % rows;
        uint64_t line = address / 64;
        uint64_t l1_set = line % 64;
        uint64_t l2_set = line % 2048;
        uint64_t llc_bank = line % 8;
        uint64_t llc_set = (line / 8) % 8192;
        std::cout << address << ' ' << line << ' ' << l1_set << ' '
                  << l2_set << ' ' << llc_set << ' ' << llc_bank << ' '
                  << channel << ' ' << rank << ' ' << bank << ' ' << row
                  << ' ' << column << '\n';
    }
    return 0;
}
