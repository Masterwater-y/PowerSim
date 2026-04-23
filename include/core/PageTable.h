#pragma once

#include "common/Types.h"
#include "core/Config.h"
#include <unordered_map>

namespace minesim {

class PageTable {
public:
    PageTable(const MemoryConfig& config);

    // Translates a Virtual Page Number (VPN) to a Physical Page Number (PPN)
    // Allocates a new PPN if it doesn't exist (simulating OS page allocation on demand)
    Addr translate(Addr vpn);

    // Helper to get VPN from a full address
    Addr get_vpn(Addr addr) const {
        return addr >> page_shift_;
    }

    // Helper to get offset from a full address
    Addr get_offset(Addr addr) const {
        return addr & page_offset_mask_;
    }

    // Combine PPN and offset to get full physical address
    Addr get_physical_address(Addr ppn, Addr offset) const {
        return (ppn << page_shift_) | offset;
    }

    uint64_t get_allocated_pages() const { return next_free_ppn_; }
    uint64_t get_max_pages() const { return max_pages_; }

private:
    uint32_t page_shift_;
    Addr page_offset_mask_;
    uint64_t max_pages_;

    uint64_t next_free_ppn_;
    std::unordered_map<Addr, Addr> vpn_to_ppn_;
};

} // namespace minesim
