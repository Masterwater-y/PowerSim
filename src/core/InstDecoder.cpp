#include "core/InstDecoder.h"
#include "dr_api.h"
#include <iostream>
#include <sstream>

namespace minesim {

static bool dr_initialized = false;

static void init_dr_env() {
    if (!dr_initialized) {
        dr_standalone_init();
        disassemble_set_syntax(DR_DISASM_ATT);
        dr_initialized = true;
    }
}

InstDecoder::InstDecoder() {
    init_dr_env();
    dcontext_ = GLOBAL_DCONTEXT;
}

InstDecoder::~InstDecoder() {
}

std::vector<MicroOp> InstDecoder::decode(const Instruction& macro_inst) {
    std::vector<MicroOp> uops;

    if (macro_inst.encoding.empty()) {
        // Fallback for missing encoding (should not happen with caching TraceReader)
        MicroOp uop;
        uop.type = macro_inst.type;
        uop.pc = macro_inst.pc;
        uop.mem_addr = macro_inst.mem_addr;
        uop.mem_size = macro_inst.mem_size;
        uop.is_branch_taken = macro_inst.is_branch_taken;
        uop.is_conditional_branch = macro_inst.is_conditional_branch;
        uop.parent_disasm = "<no encoding>";
        uops.push_back(uop);
        return uops;
    }

    instr_t *dr_inst = instr_create(dcontext_);
    byte* next_pc = decode_from_copy(dcontext_, 
                                     (byte*)macro_inst.encoding.data(), 
                                     (byte*)macro_inst.pc, 
                                     dr_inst);

    if (next_pc == NULL || !instr_valid(dr_inst)) {
        // Decoding failed fallback
        MicroOp uop;
        uop.type = macro_inst.type;
        uop.pc = macro_inst.pc;
        uop.parent_disasm = "<decode failed>";
        uops.push_back(uop);
        instr_destroy(dcontext_, dr_inst);
        return uops;
    }

    // Get parent disassembly string
    char buf[256];
    instr_disassemble_to_buffer(dcontext_, dr_inst, buf, sizeof(buf));
    std::string disasm_str(buf);
    auto end = disasm_str.find_last_not_of(" \t\n\r");
    if (end != std::string::npos) {
        disasm_str = disasm_str.substr(0, end + 1);
    }

    bool reads_mem = instr_reads_memory(dr_inst);
    bool writes_mem = instr_writes_memory(dr_inst);
    bool is_branch = instr_is_cbr(dr_inst) || instr_is_ubr(dr_inst) || 
                     instr_is_call(dr_inst) || instr_is_return(dr_inst);
    
    int opcode = instr_get_opcode(dr_inst);

    // Serialization Check
    bool is_serializing = false;
    if (opcode == OP_cpuid || opcode == OP_mfence || opcode == OP_sfence || 
        opcode == OP_lfence || opcode == OP_iret || opcode == OP_invd || 
        opcode == OP_wbinvd) {
        is_serializing = true;
    }

    // Heuristic: Does this instruction require an ALU execution port?
    bool has_alu = true;
    
    // Pure branches do not typically require a general ALU port (they resolve in Branch Unit)
    if (opcode >= OP_jmp && opcode <= OP_jnle) {
        has_alu = false;
    }
    
    // Pure data movement
    if (opcode == OP_mov_st || opcode == OP_mov_ld || 
        opcode == OP_movzx || opcode == OP_movsx || opcode == OP_movsxd ||
        opcode == OP_vmovaps || opcode == OP_vmovups || opcode == OP_movaps || 
        opcode == OP_movups || opcode == OP_movdqa || opcode == OP_movdqu) {
        // If it interacts with memory, it's just a LOAD/STORE
        if (reads_mem || writes_mem) {
            has_alu = false;
        }
    }
    
    if (opcode == OP_push || opcode == OP_push_imm || opcode == OP_pop) {
        has_alu = false;
    }

    // Extract registers for dependencies
    std::vector<uint16_t> mem_read_regs;
    std::vector<uint16_t> mem_write_regs;
    std::vector<uint16_t> inst_src_regs;
    std::vector<uint16_t> inst_dst_regs;

    for (int i = 0; i < instr_num_srcs(dr_inst); i++) {
        opnd_t src = instr_get_src(dr_inst, i);
        if (opnd_is_reg(src)) {
            inst_src_regs.push_back(opnd_get_reg(src));
        } else if (opnd_is_memory_reference(src) || opnd_is_base_disp(src)) {
            reg_id_t base = opnd_get_base(src);
            if (base != DR_REG_NULL) mem_read_regs.push_back(base);
            reg_id_t index = opnd_get_index(src);
            if (index != DR_REG_NULL) mem_read_regs.push_back(index);
        }
    }
    
    for (int i = 0; i < instr_num_dsts(dr_inst); i++) {
        opnd_t dst = instr_get_dst(dr_inst, i);
        if (opnd_is_reg(dst)) {
            inst_dst_regs.push_back(opnd_get_reg(dst));
        } else if (opnd_is_memory_reference(dst) || opnd_is_base_disp(dst)) {
            reg_id_t base = opnd_get_base(dst);
            if (base != DR_REG_NULL) mem_write_regs.push_back(base);
            reg_id_t index = opnd_get_index(dst);
            if (index != DR_REG_NULL) mem_write_regs.push_back(index);
        }
    }

    // Unique IDs for intermediate values within a macro-instruction
    const uint16_t VREG_LOAD = 1000;
    const uint16_t VREG_ALU = 1001;

    // Push Uops in logical execution order
    
    // 1. LOAD Uop
    if (reads_mem) {
        MicroOp uop;
        uop.type = InstType::LOAD;
        uop.pc = macro_inst.pc;
        uop.mem_addr = macro_inst.mem_addr;
        uop.mem_size = macro_inst.mem_size;
        uop.parent_disasm = disasm_str;
        uop.is_serializing = is_serializing;
        uop.src_regs = mem_read_regs;
        uop.dst_regs = {VREG_LOAD};
        uops.push_back(uop);
    }

    // 2. ALU Uop (Arithmetic / Logic / Address calculation)
    if (has_alu) {
        MicroOp uop;
        uop.type = InstType::ALU;
        uop.pc = macro_inst.pc;
        uop.parent_disasm = disasm_str;
        uop.is_serializing = is_serializing;
        uop.src_regs = inst_src_regs;
        if (reads_mem) {
            uop.src_regs.push_back(VREG_LOAD);
        }
        uop.dst_regs = inst_dst_regs;
        if (writes_mem || is_branch) {
            uop.dst_regs.push_back(VREG_ALU);
        }
        uops.push_back(uop);
    }

    // 3. STORE Uop
    if (writes_mem) {
        MicroOp uop;
        uop.type = InstType::STORE;
        uop.pc = macro_inst.pc;
        uop.mem_addr = macro_inst.mem_addr;
        uop.mem_size = macro_inst.mem_size;
        uop.parent_disasm = disasm_str;
        uop.is_serializing = is_serializing;
        uop.src_regs = mem_write_regs;
        
        if (has_alu) {
            uop.src_regs.push_back(VREG_ALU);
        } else if (reads_mem) {
            uop.src_regs.push_back(VREG_LOAD);
        } else {
            // Include instruction source registers directly if no ALU operation (e.g. push reg)
            uop.src_regs.insert(uop.src_regs.end(), inst_src_regs.begin(), inst_src_regs.end());
        }
        uops.push_back(uop);
    }

    // 4. BRANCH Uop
    if (is_branch) {
        MicroOp uop;
        uop.type = InstType::BRANCH;
        uop.pc = macro_inst.pc;
        uop.is_branch_taken = macro_inst.is_branch_taken;
        uop.is_conditional_branch = instr_is_cbr(dr_inst);
        uop.parent_disasm = disasm_str;
        uop.is_serializing = is_serializing;
        
        if (has_alu) {
            uop.src_regs.push_back(VREG_ALU);
        } else {
            uop.src_regs = inst_src_regs;
        }
        uops.push_back(uop);
    }

    // Fallback: If no rules matched, at least it's an ALU uop
    if (uops.empty()) {
        MicroOp uop;
        uop.type = InstType::ALU;
        uop.pc = macro_inst.pc;
        uop.parent_disasm = disasm_str;
        uop.is_serializing = is_serializing;
        uop.src_regs = inst_src_regs;
        uop.dst_regs = inst_dst_regs;
        uops.push_back(uop);
    }

    instr_destroy(dcontext_, dr_inst);
    return uops;
}

} // namespace minesim