#pragma once

#include <array>
#include <cstdint>
#include "fastsim/config.hpp"
#include "fastsim/types.hpp"

namespace fastsim {

// Stable resource classes shared by the lower-bound scheduler and the
// response repair.  They describe the configured target FUPool; they are not
// host-worker lanes and do not encode workload-specific behavior.
enum class IntervalFuPool : std::uint8_t {
    kInteger,
    kIntegerMultiply,
    kFloatSimple,
    kFloatComplex,
    kSimd,
    kPredicate,
    kMemory,
    kSystem,
    kCount,
};
static_assert(static_cast<std::size_t>(IntervalFuPool::kCount) ==
              kSpeculativeProfilePoolCount);


struct TargetOpTraits {
    IntervalFuPool pool = IntervalFuPool::kInteger;
    std::uint32_t latency = 1;
    bool pipelined = true;
};

// Pure hardware metadata shared by both execution engines. Memory and syscall
// completion are owned by each engine's lifecycle, not this non-memory table.
inline std::array<TargetOpTraits, 128> target_op_traits(const SimulatorConfig& config) {
    std::array<TargetOpTraits, 128> table{};
    for (auto& traits : table) {
        traits = TargetOpTraits{IntervalFuPool::kInteger,
                          config.integer_alu_latency,
                          config.integer_alu_pipelined};
    }
    table[2] = TargetOpTraits{IntervalFuPool::kIntegerMultiply,
                               config.integer_multiply_latency,
                               config.integer_multiply_pipelined};
    table[3] = TargetOpTraits{IntervalFuPool::kIntegerMultiply,
                               config.integer_divide_latency,
                               config.integer_divide_pipelined};
    for (int op = 4; op <= 6; ++op) {
        table[static_cast<std::size_t>(op)] =
            TargetOpTraits{IntervalFuPool::kFloatSimple,
                     config.float_simple_latency,
                     config.float_simple_pipelined};
    }
    table[7] = TargetOpTraits{IntervalFuPool::kFloatComplex,
                               config.float_multiply_latency,
                               config.float_complex_pipelined};
    table[8] = TargetOpTraits{
        IntervalFuPool::kFloatComplex,
        config.float_multiply_accumulate_latency,
        config.float_complex_pipelined};
    table[9] = TargetOpTraits{IntervalFuPool::kFloatComplex,
                               config.float_divide_latency,
                               config.float_divide_pipelined};
    table[10] = TargetOpTraits{IntervalFuPool::kFloatComplex,
                                config.float_misc_latency,
                                config.float_complex_pipelined};
    table[11] = TargetOpTraits{IntervalFuPool::kFloatComplex,
                                config.float_sqrt_latency,
                                config.float_sqrt_pipelined};
    for (int op = 12; op <= 55; ++op) {
        table[static_cast<std::size_t>(op)] =
            TargetOpTraits{IntervalFuPool::kSimd, config.simd_latency, true};
    }
    table[51] = TargetOpTraits{IntervalFuPool::kPredicate,
                                config.predicate_latency, true};
    for (int op = 77; op <= 86; ++op) {
        table[static_cast<std::size_t>(op)] =
            TargetOpTraits{IntervalFuPool::kSimd, config.simd_latency, true};
    }
    table[87] = TargetOpTraits{IntervalFuPool::kFloatSimple,
                                config.float_simple_latency,
                                config.float_simple_pipelined};
    for (std::size_t op = 88; op < table.size(); ++op) {
        table[op] = TargetOpTraits{IntervalFuPool::kSystem,
                                    config.system_latency, true};
    }
    return table;
}

inline std::array<std::uint32_t, kSpeculativeProfilePoolCount>
target_fu_counts(const SimulatorConfig& c) {
    return {c.integer_alu_units, c.integer_multiply_units,
            c.float_simple_units, c.float_complex_units, c.simd_units,
            c.predicate_units, c.memory_units, c.system_units};
}

}  // namespace fastsim
