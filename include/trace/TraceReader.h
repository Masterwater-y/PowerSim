#pragma once
#include "common/Types.h"
#include <string>
#include <memory>
#include <vector>
#include <unordered_map>
#include <zlib.h>

namespace minesim {

class TraceReader {
public:
    explicit TraceReader(const std::string& filename);
    ~TraceReader();

    bool get_next_instruction(Instruction& inst);
    bool is_eof() const;

private:
    gzFile file_;
    bool eof_;
    std::vector<uint8_t> current_encoding_;
    
    Instruction pending_inst_;
    bool has_pending_inst_;
    
    std::unordered_map<Addr, std::vector<uint8_t>> encoding_cache_;
    
    // Internal struct to match trace_entry_t from dynamorio
    #pragma pack(push, 1)
    struct trace_entry_t {
        uint16_t type;
        uint16_t size;
        union {
            uint64_t addr;
            uint8_t length[8];
            uint8_t encoding[8];
        };
    };
    #pragma pack(pop)

    bool read_entry(trace_entry_t& entry);
};

} // namespace minesim