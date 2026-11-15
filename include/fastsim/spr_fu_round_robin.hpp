#pragma once
#include <array>
#include <cstdint>
namespace fastsim {
class SprFuRoundRobin {
public:
    int select(unsigned op_class, std::uint32_t mask, std::uint32_t available) {
        constexpr unsigned ports = 12;
        mask &= (1u << ports) - 1;
        if (!mask) return -1;
        auto& next = next_[op_class < next_.size() ? op_class : 88];
        int first = -1;
        for (unsigned i=0; i<ports; ++i) {
            const auto port = (next + i) % ports;
            if (!(mask & (1u << port))) continue;
            if (first < 0) first = static_cast<int>(port);
            if (available & (1u << port)) {
                next = static_cast<std::uint8_t>((port + 1) % ports);
                return static_cast<int>(port);
            }
        }
        // gem5's failed circular search reads the first entry a second time.
        next = static_cast<std::uint8_t>((first + 1) % ports);
        return -1;
    }
private:
    std::array<std::uint8_t, 128> next_{};
};
}
