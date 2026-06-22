#pragma once
#include "common/Types.h"
#include <vector>
#include <string>
#include <unordered_map>

namespace minesim {

// Represent a fundamental RISC-like micro-operation
struct MicroOp {
    InstType type;          // LOAD, STORE, ALU, or BRANCH
    Addr pc;                // PC of the parent macro-instruction
    
    // Memory access information
    Addr mem_addr = 0;
    uint32_t mem_size = 0;
    
    // Branch information
    bool is_branch_taken = false;
    bool is_conditional_branch = false;

    // Serialization
    bool is_serializing = false;
    bool is_rename_only = false;

    // Register dependencies
    std::vector<uint16_t> src_regs;
    std::vector<uint16_t> dst_regs;
    std::vector<uint16_t> store_addr_regs;
    std::vector<uint16_t> store_data_regs;

    // Disassembly string of the parent macro-instruction (for debugging)
    std::string parent_disasm;
};

class InstDecoder {
public:
    InstDecoder();
    ~InstDecoder();

    // Decode a single macro-instruction into one or more micro-operations
    std::vector<MicroOp> decode(const Instruction& macro_inst);

private:
    void* dcontext_;
    std::unordered_map<Addr, std::vector<MicroOp>> decode_cache_;
};

} // namespace minesim
