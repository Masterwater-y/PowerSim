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
    std::vector<uint8_t> encoding;
    
    // Memory access info (if any)
    Addr mem_addr = 0;
    uint32_t mem_size = 0;
    bool is_mem_access = false;
    bool is_branch_taken = false;
    bool is_conditional_branch = false;
};

} // namespace minesim