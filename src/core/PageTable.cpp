#include "core/PageTable.h"
#include <iostream>
#include <cmath>

namespace minesim {

PageTable::PageTable(const MemoryConfig& config) {
    uint32_t page_size_bytes = config.page_size_kb * 1024;
    page_shift_ = static_cast<uint32_t>(std::log2(page_size_bytes));
    page_offset_mask_ = page_size_bytes - 1;
    
    // Calculate total number of pages allowed
    uint64_t total_memory_bytes = static_cast<uint64_t>(config.size_mb) * 1024 * 1024;
    max_pages_ = total_memory_bytes / page_size_bytes;
    next_free_ppn_ = 0;

    std::cout << "Initialized Page Table"
              << " | Memory: " << config.size_mb << " MB"
              << " | Page Size: " << config.page_size_kb << " KB"
              << " | Max Pages: " << max_pages_ << "\n";
}

Addr PageTable::translate(Addr vpn) {
    auto it = vpn_to_ppn_.find(vpn);
    if (it != vpn_to_ppn_.end()) {
        return it->second;
    }

    // "Page Fault": OS allocates a new physical page
    if (next_free_ppn_ >= max_pages_) {
        // Since this is a simple simulator, if we exceed memory we can either
        // warn the user or just simulate swapping (which might be too complex for now)
        // For now, we print a warning once.
        static bool memory_full_warning_printed = false;
        if (!memory_full_warning_printed) {
            std::cerr << "Warning: Simulator memory capacity exceeded! Simulating memory overcommit without swap penalties.\n";
            memory_full_warning_printed = true;
        }
    }
    
    Addr ppn = next_free_ppn_++;
    vpn_to_ppn_[vpn] = ppn;
    return ppn;
}

} // namespace minesim
