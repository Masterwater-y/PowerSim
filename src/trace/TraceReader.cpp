#include "trace/TraceReader.h"
#include <iostream>
#include <cstring>

namespace minesim {

// Trace types from DynamoRIO trace_entry.h
enum trace_type_t {
    TRACE_TYPE_READ = 0,
    TRACE_TYPE_WRITE = 1,
    TRACE_TYPE_INSTR = 10,
    TRACE_TYPE_INSTR_DIRECT_JUMP = 11,
    TRACE_TYPE_INSTR_INDIRECT_JUMP = 12,
    TRACE_TYPE_INSTR_CONDITIONAL_JUMP = 13,
    TRACE_TYPE_INSTR_DIRECT_CALL = 14,
    TRACE_TYPE_INSTR_INDIRECT_CALL = 15,
    TRACE_TYPE_INSTR_RETURN = 16,
    TRACE_TYPE_INSTR_BUNDLE = 17,
    TRACE_TYPE_MARKER = 25,
    TRACE_TYPE_HEADER = 28,
    TRACE_TYPE_ENCODING = 47,
    TRACE_TYPE_INSTR_TAKEN_JUMP = 48,
    TRACE_TYPE_INSTR_UNTAKEN_JUMP = 49,
};

TraceReader::TraceReader(const std::string& filename) : eof_(false), file_(nullptr), has_pending_inst_(false) {
    file_ = gzopen(filename.c_str(), "rb");
    if (!file_) {
        std::cerr << "Failed to open trace file: " << filename << std::endl;
        eof_ = true;
    }
}

TraceReader::~TraceReader() {
    if (file_) {
        gzclose(file_);
    }
}

bool TraceReader::is_eof() const {
    return eof_;
}

bool TraceReader::read_entry(trace_entry_t& entry) {
    if (eof_) return false;
    int bytes_read = gzread(file_, &entry, sizeof(trace_entry_t));
    if (bytes_read < static_cast<int>(sizeof(trace_entry_t))) {
        eof_ = true;
        return false;
    }
    return true;
}

bool TraceReader::get_next_instruction(Instruction& inst) {
    if (eof_ && !has_pending_inst_) return false;

    if (has_pending_inst_) {
        inst = pending_inst_;
        has_pending_inst_ = false;
    } else {
        // Read until we find the first instruction
        trace_entry_t entry;
        bool found = false;
        while (read_entry(entry)) {
            if (entry.type == TRACE_TYPE_ENCODING) {
                for (int i = 0; i < entry.size && i < 8; ++i) {
                    current_encoding_.push_back(entry.encoding[i]);
                }
            } else if ((entry.type >= TRACE_TYPE_INSTR && entry.type <= TRACE_TYPE_INSTR_RETURN) ||
                       entry.type == TRACE_TYPE_INSTR_TAKEN_JUMP || entry.type == TRACE_TYPE_INSTR_UNTAKEN_JUMP) {
                inst.pc = entry.addr;
                inst.size = entry.size;
                if (!current_encoding_.empty()) {
                    inst.encoding = current_encoding_;
                    encoding_cache_[inst.pc] = current_encoding_;
                    current_encoding_.clear();
                } else {
                    inst.encoding = encoding_cache_[inst.pc];
                }
                
                inst.type = InstType::ALU;
                inst.is_conditional_branch = false;
                inst.is_branch_taken = false;
                if (entry.type == TRACE_TYPE_INSTR_CONDITIONAL_JUMP || 
                    entry.type == TRACE_TYPE_INSTR_DIRECT_JUMP || 
                    entry.type == TRACE_TYPE_INSTR_INDIRECT_JUMP ||
                    entry.type == TRACE_TYPE_INSTR_DIRECT_CALL ||
                    entry.type == TRACE_TYPE_INSTR_INDIRECT_CALL ||
                    entry.type == TRACE_TYPE_INSTR_RETURN ||
                    entry.type == TRACE_TYPE_INSTR_TAKEN_JUMP ||
                    entry.type == TRACE_TYPE_INSTR_UNTAKEN_JUMP) {
                    inst.type = InstType::BRANCH;
                    if (entry.type == TRACE_TYPE_INSTR_TAKEN_JUMP || entry.type == TRACE_TYPE_INSTR_UNTAKEN_JUMP || entry.type == TRACE_TYPE_INSTR_CONDITIONAL_JUMP) {
                        inst.is_conditional_branch = true;
                    }
                    inst.is_branch_taken = (entry.type == TRACE_TYPE_INSTR_TAKEN_JUMP || entry.type == TRACE_TYPE_INSTR_CONDITIONAL_JUMP);
                }
                found = true;
                break;
            }
        }
        if (!found) return false;
    }

    // Now look ahead for memory references or the next instruction
    trace_entry_t entry;
    while (read_entry(entry)) {
        if (entry.type == TRACE_TYPE_READ || entry.type == TRACE_TYPE_WRITE) {
            inst.is_mem_access = true;
            inst.mem_addr = entry.addr;
            inst.mem_size = entry.size;
            if (inst.type == InstType::ALU) {
                inst.type = (entry.type == TRACE_TYPE_READ) ? InstType::LOAD : InstType::STORE;
            }
        } else if (entry.type == TRACE_TYPE_ENCODING) {
            for (int i = 0; i < entry.size && i < 8; ++i) {
                current_encoding_.push_back(entry.encoding[i]);
            }
        } else if ((entry.type >= TRACE_TYPE_INSTR && entry.type <= TRACE_TYPE_INSTR_RETURN) ||
                   entry.type == TRACE_TYPE_INSTR_TAKEN_JUMP || entry.type == TRACE_TYPE_INSTR_UNTAKEN_JUMP) {
            // Found the NEXT instruction
            pending_inst_.pc = entry.addr;
            pending_inst_.size = entry.size;
            if (!current_encoding_.empty()) {
                pending_inst_.encoding = current_encoding_;
                encoding_cache_[pending_inst_.pc] = current_encoding_;
                current_encoding_.clear();
            } else {
                pending_inst_.encoding = encoding_cache_[pending_inst_.pc];
            }
            
            pending_inst_.type = InstType::ALU;
            pending_inst_.is_mem_access = false;
            pending_inst_.is_conditional_branch = false;
            pending_inst_.is_branch_taken = false;
            if (entry.type == TRACE_TYPE_INSTR_CONDITIONAL_JUMP || 
                entry.type == TRACE_TYPE_INSTR_DIRECT_JUMP || 
                entry.type == TRACE_TYPE_INSTR_INDIRECT_JUMP ||
                entry.type == TRACE_TYPE_INSTR_DIRECT_CALL ||
                entry.type == TRACE_TYPE_INSTR_INDIRECT_CALL ||
                entry.type == TRACE_TYPE_INSTR_RETURN ||
                entry.type == TRACE_TYPE_INSTR_TAKEN_JUMP ||
                entry.type == TRACE_TYPE_INSTR_UNTAKEN_JUMP) {
                pending_inst_.type = InstType::BRANCH;
                if (entry.type == TRACE_TYPE_INSTR_TAKEN_JUMP || entry.type == TRACE_TYPE_INSTR_UNTAKEN_JUMP || entry.type == TRACE_TYPE_INSTR_CONDITIONAL_JUMP) {
                    pending_inst_.is_conditional_branch = true;
                }
                pending_inst_.is_branch_taken = (entry.type == TRACE_TYPE_INSTR_TAKEN_JUMP || entry.type == TRACE_TYPE_INSTR_CONDITIONAL_JUMP);
            }
            has_pending_inst_ = true;
            break;
        }
    }

    return true;
}

} // namespace minesim