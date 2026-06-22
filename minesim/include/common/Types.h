#pragma once
#include <cstdint>
#include <string>
#include <vector>

namespace minesim {

using Addr = uint64_t;
using Cycle = uint64_t;

enum class InstType {
    ALU,
    BRANCH,
    LOAD,
    STORE,
    UNKNOWN
};

struct Instruction {
    Addr pc;
    InstType type;
    uint8_t size;
    uint16_t trace_type = 0;
    std::vector<uint8_t> encoding;
    
    // Memory access info (if any)
    Addr mem_addr = 0;
    uint32_t mem_size = 0;
    bool is_mem_access = false;
    bool is_branch_taken = false;
    bool is_conditional_branch = false;

    // Extended branch classification (set by TraceReader from trace_type).
    bool is_unconditional_direct_branch = false; // direct jmp
    bool is_indirect_branch = false;             // indirect jmp / indirect call
    bool is_call = false;                        // direct or indirect call
    bool is_return = false;                      // ret
    Addr next_pc = 0;                            // PC of the following instruction (actual target for taken branches)
};

} // namespace minesim
