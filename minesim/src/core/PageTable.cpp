#include "core/PageTable.h"
#include <iostream>
#include <cmath>

namespace minesim {

PageTable::PageTable(const MemoryConfig& config) {
    uint64_t total_memory_bytes = static_cast<uint64_t>(config.size_mb) * 1024ULL * 1024ULL;
    max_4k_pages_ = total_memory_bytes / (4ULL * 1024ULL);
    next_free_4k_ppn_ = 0;

    thp_page_size_kb_ = config.thp_page_size_kb;
    thp_min_vaddr_ = config.thp_min_vaddr;

    std::cout << "Initialized Page Table"
              << " | Memory: " << config.size_mb << " MB"
              << " | Default Page: " << config.default_page_size_kb << " KB"
              << " | THP Page: " << thp_page_size_kb_ << " KB"
              << " | THP Min vaddr: 0x" << std::hex << thp_min_vaddr_ << std::dec
              << " | Max 4K Pages: " << max_4k_pages_ << "\n";
}

PageSize PageTable::page_size_for(Addr vaddr) const {
    if (thp_page_size_kb_ <= 4) return PageSize::SIZE_4K;
    if (vaddr < thp_min_vaddr_) return PageSize::SIZE_4K;
    if (thp_page_size_kb_ >= 1024 * 1024) return PageSize::SIZE_1G; // 1 GB
    return PageSize::SIZE_2M;
}

Addr PageTable::translate(Addr vaddr, PageSize sz) {
    Addr vpn = get_vpn(vaddr, sz);

    std::unordered_map<Addr, Addr>* tbl = &vpn_to_ppn_4k_;
    uint64_t alloc_4k_per_page = 1;
    switch (sz) {
        case PageSize::SIZE_4K: tbl = &vpn_to_ppn_4k_; alloc_4k_per_page = 1ULL; break;
        case PageSize::SIZE_2M: tbl = &vpn_to_ppn_2m_; alloc_4k_per_page = 512ULL; break;
        case PageSize::SIZE_1G: tbl = &vpn_to_ppn_1g_; alloc_4k_per_page = 262144ULL; break;
    }

    auto it = tbl->find(vpn);
    if (it != tbl->end()) {
        return it->second;
    }

    if (next_free_4k_ppn_ + alloc_4k_per_page > max_4k_pages_) {
        static bool memory_full_warning_printed = false;
        if (!memory_full_warning_printed) {
            std::cerr << "Warning: Simulator memory capacity exceeded! Simulating memory overcommit without swap penalties.\n";
            memory_full_warning_printed = true;
        }
    }

    // The PPN is expressed in units of the chosen page size: shift the global
    // 4 KB allocator down by the size-specific shift difference.
    Addr ppn = next_free_4k_ppn_ / alloc_4k_per_page;
    next_free_4k_ppn_ += alloc_4k_per_page;

    (*tbl)[vpn] = ppn;
    return ppn;
}

} // namespace minesim
