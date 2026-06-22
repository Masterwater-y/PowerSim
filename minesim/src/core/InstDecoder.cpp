#include "core/InstDecoder.h"
#include "dr_api.h"
#include <algorithm>
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

static void append_memory_operand_regs(opnd_t opnd, std::vector<uint16_t>& regs) {
    if (!opnd_is_base_disp(opnd)) {
        return;
    }
    reg_id_t base = opnd_get_base(opnd);
    if (base != DR_REG_NULL) regs.push_back(base);
    reg_id_t index = opnd_get_index(opnd);
    if (index != DR_REG_NULL) regs.push_back(index);
}

static void dedupe_regs(std::vector<uint16_t>& regs) {
    std::sort(regs.begin(), regs.end());
    regs.erase(std::unique(regs.begin(), regs.end()), regs.end());
}

static bool reg_lists_overlap(const std::vector<reg_id_t>& regs, reg_id_t target) {
    for (reg_id_t reg : regs) {
        if (reg != DR_REG_NULL && reg_overlap(reg, target)) {
            return true;
        }
    }
    return false;
}

static bool is_zero_idiom(instr_t* dr_inst, bool reads_mem, bool writes_mem) {
    if (reads_mem || writes_mem) {
        return false;
    }

    const int opcode = instr_get_opcode(dr_inst);
    switch (opcode) {
        case OP_xor:
        case OP_pxor:
        case OP_xorps:
        case OP_xorpd:
        case OP_vxorps:
        case OP_vxorpd:
        case OP_vpxor:
        case OP_vpxord:
        case OP_vpxorq:
            break;
        default:
            return false;
    }

    std::vector<reg_id_t> dst_regs;
    std::vector<reg_id_t> src_regs;
    for (int i = 0; i < instr_num_dsts(dr_inst); ++i) {
        opnd_t dst = instr_get_dst(dr_inst, i);
        if (opnd_is_reg(dst)) {
            dst_regs.push_back(opnd_get_reg(dst));
        }
    }
    for (int i = 0; i < instr_num_srcs(dr_inst); ++i) {
        opnd_t src = instr_get_src(dr_inst, i);
        if (opnd_is_reg(src)) {
            src_regs.push_back(opnd_get_reg(src));
        }
    }
    if (dst_regs.empty() || src_regs.empty()) {
        return false;
    }

    for (reg_id_t dst : dst_regs) {
        if (dst == DR_REG_NULL) {
            continue;
        }
        bool all_overlap = true;
        for (reg_id_t src : src_regs) {
            if (src == DR_REG_NULL) {
                continue;
            }
            if (!reg_overlap(src, dst)) {
                all_overlap = false;
                break;
            }
        }
        if (all_overlap) {
            return true;
        }
    }
    return false;
}

