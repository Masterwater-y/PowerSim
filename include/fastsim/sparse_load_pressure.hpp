#pragma once

#include <algorithm>
#include <cstdint>
#include <limits>
#include <stdexcept>

namespace fastsim {

// Optimistic successful-load demand bound for members already ready before
// the same member-slot callback. No attempt/FU history or dynamic storage.
class SparseLoadPressure {
  public:
    struct Result {
        std::uint64_t cycle;
        std::uint64_t members;
        bool new_cohort;
    };

    Result constrain(std::uint64_t release, std::uint64_t admitted,
                     std::uint32_t ports) {
        if (!ports || admitted < release)
            throw std::invalid_argument("invalid sparse load pressure input");
        const bool fresh = !valid_ || release != release_;
        const auto count = fresh ? 0 : members_;
        if (count == std::numeric_limits<std::uint64_t>::max() ||
            release > std::numeric_limits<std::uint64_t>::max() - count / ports)
            throw std::overflow_error("sparse load pressure bound overflow");
        const auto bound = release + count / ports;
        release_ = release;
        members_ = count + 1;
        valid_ = true;
        return {std::max(admitted, bound), members_, fresh};
    }

  private:
    std::uint64_t release_ = 0;
    std::uint64_t members_ = 0;
    bool valid_ = false;
};

} // namespace fastsim
