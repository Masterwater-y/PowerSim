#pragma once

#include "core/Cache.h"
#include "core/TLB.h"
#include "core/PageTable.h"
#include <memory>

namespace minesim {

class MemoryHierarchy {
public:
    MemoryHierarchy(const MicroArchConfig& config);

    // Interface for Instruction Fetch (ITLB -> STLB -> PT -> L1I -> L2 -> L3)
    // Returns the total latency in cycles for the access
    uint32_t fetch_instruction(Addr vaddr, uint64_t current_cycle);

    // Interface for Data Read (DTLB -> STLB -> PT -> L1D -> L2 -> L3)
    // Returns the total latency in cycles for the access
    uint32_t read_data(Addr vaddr, uint64_t current_cycle);

    // Interface for Data Write (DTLB -> STLB -> PT -> L1D -> L2 -> L3)
    // Returns the total latency in cycles for the access
    uint32_t write_data(Addr vaddr, uint64_t current_cycle);

    // Print statistics for all caches and TLBs in the hierarchy
    void print_stats() const;

private:
    std::unique_ptr<Cache> l1i_;
    std::unique_ptr<Cache> l1d_;
    std::unique_ptr<Cache> l2_;
    std::unique_ptr<Cache> l3_;

    std::unique_ptr<TLB> itlb_;
    std::unique_ptr<TLB> dtlb_;
    std::unique_ptr<TLB> stlb_;

    std::unique_ptr<PageTable> page_table_;

    // Configuration
    uint32_t main_memory_latency_;
    uint32_t page_walk_latency_;

    // Helper method for accessing the data hierarchy
    uint32_t access_data_hierarchy(Addr vaddr, bool is_write, uint64_t current_cycle);
    
    // Helper method for translating virtual address to physical address
    // Returns the translation latency (TLB hits/misses + Page Walk)
    uint32_t translate_address(Addr vaddr, Addr& paddr, bool is_instruction, uint64_t current_cycle);
};

} // namespace minesim