static bool is_simple_reg_move(instr_t* dr_inst, bool reads_mem, bool writes_mem) {
    if (reads_mem || writes_mem || !instr_is_mov(dr_inst)) {
        return false;
    }

    int reg_srcs = 0;
    int reg_dsts = 0;
    int non_reg_srcs = 0;
    for (int i = 0; i < instr_num_srcs(dr_inst); ++i) {
        opnd_t src = instr_get_src(dr_inst, i);
        if (opnd_is_reg(src)) {
            reg_srcs++;
        } else {
            non_reg_srcs++;
        }
    }
    for (int i = 0; i < instr_num_dsts(dr_inst); ++i) {
        opnd_t dst = instr_get_dst(dr_inst, i);
        if (opnd_is_reg(dst)) {
            reg_dsts++;
        }
    }
    return reg_srcs == 1 && reg_dsts == 1 && non_reg_srcs == 0;
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

    auto cache_it = decode_cache_.find(macro_inst.pc);
    if (cache_it != decode_cache_.end()) {
        uops = cache_it->second;
        for (auto& uop : uops) {
            uop.pc = macro_inst.pc;
            uop.mem_addr = macro_inst.mem_addr;
            uop.mem_size = macro_inst.mem_size;
            if (uop.type == InstType::BRANCH) {
                uop.is_branch_taken = macro_inst.is_branch_taken;
                uop.is_conditional_branch = macro_inst.is_conditional_branch;
            }
        }
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
    bool zero_idiom = is_zero_idiom(dr_inst, reads_mem, writes_mem);
    bool simple_reg_move = is_simple_reg_move(dr_inst, reads_mem, writes_mem);
    
    int opcode = instr_get_opcode(dr_inst);

    // Serialization Check
    bool is_serializing = false;
    if (opcode == OP_cpuid || opcode == OP_mfence || opcode == OP_sfence || 
        opcode == OP_lfence || opcode == OP_iret || opcode == OP_invd || 
        opcode == OP_wbinvd) {
        is_serializing = true;
    }
    // LOCK-prefixed RMW, xchg [mem], wrmsr are also full serializers on x86.
    if (instr_get_prefix_flag(dr_inst, PREFIX_LOCK)) {
        is_serializing = true;
    }
    if (opcode == OP_xchg && (reads_mem || writes_mem)) {
        is_serializing = true;
    }
    if (opcode == OP_wrmsr) {
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
        } else if (opnd_is_memory_reference(src)) {
            append_memory_operand_regs(src, mem_read_regs);
        }
    }
    
    for (int i = 0; i < instr_num_dsts(dr_inst); i++) {
        opnd_t dst = instr_get_dst(dr_inst, i);
        if (opnd_is_reg(dst)) {
            inst_dst_regs.push_back(opnd_get_reg(dst));
        } else if (opnd_is_memory_reference(dst)) {
            append_memory_operand_regs(dst, mem_write_regs);
        }
    }
    dedupe_regs(mem_read_regs);
    dedupe_regs(mem_write_regs);
    dedupe_regs(inst_src_regs);
    dedupe_regs(inst_dst_regs);

    if (zero_idiom) {
        std::vector<uint16_t> filtered_src_regs;
        for (uint16_t reg : inst_src_regs) {
            if (!reg_lists_overlap(inst_dst_regs, reg)) {
                filtered_src_regs.push_back(reg);
            }
        }
        inst_src_regs.swap(filtered_src_regs);
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
        uop.is_rename_only = zero_idiom || simple_reg_move;
        uop.src_regs = inst_src_regs;
        if (reads_mem) {
            uop.src_regs.push_back(VREG_LOAD);
        }
        dedupe_regs(uop.src_regs);
        uop.dst_regs = inst_dst_regs;
        if (writes_mem || is_branch) {
            uop.dst_regs.push_back(VREG_ALU);
        }
        dedupe_regs(uop.dst_regs);
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
        uop.store_addr_regs = mem_write_regs;
        uop.src_regs = mem_write_regs;
        
        if (has_alu) {
            uop.store_data_regs.push_back(VREG_ALU);
            uop.src_regs.push_back(VREG_ALU);
        } else if (reads_mem) {
            uop.store_data_regs.push_back(VREG_LOAD);
            uop.src_regs.push_back(VREG_LOAD);
        } else {
            // Include instruction source registers directly if no ALU operation (e.g. push reg)
            uop.store_data_regs.insert(uop.store_data_regs.end(), inst_src_regs.begin(), inst_src_regs.end());
            uop.src_regs.insert(uop.src_regs.end(), inst_src_regs.begin(), inst_src_regs.end());
        }
        dedupe_regs(uop.store_addr_regs);
        dedupe_regs(uop.store_data_regs);
        dedupe_regs(uop.src_regs);
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
        dedupe_regs(uop.src_regs);
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
        dedupe_regs(uop.src_regs);
        dedupe_regs(uop.dst_regs);
        uops.push_back(uop);
    }

    instr_destroy(dcontext_, dr_inst);
    decode_cache_[macro_inst.pc] = uops;
    return uops;
}

} // namespace minesim
