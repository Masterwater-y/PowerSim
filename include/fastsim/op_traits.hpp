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
    // SPR-only in-memory identities preserve the previous coarse timing when
    // the SPR scheduler is disabled; they do not change the on-disk FST ABI.
    table[kSprSimdPack] = table[17];
    table[kSprSimdExtract] = table[17];
    table[kSprSimdInsert] = table[17];
    table[kSprStoreAddress] = table[57];
    table[kSprStoreData] = table[1];
    if (config.causal_spr_resources) {
        // Source preset: gem5-spr/src/arch/x86/X86CPU.py::spr_fu_pool,
        // inheriting FuncUnitConfig.py OpDesc defaults. This pure metadata
        // selector does not enable the legacy interval SPR scheduler. Native
        // source adapters still gate canonical classes not yet qualified.
        for (int op : {1, 2, 4, 5, 6, 7, 8, 9, 10, 17, 21, 25, 29})
            table[op].pipelined = true;
        table[1].latency=1;table[2].latency=3;
        table[3].latency=1;table[3].pipelined=false;
        for (int op : {4, 5, 6, 25})table[op].latency=2;
        table[7].latency=4;table[8].latency=5;
        table[9].latency=14;table[10].latency=1;
        table[11].latency=24;table[11].pipelined=false;
        table[17].latency=1;table[21].latency=1;
        table[29].latency=11;
        // X86CPU.py::X86StoreAddressPort/X86StoreDataPort inherit OpDesc's
        // opLat=1 and pipelined=true (cpu/FuncUnit.py); spr_fu_pool preserves
        // both. These identities must not inherit the coarse OFF ALU knobs.
        for (int op : {kSprStoreAddress, kSprStoreData}) {
            table[op].latency=1;
            table[op].pipelined=true;
        }
    }
    return table;
}

// Port capabilities follow the instantiated SPR proxy in models/o3.py.
// The bitmask models physical contention, not a claim about real hardware.
inline std::uint16_t spr_port_mask(const TraceRecord& record) {
    constexpr auto bit = [](unsigned p) { return static_cast<std::uint16_t>(1u << p); };
    const auto op = record.canonical_op_class();
    if (op == 0 && !record.is_memory() && !record.is_syscall()) return 0;
    if (record.is_syscall()) return bit(0);
    if (has_flag(record.flags, kBranch) && op == 1) return bit(0) | bit(6);
    if (op == kSprStoreData) return bit(4) | bit(9);
    if (record.is_memory()) {
        return record.is_write() ? bit(7) | bit(8)
                                 : bit(2) | bit(3) | bit(10);
    }
    switch (op) {
      case 1: return bit(0) | bit(1) | bit(5) | bit(6) | bit(11);
      case 2: case 4: case 17: case 25: return bit(1) | bit(5);
      case 3: return bit(1);
      case 7: case 15: case 21: case 31: return bit(0) | bit(1);
      case 9: case 11: case 23: case 24: case 29: case 34: case 51:
      case 88: case kSprSimdExtract: return bit(0);
      case kSprSimdPack: case kSprSimdInsert: return bit(5);
      case kSprStoreAddress: return bit(7) | bit(8);
      default: return bit(0) | bit(1) | bit(5);
    }
}

inline std::uint32_t spr_issue_interval(const TraceRecord& record) {
    const auto op = record.canonical_op_class();
    return op == 9 ? 4u : op == 29 ? 3u : 1u;
}

inline std::array<std::uint32_t, kSpeculativeProfilePoolCount>
target_fu_counts(const SimulatorConfig& c) {
    return {c.integer_alu_units, c.integer_multiply_units,
            c.float_simple_units, c.float_complex_units, c.simd_units,
            c.predicate_units, c.memory_units, c.system_units};
}

}  // namespace fastsim
