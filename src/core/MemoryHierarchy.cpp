#include "core/MemoryHierarchy.h"
#include <iostream>

namespace minesim {

MemoryHierarchy::MemoryHierarchy(const MicroArchConfig& config) {
    std::cout << "--- Initializing Memory Hierarchy ---\n";
    l1i_ = std::make_unique<Cache>("L1I", config.l1i);
    l1d_ = std::make_unique<Cache>("L1D", config.l1d);
    l2_  = std::make_unique<Cache>("L2", config.l2);
    l3_  = std::make_unique<Cache>("L3", config.l3);

    itlb_ = std::make_unique<TLB>("ITLB", config.itlb);
    dtlb_ = std::make_unique<TLB>("DTLB", config.dtlb);
    stlb_ = std::make_unique<TLB>("STLB", config.stlb);

    page_table_ = std::make_unique<PageTable>(config.mem);
    
    main_memory_latency_ = config.mem.latency;
    page_walk_latency_ = config.mem.page_walk_latency;

    std::cout << "-------------------------------------\n";
}

uint32_t MemoryHierarchy::translate_address(Addr vaddr, Addr& paddr, bool is_instruction, uint64_t current_cycle) {
    uint32_t tlb_latency = 0;
    Addr vpn = page_table_->get_vpn(vaddr);
    Addr offset = page_table_->get_offset(vaddr);
    Addr ppn = 0;

    TLB* l1_tlb = is_instruction ? itlb_.get() : dtlb_.get();
    
    tlb_latency += l1_tlb->get_latency();
    if (l1_tlb->lookup(vpn, ppn, current_cycle)) {
        paddr = page_table_->get_physical_address(ppn, offset);
        return tlb_latency; // L1 TLB hit
    }

    tlb_latency += stlb_->get_latency();
    if (stlb_->lookup(vpn, ppn, current_cycle)) {
        l1_tlb->insert(vpn, ppn, current_cycle); // Fill L1 TLB
        paddr = page_table_->get_physical_address(ppn, offset);
        return tlb_latency; // STLB hit
    }

    // TLB miss, Page walk
    tlb_latency += page_walk_latency_;
    ppn = page_table_->translate(vpn);
    
    stlb_->insert(vpn, ppn, current_cycle);
    l1_tlb->insert(vpn, ppn, current_cycle);
    
    paddr = page_table_->get_physical_address(ppn, offset);
    return tlb_latency;
}

uint32_t MemoryHierarchy::fetch_instruction(Addr vaddr, uint64_t current_cycle) {
    Addr paddr = 0;
    uint32_t total_latency = translate_address(vaddr, paddr, true, current_cycle);

    total_latency += l1i_->get_latency();
    if (l1i_->access(paddr, false, current_cycle)) {
        return total_latency; // L1I Hit
    }

    total_latency += l2_->get_latency();
    if (l2_->access(paddr, false, current_cycle)) {
        return total_latency; // L2 Hit
    }

    total_latency += l3_->get_latency();
    if (l3_->access(paddr, false, current_cycle)) {
        return total_latency; // L3 Hit
    }

    total_latency += main_memory_latency_;
    return total_latency;
}

uint32_t MemoryHierarchy::read_data(Addr vaddr, uint64_t current_cycle) {
    return access_data_hierarchy(vaddr, false, current_cycle);
}

uint32_t MemoryHierarchy::write_data(Addr vaddr, uint64_t current_cycle) {
    return access_data_hierarchy(vaddr, true, current_cycle);
}

uint32_t MemoryHierarchy::access_data_hierarchy(Addr vaddr, bool is_write, uint64_t current_cycle) {
    Addr paddr = 0;
    uint32_t total_latency = translate_address(vaddr, paddr, false, current_cycle);

    total_latency += l1d_->get_latency();
    if (l1d_->access(paddr, is_write, current_cycle)) {
        return total_latency; // L1D Hit
    }

    total_latency += l2_->get_latency();
    if (l2_->access(paddr, is_write, current_cycle)) {
        return total_latency; // L2 Hit
    }

    total_latency += l3_->get_latency();
    if (l3_->access(paddr, is_write, current_cycle)) {
        return total_latency; // L3 Hit
    }

    total_latency += main_memory_latency_;
    return total_latency;
}

void MemoryHierarchy::print_stats() const {
    itlb_->print_stats();
    dtlb_->print_stats();
    stlb_->print_stats();

    l1i_->print_stats();
    l1d_->print_stats();
    l2_->print_stats();
    l3_->print_stats();
    
    std::cout << "\n--- Memory Stats ---\n"
              << "Allocated Pages: " << page_table_->get_allocated_pages() << "\n"
              << "Max Pages: " << page_table_->get_max_pages() << "\n"
              << "--------------------\n";
}

} // namespace minesim
