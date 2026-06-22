#pragma once

#include "common/Types.h"
#include "core/Config.h"
#include <unordered_map>

namespace minesim {

enum class PageSize {
    SIZE_4K = 0,
    SIZE_2M = 1,
    SIZE_1G = 2,
};

class PageTable {
public:
    PageTable(const MemoryConfig& config);

    // Decide which page size to use for the given virtual address.
    PageSize page_size_for(Addr vaddr) const;

    // Returns the page-shift (bits) for a given size.
    static uint32_t shift_for(PageSize sz) {
        switch (sz) {
            case PageSize::SIZE_4K: return 12;
            case PageSize::SIZE_2M: return 21;
            case PageSize::SIZE_1G: return 30;
        }
        return 12;
    }

    // Translate (vaddr, page_size) -> physical address.
    // Allocates a new PPN on first use (simulating OS page allocation).
    Addr translate(Addr vaddr, PageSize sz);

    // Helpers for building VPN/offset under a given page size.
    Addr get_vpn(Addr addr, PageSize sz) const {
        return addr >> shift_for(sz);
    }

    Addr get_offset(Addr addr, PageSize sz) const {
        return addr & ((1ULL << shift_for(sz)) - 1ULL);
    }

    Addr get_physical_address(Addr ppn, Addr offset, PageSize sz) const {
        return (ppn << shift_for(sz)) | offset;
    }

    uint64_t get_allocated_pages() const { return next_free_4k_ppn_; }
    uint64_t get_max_pages() const { return max_4k_pages_; }

private:
    // VPN -> PPN under each page size.
    std::unordered_map<Addr, Addr> vpn_to_ppn_4k_;
    std::unordered_map<Addr, Addr> vpn_to_ppn_2m_;
    std::unordered_map<Addr, Addr> vpn_to_ppn_1g_;

    // Page allocators (counted in 4 KB units to share the same PA space).
    uint64_t next_free_4k_ppn_;
    uint64_t max_4k_pages_;

    // THP policy.
    uint32_t thp_page_size_kb_;
    uint64_t thp_min_vaddr_;
};

} // namespace minesim
