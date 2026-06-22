#include "core/MemoryHierarchy.h"
#include <iostream>
#include <algorithm>

namespace minesim {

MemoryHierarchy::MemoryHierarchy(const MicroArchConfig& config) {
    std::cout << "--- Initializing Memory Hierarchy ---\n";
    l1i_ = std::make_unique<Cache>("L1I", config.l1i);
    l1d_ = std::make_unique<Cache>("L1D", config.l1d);
    l2_  = std::make_unique<Cache>("L2", config.l2);
    l3_  = std::make_unique<Cache>("L3", config.l3);

    itlb_     = std::make_unique<TLB>("ITLB", config.itlb);
    dtlb_4k_  = std::make_unique<TLB>("DTLB_4K", config.dtlb_4k);
    dtlb_2m_  = std::make_unique<TLB>("DTLB_2M", config.dtlb_2m);
    dtlb_1g_  = std::make_unique<TLB>("DTLB_1G", config.dtlb_1g);
    stlb_     = std::make_unique<TLB>("STLB", config.stlb);

    page_table_ = std::make_unique<PageTable>(config.mem);

    main_memory_latency_ = config.mem.latency;
    page_walk_latency_ = config.mem.page_walk_latency;

    enable_mshr_ = config.experimental.enable_mshr;
    enable_dram_bw_ = config.experimental.enable_dram_bw;
    enable_l2_bw_ = config.experimental.enable_l2_bw;
    enable_l3_bw_ = config.experimental.enable_l3_bw;
    mshr_capacity_ = config.experimental.mshr_capacity > 0 ? config.experimental.mshr_capacity : 1;
    l2_burst_cycles_ = config.experimental.l2_burst_cycles > 0 ? config.experimental.l2_burst_cycles : 1;
    l3_burst_cycles_ = config.experimental.l3_burst_cycles > 0 ? config.experimental.l3_burst_cycles : 1;
    num_dram_channels_ = config.experimental.num_dram_channels > 0 ? config.experimental.num_dram_channels : 1;
    dram_burst_cycles_ = config.experimental.dram_burst_cycles;
    dram_channel_next_free_.assign(num_dram_channels_, 0);

    enable_next_line_prefetcher_ = config.experimental.enable_next_line_prefetcher;
    next_line_prefetch_distance_ = config.experimental.next_line_prefetch_distance;

    std::cout << "-------------------------------------\n";
}

void MemoryHierarchy::reset_stats() {
    l1i_->reset_stats();
    l1d_->reset_stats();
    l2_->reset_stats();
    l3_->reset_stats();
    itlb_->reset_stats();
    dtlb_4k_->reset_stats();
    dtlb_2m_->reset_stats();
    dtlb_1g_->reset_stats();
    stlb_->reset_stats();

    data_page_walks_ = 0;
    data_page_walks_load_ = 0;
    data_page_walks_store_ = 0;
    inst_page_walks_ = 0;
    total_next_line_prefetches_ = 0;
    total_mshr_stall_cycles_ = 0;
    total_mshr_coalesced_ = 0;
    total_mshr_misses_allocated_ = 0;
    total_prefetch_mshr_reserved_ = 0;
    total_prefetch_mshr_dropped_ = 0;
    total_l2_bw_stall_ = 0;
    total_l3_bw_stall_ = 0;
    total_l2_accesses_ = 0;
    total_l3_accesses_ = 0;
    total_dram_bw_stall_ = 0;
    total_dram_accesses_ = 0;
}

void MemoryHierarchy::cleanup_mshr(uint64_t current_cycle) {
    while (!mshr_.empty() && mshr_.front().free_cycle <= current_cycle) {
        mshr_.pop_front();
    }
}

uint64_t MemoryHierarchy::reserve_mshr_fill(Addr line_addr, uint64_t issue_cycle,
                                            uint32_t latency, bool allow_drop,
                                            const LatencyBreakdown& breakdown) {
    cleanup_mshr(issue_cycle);

    for (auto& e : mshr_) {
        if (e.line_addr == line_addr) {
            return e.free_cycle;
        }
    }

    uint64_t blocked = issue_cycle;
    if (mshr_.size() >= mshr_capacity_) {
        if (allow_drop) {
            total_prefetch_mshr_dropped_++;
            return 0;
        }
        blocked = mshr_.front().free_cycle;
        total_mshr_stall_cycles_ += (blocked - issue_cycle);
        cleanup_mshr(blocked);
    }

    uint64_t free_cycle = std::max(issue_cycle, blocked) + latency;
    mshr_.push_back({line_addr, free_cycle, breakdown});
    return free_cycle;
}

void MemoryHierarchy::account_l2_access(uint64_t current_cycle, uint32_t* total_latency) {
    total_l2_accesses_++;
    if (!enable_l2_bw_) {
        return;
    }
    uint64_t arrive = current_cycle + (total_latency ? *total_latency : 0);
    uint64_t serve = std::max(arrive, l2_next_free_);
    uint64_t stall = serve - arrive;
    total_l2_bw_stall_ += stall;
    if (total_latency) {
        *total_latency += static_cast<uint32_t>(stall);
    }
    l2_next_free_ = serve + l2_burst_cycles_;
}

void MemoryHierarchy::account_l3_access(uint64_t current_cycle, uint32_t* total_latency) {
    total_l3_accesses_++;
    if (!enable_l3_bw_) {
        return;
    }
    uint64_t arrive = current_cycle + (total_latency ? *total_latency : 0);
    uint64_t serve = std::max(arrive, l3_next_free_);
    uint64_t stall = serve - arrive;
    total_l3_bw_stall_ += stall;
    if (total_latency) {
        *total_latency += static_cast<uint32_t>(stall);
    }
    l3_next_free_ = serve + l3_burst_cycles_;
}

void MemoryHierarchy::account_dram_access(Addr paddr, uint64_t current_cycle, uint32_t* total_latency) {
    if (enable_dram_bw_ && num_dram_channels_ > 0) {
        uint32_t ch = static_cast<uint32_t>((paddr >> 6) % num_dram_channels_);
        uint64_t arrive = current_cycle + (total_latency ? *total_latency : 0);
        uint64_t serve = std::max(arrive, dram_channel_next_free_[ch]);
        uint64_t bw_stall = serve - arrive;
        total_dram_bw_stall_ += bw_stall;
        total_dram_accesses_++;
        if (total_latency) {
            *total_latency += static_cast<uint32_t>(bw_stall);
        }
        dram_channel_next_free_[ch] = serve + dram_burst_cycles_;
    } else {
        total_dram_accesses_++;
    }

    if (total_latency) {
        *total_latency += main_memory_latency_;
    }
}

void MemoryHierarchy::propagate_l3_writeback(Addr paddr, uint64_t current_cycle) {
    account_dram_access(paddr, current_cycle, nullptr);
}

void MemoryHierarchy::propagate_l2_writeback(Addr paddr, uint64_t current_cycle) {
    account_l3_access(current_cycle, nullptr);
    auto l3_result = l3_->access(paddr, true, current_cycle);
    if (l3_result.evicted_dirty) {
        propagate_l3_writeback(l3_result.evicted_addr, current_cycle);
    }
}

void MemoryHierarchy::propagate_l1d_writeback(Addr paddr, uint64_t current_cycle) {
    account_l2_access(current_cycle, nullptr);
    auto l2_result = l2_->access(paddr, true, current_cycle);
    if (l2_result.evicted_dirty) {
        propagate_l2_writeback(l2_result.evicted_addr, current_cycle);
    }
}

void MemoryHierarchy::prefetch_next_lines_into_l2(Addr paddr, uint64_t current_cycle) {
    if (!enable_next_line_prefetcher_ || next_line_prefetch_distance_ == 0) {
        return;
    }
    constexpr uint64_t LINE = 64ULL;
    Addr base = paddr & ~(LINE - 1);
    for (uint32_t i = 1; i <= next_line_prefetch_distance_; ++i) {
        Addr nxt = base + i * LINE;
        uint64_t pref_issue = current_cycle + i;
        cleanup_mshr(pref_issue);
        uint32_t pref_lat = l2_->get_latency() + l3_->get_latency() + main_memory_latency_;
        uint64_t pref_free = reserve_mshr_fill(nxt, pref_issue, pref_lat, /*allow_drop=*/true);
        if (pref_free == 0) {
            continue;
        }
        total_prefetch_mshr_reserved_++;

        // Install into L2 (silent: no demand counters touched). On capacity
        // miss we must propagate the evicted dirty line to L3/DRAM.
        account_l2_access(pref_issue, nullptr);
        auto r2 = l2_->prefetch_install(nxt, current_cycle);
        if (r2.evicted_dirty) {
            propagate_l2_writeback(r2.evicted_addr, current_cycle);
        }
        // Also pre-install in L3 so a later L2 eviction doesn't cause a
        // demand LLC miss on the same line.
        account_l3_access(pref_issue, nullptr);
        auto r3 = l3_->prefetch_install(nxt, current_cycle);
        if (r3.evicted_dirty) {
            propagate_l3_writeback(r3.evicted_addr, current_cycle);
        }
        total_next_line_prefetches_++;
    }
}

uint32_t MemoryHierarchy::translate_address(Addr vaddr, Addr& paddr, bool is_instruction, bool is_write, uint64_t current_cycle) {
    // Pick the appropriate page size.
    PageSize sz = is_instruction ? PageSize::SIZE_4K : page_table_->page_size_for(vaddr);
    Addr vpn = page_table_->get_vpn(vaddr, sz);
    Addr offset = page_table_->get_offset(vaddr, sz);

    // Pick the matching first-level TLB.
    TLB* l1_tlb = nullptr;
    if (is_instruction) {
        l1_tlb = itlb_.get();
    } else {
        switch (sz) {
            case PageSize::SIZE_4K: l1_tlb = dtlb_4k_.get(); break;
            case PageSize::SIZE_2M: l1_tlb = dtlb_2m_.get(); break;
            case PageSize::SIZE_1G: l1_tlb = dtlb_1g_.get(); break;
        }
    }

    uint32_t tlb_latency = l1_tlb->get_latency();
    Addr ppn = 0;
    if (l1_tlb->lookup(vpn, ppn, current_cycle)) {
        paddr = page_table_->get_physical_address(ppn, offset, sz);
        return tlb_latency;
    }

    // 1G pages on Intel skip the STLB and go straight to a page walk.
    if (sz != PageSize::SIZE_1G) {
        tlb_latency += stlb_->get_latency();
        if (stlb_->lookup(vpn, ppn, current_cycle)) {
            l1_tlb->insert(vpn, ppn, current_cycle);
            paddr = page_table_->get_physical_address(ppn, offset, sz);
            return tlb_latency;
        }
    }

    // STLB miss -> page walk (this is the event that perf reports as
    // dTLB-*-misses / iTLB-load-misses on Intel: WALK_COMPLETED).
    tlb_latency += page_walk_latency_;
    if (is_instruction) {
        inst_page_walks_++;
    } else {
        data_page_walks_++;
        if (is_write) data_page_walks_store_++;
        else          data_page_walks_load_++;
    }
    ppn = page_table_->translate(vaddr, sz);

    if (sz != PageSize::SIZE_1G) {
        stlb_->insert(vpn, ppn, current_cycle);
    }
    l1_tlb->insert(vpn, ppn, current_cycle);

    paddr = page_table_->get_physical_address(ppn, offset, sz);
    return tlb_latency;
}

uint32_t MemoryHierarchy::fetch_instruction(Addr vaddr, uint64_t current_cycle) {
    Addr paddr = 0;
    uint32_t total_latency = translate_address(vaddr, paddr, true, false, current_cycle);

    total_latency += l1i_->get_latency();
    auto l1i_result = l1i_->access(paddr, false, current_cycle);
    if (l1i_result.hit) return total_latency;

    account_l2_access(current_cycle, &total_latency);
    total_latency += l2_->get_latency();
    auto l2_result = l2_->access(paddr, false, current_cycle);
    if (l2_result.evicted_dirty) {
        propagate_l2_writeback(l2_result.evicted_addr, current_cycle);
    }
    if (l2_result.hit) return total_latency;

    account_l3_access(current_cycle, &total_latency);
    total_latency += l3_->get_latency();
    auto l3_result = l3_->access(paddr, false, current_cycle);
    if (l3_result.evicted_dirty) {
        propagate_l3_writeback(l3_result.evicted_addr, current_cycle);
    }
    if (l3_result.hit) return total_latency;

    account_dram_access(paddr, current_cycle, &total_latency);
    return total_latency;
}

uint32_t MemoryHierarchy::access_data_unit_with_dram(Addr vaddr, uint32_t size, bool is_write,
                                                    uint64_t current_cycle,
                                                    bool& l1d_hit, bool& went_to_dram,
                                                    LatencyBreakdown* breakdown) {
    (void)size;
    l1d_hit = false;
    went_to_dram = false;
    if (breakdown) *breakdown = LatencyBreakdown{};
    Addr paddr = 0;
    uint32_t total_latency = translate_address(vaddr, paddr, false, is_write, current_cycle);
    if (breakdown) breakdown->tlb_latency = total_latency;

    uint32_t l1_lat = l1d_->get_latency();
    total_latency += l1_lat;
    if (breakdown) breakdown->l1_latency = l1_lat;
    auto l1d_result = l1d_->access(paddr, is_write, current_cycle);
    if (l1d_result.evicted_dirty) {
        propagate_l1d_writeback(l1d_result.evicted_addr, current_cycle);
    }
    if (l1d_result.hit) {
        l1d_hit = true;
        return total_latency;
    }

    uint32_t before_l2 = total_latency;
    account_l2_access(current_cycle, &total_latency);
    uint32_t l2_bw = total_latency - before_l2;
    if (breakdown) breakdown->l2_bw_stall = l2_bw;
    uint32_t l2_lat = l2_->get_latency();
    total_latency += l2_lat;
    if (breakdown) breakdown->l2_latency = l2_lat;
    auto l2_result = l2_->access(paddr, is_write, current_cycle);
    if (l2_result.evicted_dirty) {
        propagate_l2_writeback(l2_result.evicted_addr, current_cycle);
    }
    if (l2_result.hit) {
        prefetch_next_lines_into_l2(paddr, current_cycle);
        return total_latency;
    }

    uint32_t before_l3 = total_latency;
    account_l3_access(current_cycle, &total_latency);
    uint32_t l3_bw = total_latency - before_l3;
    if (breakdown) breakdown->l3_bw_stall = l3_bw;
    uint32_t l3_lat = l3_->get_latency();
    total_latency += l3_lat;
    if (breakdown) breakdown->l3_latency = l3_lat;
    auto l3_result = l3_->access(paddr, is_write, current_cycle);
    if (l3_result.evicted_dirty) {
        propagate_l3_writeback(l3_result.evicted_addr, current_cycle);
    }
    if (l3_result.hit) {
        prefetch_next_lines_into_l2(paddr, current_cycle);
        return total_latency;
    }

    // L3 miss -> DRAM
    went_to_dram = true;
    uint32_t before_dram = total_latency;
    account_dram_access(paddr, current_cycle, &total_latency);
    uint32_t dram_bw = total_latency - before_dram - main_memory_latency_;
    if (breakdown) {
        breakdown->dram_latency = main_memory_latency_;
        breakdown->dram_bw_stall = dram_bw;
    }

    prefetch_next_lines_into_l2(paddr, current_cycle);
    return total_latency;
}

uint32_t MemoryHierarchy::access_data_unit(Addr vaddr, uint32_t size, bool is_write, uint64_t current_cycle) {
    bool l1d_hit = false;
    bool went_to_dram = false;
    return access_data_unit_with_dram(vaddr, size, is_write, current_cycle, l1d_hit, went_to_dram);
}

uint32_t MemoryHierarchy::read_data(Addr vaddr, uint32_t size, uint64_t current_cycle) {
    if (size == 0) size = 1;

    constexpr uint64_t LINE = 64ULL;
    Addr first_line = vaddr & ~(LINE - 1);
    Addr last_line  = (vaddr + size - 1) & ~(LINE - 1);

    if (first_line == last_line) {
        return access_data_unit(vaddr, size, false, current_cycle);
    }
    // Cache-line straddling (and possibly page crossing): both halves are
    // independent L1D / DTLB accesses, but they share the same load port and
    // must execute serially on x86 (no true parallel split-load issue), so
    // latencies accumulate.
    uint32_t lat1 = access_data_unit(vaddr, LINE - (vaddr & (LINE - 1)), false, current_cycle);
    Addr second_addr = first_line + LINE;
    uint32_t lat2 = access_data_unit(second_addr, (vaddr + size) - second_addr, false, current_cycle + lat1);
    return lat1 + lat2;
}

uint32_t MemoryHierarchy::write_data(Addr vaddr, uint32_t size, uint64_t current_cycle) {
    if (size == 0) size = 1;

    constexpr uint64_t LINE = 64ULL;
    Addr first_line = vaddr & ~(LINE - 1);
    Addr last_line  = (vaddr + size - 1) & ~(LINE - 1);

    if (first_line == last_line) {
        return access_data_unit(vaddr, size, true, current_cycle);
    }
    uint32_t lat1 = access_data_unit(vaddr, LINE - (vaddr & (LINE - 1)), true, current_cycle);
    Addr second_addr = first_line + LINE;
    uint32_t lat2 = access_data_unit(second_addr, (vaddr + size) - second_addr, true, current_cycle + lat1);
    return lat1 + lat2;
}

LoadAccessResult MemoryHierarchy::read_data_mshr(Addr vaddr, uint32_t size, uint64_t current_cycle) {
    LoadAccessResult res{};
    res.blocked_until = current_cycle;

    if (!enable_mshr_) {
        res.latency = read_data(vaddr, size, current_cycle);
        return res;
    }

    if (size == 0) size = 1;
    constexpr uint64_t LINE = 64ULL;

    // Garbage-collect MSHR entries whose miss has resolved by now.
    cleanup_mshr(current_cycle);

    // Process each cache line touched by this access (1 or 2). For each line:
    // - L1D hit: latency from access_data_unit, no MSHR slot.
    // - In-flight MSHR for same line: coalesce, latency = remaining wait.
    // - Otherwise miss: allocate slot. If pool full, stall issue to oldest
    //   free_cycle. New entry's free_cycle = max(current, blocked) + latency.
    auto handle_line = [&](Addr line_vaddr, Addr access_vaddr, uint32_t access_size,
                           uint64_t issue_cycle, LatencyBreakdown& out_bd) -> uint32_t {
        bool l1d_hit = false;
        bool went_to_dram = false;
        LatencyBreakdown bd;
        uint32_t lat = access_data_unit_with_dram(access_vaddr, access_size, false,
                                                  issue_cycle, l1d_hit, went_to_dram, &bd);
        if (l1d_hit) {
            out_bd = bd;
            return lat;
        }

        // Coalesce with an in-flight MSHR for the same line.
        for (auto& e : mshr_) {
            if (e.line_addr == line_vaddr) {
                total_mshr_coalesced_++;
                uint64_t remaining = (e.free_cycle > issue_cycle) ?
                                     (e.free_cycle - issue_cycle) : 0;
                out_bd = e.breakdown;
                return static_cast<uint32_t>(std::max<uint64_t>(remaining, l1d_->get_latency()));
            }
        }

        // Need a new MSHR slot. If pool full, stall issue.
        uint64_t free_cycle = reserve_mshr_fill(line_vaddr, issue_cycle, lat,
                                                /*allow_drop=*/false, bd);
        if (free_cycle > issue_cycle + lat) {
            res.blocked_until = std::max(res.blocked_until, free_cycle - lat);
            bd.mshr_stall = static_cast<uint32_t>(free_cycle - (issue_cycle + lat));
        }
        total_mshr_misses_allocated_++;
        out_bd = bd;
        return lat;
    };

    Addr first_line = vaddr & ~(LINE - 1);
    Addr last_line  = (vaddr + size - 1) & ~(LINE - 1);
    if (first_line == last_line) {
        LatencyBreakdown bd;
        res.latency = handle_line(first_line, vaddr, size, current_cycle, bd);
        res.breakdown = bd;
        return res;
    }
    uint32_t sz1 = static_cast<uint32_t>(LINE - (vaddr & (LINE - 1)));
    LatencyBreakdown bd1, bd2;
    uint32_t lat1 = handle_line(first_line, vaddr, sz1, current_cycle, bd1);
    Addr second_addr = first_line + LINE;
    uint32_t sz2 = static_cast<uint32_t>((vaddr + size) - second_addr);
    uint32_t lat2 = handle_line(second_addr, second_addr, sz2, current_cycle + lat1, bd2);
    res.latency = lat1 + lat2;
    // Accumulate both halves' breakdowns.
    res.breakdown.l1_latency   = bd1.l1_latency   + bd2.l1_latency;
    res.breakdown.l2_latency   = bd1.l2_latency   + bd2.l2_latency;
    res.breakdown.l3_latency   = bd1.l3_latency   + bd2.l3_latency;
    res.breakdown.dram_latency = bd1.dram_latency + bd2.dram_latency;
    res.breakdown.l2_bw_stall  = bd1.l2_bw_stall  + bd2.l2_bw_stall;
    res.breakdown.l3_bw_stall  = bd1.l3_bw_stall  + bd2.l3_bw_stall;
    res.breakdown.dram_bw_stall = bd1.dram_bw_stall + bd2.dram_bw_stall;
    res.breakdown.mshr_stall   = bd1.mshr_stall   + bd2.mshr_stall;
    res.breakdown.tlb_latency  = bd1.tlb_latency  + bd2.tlb_latency;
    return res;
}

void MemoryHierarchy::print_stats() const {
    itlb_->print_stats();
    dtlb_4k_->print_stats();
    dtlb_2m_->print_stats();
    dtlb_1g_->print_stats();
    stlb_->print_stats();

    l1i_->print_stats();
    l1d_->print_stats();
    l2_->print_stats();
    l3_->print_stats();

    std::cout << "\n--- Page Walk Stats (perf-aligned) ---\n"
              << "Data Page Walks (= dTLB-(load+store)-misses): " << data_page_walks_ << "\n"
              << "  Load Walks  (= dTLB-load-misses):           " << data_page_walks_load_ << "\n"
              << "  Store Walks (= dTLB-store-misses):          " << data_page_walks_store_ << "\n"
              << "Inst Page Walks (= iTLB-load-misses):         " << inst_page_walks_ << "\n"
              << "--------------------------------------\n";

    std::cout << "\n--- Memory Stats ---\n"
              << "Allocated 4K-equiv Pages: " << page_table_->get_allocated_pages() << "\n"
              << "Max 4K Pages: " << page_table_->get_max_pages() << "\n"
              << "--------------------\n";

    std::cout << "\n--- Backend Memory Throttling ---\n"
              << "MSHR Misses Allocated: " << total_mshr_misses_allocated_ << "\n"
              << "MSHR Coalesced:        " << total_mshr_coalesced_ << "\n"
              << "MSHR Stall Cycles:     " << total_mshr_stall_cycles_ << "\n"
              << "Prefetch MSHR Reserved:" << total_prefetch_mshr_reserved_ << "\n"
              << "Prefetch MSHR Dropped: " << total_prefetch_mshr_dropped_ << "\n"
              << "L2 Accesses:           " << total_l2_accesses_ << "\n"
              << "L2 BW Stall Cycles:    " << total_l2_bw_stall_
              << " (enabled=" << (enable_l2_bw_ ? "yes" : "no")
              << ", burst=" << l2_burst_cycles_ << ")\n"
              << "L3 Accesses:           " << total_l3_accesses_ << "\n"
              << "L3 BW Stall Cycles:    " << total_l3_bw_stall_
              << " (enabled=" << (enable_l3_bw_ ? "yes" : "no")
              << ", burst=" << l3_burst_cycles_ << ")\n"
              << "DRAM Accesses:         " << total_dram_accesses_ << "\n"
              << "DRAM BW Stall Cycles:  " << total_dram_bw_stall_ << "\n"
              << "Next-Line Prefetches:  " << total_next_line_prefetches_
              << " (enabled=" << (enable_next_line_prefetcher_ ? "yes" : "no")
              << ", distance=" << next_line_prefetch_distance_ << ")\n"
              << "---------------------------------\n";
}

} // namespace minesim
