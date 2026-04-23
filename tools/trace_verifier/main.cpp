#include <iostream>
#include <iomanip>
#include <vector>
#include <string>
#include "dr_api.h"
#include "trace/TraceReader.h"
#include "core/InstDecoder.h"

using namespace minesim;

class TraceVerifier {
public:
    TraceVerifier() {
        // No dr_standalone_init() here, InstDecoder handles it
    }

    ~TraceVerifier() {
    }

    void disassemble_and_print(const Instruction& inst, bool compare_mode) {
        // ... (rest remains unchanged)
        
        std::vector<MicroOp> uops = decoder_.decode(inst);
        
        if (!compare_mode) {
            std::cout << "Disasm: " << uops.front().parent_disasm << "\n";
            for (size_t i = 0; i < uops.size(); ++i) {
                std::cout << "  -> Uop[" << i << "]: ";
                switch(uops[i].type) {
                    case InstType::ALU: std::cout << "ALU    "; break;
                    case InstType::BRANCH: std::cout << "BRANCH "; break;
                    case InstType::LOAD: std::cout << "LOAD   "; break;
                    case InstType::STORE: std::cout << "STORE  "; break;
                    default: std::cout << "UNKNOWN"; break;
                }
                if (uops[i].type == InstType::LOAD || uops[i].type == InstType::STORE) {
                    std::cout << "Addr: 0x" << std::hex << uops[i].mem_addr << std::dec 
                              << " Size: " << uops[i].mem_size;
                }
                if (uops[i].type == InstType::BRANCH) {
                    if (uops[i].is_conditional_branch) {
                        std::cout << (uops[i].is_branch_taken ? "(taken)" : "(untaken)");
                    } else {
                        std::cout << "(unconditional)";
                    }
                }
                std::cout << "\n";
            }
        } else {
            // Compare mode logic...
            std::string disasm_str = uops.front().parent_disasm;
            if (disasm_str.empty() || disasm_str[0] == '<') {
                std::cout << "0x" << std::hex << std::setfill('0') << std::setw(16) << inst.pc << std::dec << " " << disasm_str << "\n";
                return;
            }
            
            // Remove trailing spaces
            auto end = disasm_str.find_last_not_of(" \t");
            if (end != std::string::npos) {
                disasm_str = disasm_str.substr(0, end + 1);
            }
            
            if (inst.type == InstType::BRANCH && inst.is_conditional_branch) {
                disasm_str += inst.is_branch_taken ? " (taken)" : " (untaken)";
            }
            
            std::cout << "0x" << std::hex << std::setfill('0') << std::setw(16) << inst.pc << std::dec << " " << disasm_str << "\n";
        }
    }

private:
    InstDecoder decoder_;
};

int main(int argc, char** argv) {
    if (argc < 2) {
        std::cerr << "Usage: " << argv[0] << " <trace_file.gz> [num_instructions] [--compare]\n";
        return 1;
    }

    std::string trace_file = argv[1];
    int num_insts = -1;
    bool compare_mode = false;
    
    for (int i = 2; i < argc; ++i) {
        std::string arg = argv[i];
        if (arg == "--compare") {
            compare_mode = true;
        } else {
            num_insts = std::stoi(arg);
        }
    }

    TraceReader reader(trace_file);
    TraceVerifier verifier;

    Instruction inst;
    int count = 0;
    
    if (!compare_mode) {
        std::cout << "Starting trace verification...\n";
        std::cout << "--------------------------------------------------------------------------------\n";
    }
    
    while ((num_insts == -1 || count < num_insts) && reader.get_next_instruction(inst)) {
        verifier.disassemble_and_print(inst, compare_mode);
        count++;
    }

    if (!compare_mode) {
        std::cout << "--------------------------------------------------------------------------------\n";
        std::cout << "Verification complete. Read " << count << " instructions.\n";
    }
    
    return 0;
}
