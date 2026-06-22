#include <iostream>
#include <string>
#include <unordered_set>
#include <unordered_map>
#include <zlib.h>
#include <iomanip>

// Define missing types from DynamoRIO trace_entry.h
// Note: Some of these types are typical in DR offline traces
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
    TRACE_TYPE_THREAD_EXIT = 29,
    TRACE_TYPE_PID = 30,
    TRACE_TYPE_THREAD = 46,
    TRACE_TYPE_ENCODING = 47,
    TRACE_TYPE_INSTR_TAKEN_JUMP = 48,
    TRACE_TYPE_INSTR_UNTAKEN_JUMP = 49,
};

enum trace_marker_type_t {
    TRACE_MARKER_TYPE_VERSION = 0,
    TRACE_MARKER_TYPE_FILETYPE = 1,
    TRACE_MARKER_TYPE_TIMESTAMP = 2,
    TRACE_MARKER_TYPE_CPUID = 3,
    TRACE_MARKER_TYPE_PAGE_SIZE = 4,
    TRACE_MARKER_TYPE_CACHE_LINE_SIZE = 5,
    TRACE_MARKER_TYPE_CHUNK_INSTR_COUNT = 6,
    TRACE_MARKER_TYPE_CHUNK_FOOTPRINT = 7,
    TRACE_MARKER_TYPE_SYSCALL = 8,
    TRACE_MARKER_TYPE_PHYSICAL_ADDRESS = 9,
    TRACE_MARKER_TYPE_VIRTUAL_ADDRESS = 10,
};

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

int main(int argc, char** argv) {
    if (argc < 2) {
        std::cerr << "Usage: " << argv[0] << " <trace_file.gz> [num_entries]\n";
        return 1;
    }

    std::string trace_file = argv[1];
    uint64_t max_entries = (argc > 2) ? std::stoull(argv[2]) : 1000000;

    gzFile file = gzopen(trace_file.c_str(), "rb");
    if (!file) {
        std::cerr << "Failed to open trace file: " << trace_file << "\n";
        return 1;
    }

    trace_entry_t entry;
    uint64_t entry_count = 0;
    
    std::unordered_set<uint64_t> pids;
    std::unordered_set<uint64_t> tids;
    std::unordered_map<uint16_t, uint64_t> marker_counts;

    uint64_t instr_count = 0;

    while (entry_count < max_entries && gzread(file, &entry, sizeof(trace_entry_t)) == sizeof(trace_entry_t)) {
        entry_count++;

        if (entry.type == TRACE_TYPE_PID) {
            pids.insert(entry.addr);
        } else if (entry.type == TRACE_TYPE_THREAD) {
            tids.insert(entry.addr);
        } else if (entry.type == TRACE_TYPE_MARKER) {
            marker_counts[entry.size]++; // In DynamoRIO, entry.size holds the marker type
        } else if ((entry.type >= TRACE_TYPE_INSTR && entry.type <= TRACE_TYPE_INSTR_RETURN) ||
                   entry.type == TRACE_TYPE_INSTR_TAKEN_JUMP || entry.type == TRACE_TYPE_INSTR_UNTAKEN_JUMP) {
            instr_count++;
        }
    }

    gzclose(file);

    std::cout << "--- Trace Analysis Report ---\n";
    std::cout << "Total Entries Parsed: " << entry_count << "\n";
    std::cout << "Total Instructions: " << instr_count << "\n\n";

    std::cout << "PIDs observed (" << pids.size() << "): ";
    for (auto pid : pids) std::cout << pid << " ";
    std::cout << "\n";

    std::cout << "TIDs observed (" << tids.size() << "): ";
    for (auto tid : tids) std::cout << tid << " ";
    std::cout << "\n\n";

    std::cout << "Markers observed:\n";
    for (const auto& kv : marker_counts) {
        std::cout << "  Marker Type " << kv.first << ": " << kv.second << " occurrences\n";
    }
    
    if (pids.size() <= 1 && tids.size() <= 1) {
        std::cout << "\n[CONCLUSION]: This trace appears to be SINGLE-THREADED and SINGLE-PROCESS.\n";
    } else {
        std::cout << "\n[CONCLUSION]: This trace appears to be MULTI-THREADED or MULTI-PROCESS.\n";
    }
    
    return 0;
}