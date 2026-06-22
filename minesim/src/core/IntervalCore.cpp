#include "core/IntervalCore.h"
#include <iostream>
#include <iomanip>
#include <algorithm>

namespace minesim {

static uint64_t regs_ready_cycle(const std::unordered_map<uint16_t, uint64_t>& rat,
                                 const std::vector<uint16_t>& regs,
                                 uint64_t base_cycle) {
    uint64_t ready_cycle = base_cycle;
    for (uint16_t reg : regs) {
        auto it = rat.find(reg);
        if (it != rat.end()) {
            ready_cycle = std::max(ready_cycle, it->second);
        }
    }
    return ready_cycle;
}

static uint64_t account_interval_against_tail(uint64_t start_cycle, uint64_t end_cycle,
                                              uint64_t tail_cycle,
                                              uint64_t& visible_cycles,
                                              uint64_t& hidden_cycles) {
    if (end_cycle <= start_cycle) {
        return 0;
    }
    uint64_t raw = end_cycle - start_cycle;
    uint64_t visible = 0;
    if (end_cycle > tail_cycle) {
        visible = end_cycle - std::max(start_cycle, tail_cycle);
    }
    visible_cycles += visible;
    hidden_cycles += raw - visible;
    return visible;
}

template <typename IntervalDeque>
static uint64_t active_memory_tail(const IntervalDeque& intervals, uint64_t cycle) {
    uint64_t tail = 0;
    for (const auto& interval : intervals) {
        if (interval.start_cycle <= cycle && interval.end_cycle > cycle) {
            tail = std::max(tail, interval.end_cycle);
        }
    }
    return tail;
}

// Window-based overlap check: returns the max end_cycle of any memory interval
// that overlaps with [window_start, window_end). Used for branch recovery
// overlap where the penalty spans a window, not a single point.
template <typename IntervalDeque>
static uint64_t active_memory_tail_window(const IntervalDeque& intervals,
                                          uint64_t window_start, uint64_t window_end) {
    uint64_t tail = 0;
    for (const auto& interval : intervals) {
        if (interval.start_cycle < window_end && interval.end_cycle > window_start) {
            tail = std::max(tail, interval.end_cycle);
        }
    }
    return tail;
}

IntervalCore::IntervalCore(const MicroArchConfig& config, MemoryHierarchy* mem)
    : config_(config), mem_(mem) {
    branch_predictor_ = BranchPredictor::create(config);
}

void IntervalCore::step(const Instruction& inst, InstDecoder* decoder) {
    total_macro_insts_++;

    // Front-end pipeline depth (fetch -> decode -> rename -> dispatch).
    // SPR-class cores have a ~5-stage in-order front-end before the issue
    // window. We previously modeled it as just `fetch -> fetch+1 -> +1`,
    // i.e. 2 stages, which made every branch flush ~3 cycles cheaper than
    // the real machine and inflated steady-state IPC. The penalty is on
    // the *flush* path: after a mispredict, you eat the full pipeline
    // depth, not just `branch_mispredict_penalty`.
    //
    // We keep `branch_mispredict_penalty` as the configurable mispredict
    // resolution cost (defaults to 18 in cfg) and add this fixed pipeline
    // depth on top, so the effective taken-mispredict cost becomes ~23
    // cycles, matching INT_MISC.RECOVERY_CYCLES on SPR.
    constexpr uint32_t kFrontEndPipelineDepth = 5;

    // 1. Fetch Stage
    if (fetch_count_ >= config_.fetch_width) {
        last_fetch_cycle_++;
        fetch_count_ = 0;
    }
    
    // Simulate I-Cache/ITLB access. Real CPUs (and perf events) count one
    // L1I/ITLB lookup per 64-byte fetch block, not per macro-instruction, so
    // only consult the memory hierarchy when the fetch block changes.
    Addr fetch_block = inst.pc >> 6;
    if (fetch_block != last_fetched_block_paddr_) {
        uint32_t fetch_latency = mem_->fetch_instruction(inst.pc, last_fetch_cycle_);
        // The hit baseline is ITLB + L1I; anything beyond is an
        // I-Cache or ITLB miss penalty exposed to the front-end.
        uint32_t baseline_fetch = config_.itlb.latency + config_.l1i.latency;
        if (fetch_latency > baseline_fetch) {
            uint32_t penalty = fetch_latency - baseline_fetch;
            last_fetch_cycle_ += penalty;
            total_icache_miss_penalties_ += penalty;
        }
        last_fetched_block_paddr_ = fetch_block;
    }
    uint64_t fetch_cycle = last_fetch_cycle_;
    fetch_count_++;

    // 2. Decode Stage (collapsed: rename / dispatch happen at +pipeline_depth)
    uint64_t decode_cycle = std::max(fetch_cycle + 1, last_decode_cycle_);
    if (decode_count_ >= config_.decode_width) {
        decode_cycle++;
        decode_count_ = 0;
    }
    last_decode_cycle_ = decode_cycle;
    decode_count_++;

    // Decode to uops
    std::vector<MicroOp> uops = decoder->decode(inst);

    // Branch Prediction:
    //   - conditional branch        -> direction predictor + taken-target BTB
    //   - indirect jump / call      -> BTB (target validation)
    //   - return                    -> RSB
    //   - direct call               -> BTB + push RSB
    //   - unconditional direct jump -> BTB
    //
    // MineSim historically only modeled target prediction for indirects and
    // returns, which silently treated direct calls/jumps and taken
    // conditionals as perfect once the direction was correct. On real cores,
    // all taken branches consume BTB target prediction, so a missing/stale BTB
    // entry should count as a branch miss even when the direction predictor is
    // right. Reuse the existing BTB-backed target validator here until the
    // predictor API is split into explicit direct-vs-indirect target stats.
    bool mispredicted = false;
    bool direct_target_proxy_miss = false;
    auto validate_direct_target = [&]() -> bool {
        return inst.next_pc != 0 && branch_predictor_->predict_direct(inst.pc, inst.next_pc);
    };
    auto validate_indirect_target = [&]() -> bool {
        return inst.next_pc != 0 && branch_predictor_->predict_indirect(inst.pc, inst.next_pc);
    };
    if (inst.is_conditional_branch) {
        branch_total_cond_++;
        if (branch_predictor_->is_perfect()) {
            // Perfect predictor: no mispredict penalty, just update stats.
            branch_predictor_->update(inst.pc, inst.is_branch_taken);
        } else {
            bool predicted_taken = branch_predictor_->predict(inst.pc);
            if (predicted_taken != inst.is_branch_taken) {
                mispredicted = true;
                branch_miss_cond_++;
            } else if (inst.is_branch_taken) {
                // Direction hit on a taken conditional still needs a BTB target hit.
                if (!validate_direct_target()) {
                    direct_target_proxy_miss = true;
                }
            }
            branch_predictor_->update(inst.pc, inst.is_branch_taken);
        }
    } else if (inst.is_return) {
        branch_total_return_++;
        if (!branch_predictor_->is_perfect()) {
            bool ok = branch_predictor_->predict_return(inst.next_pc);
            if (!ok) { mispredicted = true; branch_miss_return_++; }
        }
    } else if (inst.is_indirect_branch) {
        branch_total_indirect_++;
        if (!branch_predictor_->is_perfect()) {
            bool ok = validate_indirect_target();
            if (!ok) { mispredicted = true; branch_miss_indirect_++; }
        }
        if (inst.is_call) {
            branch_predictor_->push_call(inst.pc + inst.size);
        }
    } else if (inst.is_call) {
        branch_total_direct_call_++;
        // Direct calls have a statically encoded target and tend to be covered
        // by decode/uop-cache redirect paths. Counting every cold BTB fill as
        // a branch miss materially overshoots perf on analytics_st.
        branch_predictor_->push_call(inst.pc + inst.size);
    } else if (inst.is_unconditional_direct_branch) {
        branch_total_direct_jump_++;
        // Likewise, unconditional direct jumps do not contribute meaningfully
        // to PERF_COUNT_HW_BRANCH_MISSES on this workload, so keep them off
        // the target-miss path until MineSim has a richer frontend model.
    }
    if (mispredicted) {
        total_raw_branch_mispredicts_++;
        total_branch_mispredicts_++;
    }
    if (direct_target_proxy_miss) {
        total_raw_branch_mispredicts_++;
        total_direct_target_proxy_misses_++;
        branch_miss_direct_target_++;
        direct_target_visibility_accum_ +=
            config_.experimental.direct_target_miss_visibility_pct;
        if (direct_target_visibility_accum_ >= 100) {
            direct_target_visibility_accum_ -= 100;
            total_branch_mispredicts_++;
            total_direct_target_visible_misses_++;
            mispredicted = true;
        }
    }

    // 3. Process Uops
    for (const auto& uop : uops) {
        total_uops_++;
        
        // a. Dispatch Stage
        // Add the remaining front-end pipeline depth between decode and
        // dispatch (rename + alloc + dispatch). The previous model was
        // `decode + 1` which is only one stage and substantially underrun
        // the real CPU's in-order front-end length.
        uint64_t dispatch_floor =
            last_decode_cycle_ + (kFrontEndPipelineDepth - 1);
        uint64_t dispatch_cycle = std::max(dispatch_floor, last_dispatch_cycle_);
        if (dispatch_count_ >= config_.dispatch_width) {
            dispatch_cycle++;
            dispatch_count_ = 0;
        }

        // Serialization Hazard (Drain ROB)
        // If the instruction is serializing, it must wait until all older instructions have retired.
        if (uop.is_serializing && !rob_retire_cycles_.empty()) {
            uint64_t newest_retire = rob_retire_cycles_.back(); // The youngest instruction in ROB
            if (newest_retire > dispatch_cycle) {
                stall_serialize_ += (newest_retire - dispatch_cycle);
                dispatch_cycle = newest_retire;
                dispatch_count_ = 0;
            }
        }
        
        // Structural Hazard: ROB Full
        if (rob_retire_cycles_.size() >= config_.rob_size) {
            uint64_t oldest_retire = rob_retire_cycles_.front();
            if (oldest_retire > dispatch_cycle) {
                stall_rob_full_ += (oldest_retire - dispatch_cycle);
                dispatch_cycle = oldest_retire;
                dispatch_count_ = 0; // Reset count since we stalled to a new cycle
            }
        }

        // Clean up SQ and LQ of entries that have already retired BEFORE or AT this dispatch_cycle.
        // Because they no longer occupy SQ/LQ at the time this instruction is dispatched.
        while (!sq_.empty() && sq_.front().free_cycle <= dispatch_cycle) {
            sq_.pop_front();
        }
        while (!lq_.empty() && lq_.front().retire_cycle <= dispatch_cycle) {
            lq_.pop_front();
        }
        
        // Clean up IQ (Reservation Station) of entries that have already ISSUED BEFORE or AT this dispatch_cycle.
        // Because they no longer occupy the RS once they are sent to the execution units.
        while (!iq_.empty() && iq_.front().issue_cycle <= dispatch_cycle) {
            iq_.pop_front();
        }

        // Structural Hazard: SQ Full
        if (uop.type == InstType::STORE && sq_.size() >= config_.sq_size) {
            uint64_t oldest_sq_free = sq_.front().free_cycle;
            if (oldest_sq_free > dispatch_cycle) {
                uint64_t stall = oldest_sq_free - dispatch_cycle;
                stall_sq_full_ += stall;
                total_sq_drain_visible_cycles_ += stall;
                dispatch_cycle = oldest_sq_free;
                dispatch_count_ = 0;
            }
        }

        // Structural Hazard: LQ Full
        if (uop.type == InstType::LOAD && lq_.size() >= config_.lq_size) {
            uint64_t oldest_lq_retire = lq_.front().retire_cycle;
            if (oldest_lq_retire > dispatch_cycle) {
                stall_lq_full_ += (oldest_lq_retire - dispatch_cycle);
                dispatch_cycle = oldest_lq_retire;
                dispatch_count_ = 0;
            }
        }

        // Structural Hazard: IQ / RS Full
        // The IQ size limits how many un-issued instructions can wait in the reservation station.
        if (iq_.size() >= config_.iq_size) {
            uint64_t oldest_iq_issue = iq_.front().issue_cycle;
            if (oldest_iq_issue > dispatch_cycle) {
                stall_iq_full_ += (oldest_iq_issue - dispatch_cycle);
                dispatch_cycle = oldest_iq_issue;
                dispatch_count_ = 0;
            }
        }

        // Front-end back-pressure: when dispatch stalls (ROB/IQ/LQ/SQ full
        // or serialize drain), the fetch/decode stages cannot keep advancing
        // past the dispatch window. Without this, last_fetch_cycle_ runs
        // ahead freely and the next macro-instruction skips the entire
        // back-end stall, which is the dominant source of MineSim's
        // optimistic IPC. Clamp fetch/decode to (dispatch - pipeline_depth)
        // so the front end is always at most one pipeline-depth ahead of
        // dispatch.
        if (dispatch_cycle > kFrontEndPipelineDepth) {
            uint64_t fetch_ceiling = dispatch_cycle - kFrontEndPipelineDepth;
            if (last_fetch_cycle_ < fetch_ceiling) {
                last_fetch_cycle_ = fetch_ceiling;
                fetch_count_ = 0;
            }
            uint64_t decode_ceiling =
                dispatch_cycle - (kFrontEndPipelineDepth - 1);
            if (last_decode_cycle_ < decode_ceiling) {
                last_decode_cycle_ = decode_ceiling;
                decode_count_ = 0;
            }
        }

        last_dispatch_cycle_ = dispatch_cycle;
        dispatch_count_++;
        
        // b. Issue Stage (Execution)
        
        // 1. Data Dependency Check (Wait for Source Operands)
        uint64_t op_ready_cycle = regs_ready_cycle(rat_, uop.src_regs, dispatch_cycle);
        uint64_t store_data_ready_cycle = op_ready_cycle;
        if (uop.type == InstType::STORE &&
            (!uop.store_addr_regs.empty() || !uop.store_data_regs.empty())) {
            op_ready_cycle = regs_ready_cycle(rat_, uop.store_addr_regs, dispatch_cycle);
            store_data_ready_cycle =
                regs_ready_cycle(rat_, uop.store_data_regs, dispatch_cycle);
        }
        if (op_ready_cycle > dispatch_cycle) {
            stall_raw_dep_ += (op_ready_cycle - dispatch_cycle);
            if (config_.experimental.enable_mcw_stats) {
                // Clean up memory intervals that ended before the stall window.
                while (!mcw_memory_intervals_.empty() &&
                       mcw_memory_intervals_.front().end_cycle <= dispatch_cycle) {
                    mcw_memory_intervals_.pop_front();
                }
                uint64_t coverage_tail =
                    std::max(mcw_critical_tail_cycle_,
                             active_memory_tail(mcw_memory_intervals_, dispatch_cycle));
                uint64_t visible_dep = account_interval_against_tail(
                    dispatch_cycle, op_ready_cycle, coverage_tail,
                    mcw_visible_dependency_cycles_,
                    mcw_hidden_dependency_cycles_);
                if (visible_dep > 0) {
                    mcw_critical_tail_cycle_ =
                        std::max(mcw_critical_tail_cycle_, op_ready_cycle);
                }
                // Observation: is this dep stall overlapping with active memory?
                bool has_active_memory = false;
                for (const auto& interval : mcw_memory_intervals_) {
                    if (interval.start_cycle <= dispatch_cycle &&
                        interval.end_cycle > dispatch_cycle) {
                        has_active_memory = true;
                        break;
                    }
                }
                if (has_active_memory) {
                    mcw_dep_stall_with_memory_overlap_ +=
                        (op_ready_cycle - dispatch_cycle);
                } else {
                    mcw_dep_stall_no_memory_overlap_ +=
                        (op_ready_cycle - dispatch_cycle);
                }
            }
        }

        // 1.5. Store-to-Load Forwarding (STLF) Check
        bool is_stlf_hit = false;
        if (uop.type == InstType::LOAD) {
            // Search SQ backwards (most recent stores first)
            for (auto it = sq_.rbegin(); it != sq_.rend(); ++it) {
                // Check if there is an address overlap
                if (it->addr < uop.mem_addr + uop.mem_size && it->addr + it->size > uop.mem_addr) {
                    // Check if the store data will be ready before or when the load issues
                    // Or if the load has to wait for the store
                    if (it->addr <= uop.mem_addr && (it->addr + it->size) >= (uop.mem_addr + uop.mem_size)) {
                        // Full overlap: STLF Hit
                        // Forward once the older store's data is available in SQ.
                        op_ready_cycle = std::max(op_ready_cycle, it->data_ready_cycle);
                        is_stlf_hit = true;
                    } else {
                        // Partial overlap: STLF stall (LD_BLOCKS.STORE_FORWARD).
                        // Real x86 cannot forward partial overlaps; the load
                        // must wait for the store to drain into L1D and then
                        // re-issue, costing an extra ~11 cycles on SPR.
                        uint64_t partial_ready = std::max(it->retire_cycle, it->data_ready_cycle);
                        if (config_.experimental.enable_partial_stlf) {
                            partial_ready += config_.experimental.partial_stlf_penalty;
                            total_partial_stlf_count_++;
                            total_partial_stlf_stall_ +=
                                config_.experimental.partial_stlf_penalty;
                        }
                        op_ready_cycle = std::max(op_ready_cycle, partial_ready);
                    }
                    break; // Only care about the most recent overlapping store
                }
            }
        }
        
        // 2. Structural Hazard Check (Wait for Issue Port/Bandwidth)
        uint64_t issue_cycle = op_ready_cycle;
        if (!uop.is_rename_only) {
            issue_cycle = get_next_issue_cycle(op_ready_cycle, uop.type);
            if (issue_cycle > op_ready_cycle) {
                stall_port_busy_ += (issue_cycle - op_ready_cycle);
            }
        }

        // Dep stall classification (observation-only).
        if (op_ready_cycle > dispatch_cycle) {
            uint64_t dep_len = op_ready_cycle - dispatch_cycle;
            dep_interval_total_cycles_ += dep_len;

            // If issue_cycle > op_ready_cycle, the port was busy at op_ready_cycle,
            // meaning the dep stall was at least partially hidden behind port pressure.
            // This is a conservative classification: "apparent" means the instruction
            // could not have issued at dispatch_cycle even if operands were ready.
            if (issue_cycle > op_ready_cycle) {
                dep_apparent_cycles_ += dep_len;
            } else {
                dep_true_blocking_cycles_ += dep_len;
            }

            // Interval-level memory overlap: count cycles in [dispatch, op_ready)
            // that overlap with any active memory interval.
            for (const auto& interval : mcw_memory_intervals_) {
                if (interval.end_cycle > dispatch_cycle &&
                    interval.start_cycle < op_ready_cycle) {
                    uint64_t overlap_start =
                        std::max(dispatch_cycle, interval.start_cycle);
                    uint64_t overlap_end =
                        std::min(op_ready_cycle, interval.end_cycle);
                    if (overlap_end > overlap_start) {
                        dep_interval_memory_overlap_cycles_ +=
                            (overlap_end - overlap_start);
                    }
                }
            }
        }
        
        // Calculate Uop Execution Latency
        uint32_t exec_latency = uop.is_rename_only ? 0 : 1; // Default ALU/Branch
        if (uop.type == InstType::LOAD) {
            if (is_stlf_hit) {
                exec_latency = 1; // Data forwarded from SQ
            } else {
                LoadAccessResult res =
                    mem_->read_data_mshr(uop.mem_addr, uop.mem_size, issue_cycle);
                if (res.blocked_until > issue_cycle) {
                    // MSHR/LFB pool exhausted -> stall this load.
                    total_mshr_issue_stall_ += (res.blocked_until - issue_cycle);
                    issue_cycle = res.blocked_until;
                }
                exec_latency = res.latency;
                if (exec_latency > config_.l1d.latency) {
                    total_dcache_miss_penalties_ += (exec_latency - config_.l1d.latency);
                    if (config_.experimental.enable_mcw_stats) {
                        uint64_t miss_start = issue_cycle + config_.l1d.latency;
                        uint64_t miss_end = issue_cycle + exec_latency;
                        while (!mcw_memory_intervals_.empty() &&
                               mcw_memory_intervals_.front().end_cycle <= miss_start) {
                            mcw_memory_intervals_.pop_front();
                        }
                        uint64_t coverage_tail =
                            std::max(mcw_critical_tail_cycle_,
                                     active_memory_tail(mcw_memory_intervals_, miss_start));
                        uint64_t visible_load = account_interval_against_tail(
                            miss_start, miss_end, coverage_tail,
                            mcw_visible_load_miss_cycles_,
                            mcw_hidden_load_miss_cycles_);
                        if (visible_load > 0) {
                            mcw_critical_tail_cycle_ =
                                std::max(mcw_critical_tail_cycle_, miss_end);
                            // Proportionally attribute visible portion to
                            // each sub-component of the memory breakdown.
                            // MSHR stall is applied before miss_start
                            // (it bumps issue_cycle), so it is NOT part of
                            // miss_duration. Tracked separately as
                            // total_mshr_issue_stall_.
                            uint64_t miss_duration = miss_end - miss_start;
                            if (miss_duration > 0) {
                                auto attr = [&](uint64_t& counter, uint32_t component) {
                                    if (component > 0) {
                                        counter += (visible_load * component) / miss_duration;
                                    }
                                };
                                const auto& bd = res.breakdown;
                                attr(visible_mem_l2_latency_,   bd.l2_latency);
                                attr(visible_mem_l3_latency_,   bd.l3_latency);
                                attr(visible_mem_dram_latency_, bd.dram_latency);
                                attr(visible_mem_l2_bw_stall_,  bd.l2_bw_stall);
                                attr(visible_mem_l3_bw_stall_,  bd.l3_bw_stall);
                                attr(visible_mem_dram_bw_stall_, bd.dram_bw_stall);
                                attr(visible_mem_tlb_latency_,  bd.tlb_latency);
                            }
                        }
                        mcw_memory_intervals_.push_back({miss_start, miss_end});
                        mcw_memory_depth_sum_ += mcw_memory_intervals_.size();
                        mcw_memory_depth_samples_++;
                        while (mcw_memory_intervals_.size() >
                               config_.experimental.mcw_window_size) {
                            mcw_memory_intervals_.pop_front();
                            mcw_window_full_evictions_++;
                        }
                        mcw_memory_busy_until_ =
                            std::max(mcw_memory_busy_until_, miss_end);
                        mcw_load_miss_intervals_++;
                    }
                }
            }
        } else if (uop.type == InstType::STORE) {
            // Store execution only generates address and data.
            // Actual cache write happens at Retire stage!
            exec_latency = 1; // AGU latency
        }
        
        uint64_t complete_cycle = issue_cycle + exec_latency;
        uint64_t visible_complete_cycle = complete_cycle;
        if (uop.type == InstType::STORE) {
            visible_complete_cycle = std::max(complete_cycle, store_data_ready_cycle);
        }
        last_uop_complete_cycle_ = complete_cycle;
        if (uop.type == InstType::BRANCH) {
            last_branch_complete_cycle_ = complete_cycle;
        }
        // 3. Update Register Alias Table (RAT) for Destination Operands
        for (uint16_t reg : uop.dst_regs) {
            rat_[reg] = visible_complete_cycle;
        }
        
        // c. Retire Stage
        uint64_t retire_cycle = std::max(visible_complete_cycle, last_retire_cycle_);
        if (retire_count_ >= config_.retire_width) {
            retire_cycle++;
            retire_count_ = 0;
        }
        
        // In-order retire constraint: cannot retire before older instructions
        if (!rob_retire_cycles_.empty()) {
            uint64_t previous_retire = rob_retire_cycles_.back();
            if (previous_retire > retire_cycle) {
                retire_cycle = previous_retire;
                retire_count_ = 0;
            }
        }
        
        last_retire_cycle_ = retire_cycle;
        retire_count_++;

        // Cache write happens at retire for STOREs
        if (uop.type == InstType::STORE) {
            uint32_t mem_lat = mem_->write_data(uop.mem_addr, uop.mem_size, retire_cycle);
            if (mem_lat > config_.l1d.latency) {
                total_dcache_miss_penalties_ += (mem_lat - config_.l1d.latency);
            }
            // Store buffer entry only frees once the data is actually drained
            // into L1D, not at retire. This makes SQ-full stalls reflect the
            // real drain bandwidth (relevant when stores miss to L2/L3/DRAM).
            uint64_t sq_free_cycle = retire_cycle;
            if (config_.experimental.enable_sq_drain_block && mem_lat > config_.l1d.latency) {
                uint64_t drain_extra = mem_lat - config_.l1d.latency;
                sq_free_cycle = retire_cycle + drain_extra;
                total_sq_drain_stall_ += drain_extra;
            }
            sq_.push_back(
                {dispatch_cycle, complete_cycle, visible_complete_cycle, retire_cycle,
                 sq_free_cycle,
                 uop.mem_addr, uop.mem_size});
        } else if (uop.type == InstType::LOAD) {
            lq_.push_back({dispatch_cycle, retire_cycle, uop.mem_addr, uop.mem_size});
        }
        
        // Add to IQ tracking (leaves RS when it issues)
        iq_.push_back({dispatch_cycle, issue_cycle});
        
        // Add to ROB tracking
        rob_retire_cycles_.push_back(retire_cycle);
        if (rob_retire_cycles_.size() > config_.rob_size) {
            rob_retire_cycles_.pop_front();
        }
        
        // Update global cycle
        current_cycle_ = std::max(current_cycle_, retire_cycle);

        // Serialization Hazard (Block Pipeline)
        // No younger instructions can be fetched/decoded/dispatched until this serializing instruction retires.
        if (uop.is_serializing) {
            last_fetch_cycle_ = std::max(last_fetch_cycle_, retire_cycle);
            fetch_count_ = 0;
            
            last_decode_cycle_ = std::max(last_decode_cycle_, retire_cycle);
            decode_count_ = 0;
            
            last_dispatch_cycle_ = std::max(last_dispatch_cycle_, retire_cycle);
            dispatch_count_ = 0;
        }
    }

    // Apply branch misprediction penalty
    // A mispredicted branch flushes the frontend, meaning the next instruction 
    // cannot be fetched until the branch resolves (complete_cycle).
    if (mispredicted) {
        // The branch resolves at execute-completion time of the branch uop
        // itself, not at retire time.  In an OOO core, branch misprediction
        // is detected at execute, while older loads may still be waiting for
        // data.  Using retire time would always find an empty memory-interval
        // deque because all prior loads have already retired (in-order
        // retirement).
        uint64_t resolve_cycle = last_branch_complete_cycle_;
        uint64_t flush_target =
            resolve_cycle + config_.branch_mispredict_penalty;
        if (config_.experimental.enable_mcw_stats) {
            br_mispredict_count_++;
            while (!mcw_memory_intervals_.empty() &&
                   mcw_memory_intervals_.front().end_cycle <= resolve_cycle) {
                mcw_memory_intervals_.pop_front();
            }
            bool deque_nonempty = !mcw_memory_intervals_.empty();
            if (deque_nonempty) {
                br_deque_nonempty_at_resolve_++;
            }
            uint64_t window_memory_tail = active_memory_tail_window(
                mcw_memory_intervals_, resolve_cycle, flush_target);
            if (window_memory_tail > 0) {
                br_window_hit_count_++;
                br_window_tail_sum_ += window_memory_tail;
            }
            // When the interval deque is empty (common: prior loads have all
            // retired by the time this branch resolves), fall back to
            // mcw_memory_busy_until_ which tracks the latest memory end_cycle
            // without being cleaned up.  If memory is still busy past
            // resolve_cycle, the branch recovery window overlaps with it.
            if (window_memory_tail == 0 && mcw_memory_busy_until_ > resolve_cycle) {
                br_busy_until_fallback_count_++;
                window_memory_tail = mcw_memory_busy_until_;
            }
            if (window_memory_tail == 0 && !deque_nonempty) {
                // deque empty, busy_until didn't cover either
            } else if (window_memory_tail > 0 && !deque_nonempty) {
                br_deque_empty_busy_cover_count_++;
            }
            // True overlap: compute actual intersection between
            // [resolve_cycle, flush_target) and all active memory intervals.
            for (const auto& interval : mcw_memory_intervals_) {
                if (interval.end_cycle > resolve_cycle &&
                    interval.start_cycle < flush_target) {
                    uint64_t overlap_start =
                        std::max(resolve_cycle, interval.start_cycle);
                    uint64_t overlap_end =
                        std::min(flush_target, interval.end_cycle);
                    if (overlap_end > overlap_start) {
                        br_true_overlap_cycles_ +=
                            (overlap_end - overlap_start);
                    }
                }
            }
            uint64_t coverage_tail =
                std::max(mcw_critical_tail_cycle_, window_memory_tail);
            uint64_t visible_branch = account_interval_against_tail(
                resolve_cycle, flush_target, coverage_tail,
                mcw_visible_branch_recovery_cycles_,
                mcw_hidden_branch_recovery_cycles_);
            mcw_branch_recovery_intervals_++;
            if (visible_branch > 0) {
                br_visible_nonzero_count_++;
                mcw_critical_tail_cycle_ =
                    std::max(mcw_critical_tail_cycle_, flush_target);
            }
        }
        if (flush_target > last_fetch_cycle_) {
            stall_branch_flush_ += (flush_target - last_fetch_cycle_);
        }
        last_fetch_cycle_ = std::max(last_fetch_cycle_, flush_target);
        fetch_count_ = 0;

        // Decode and Dispatch also stalled
        last_decode_cycle_ = std::max(last_decode_cycle_, last_fetch_cycle_ + 1);
        decode_count_ = 0;
        last_dispatch_cycle_ = std::max(
            last_dispatch_cycle_,
            last_fetch_cycle_ + kFrontEndPipelineDepth);
        dispatch_count_ = 0;

        // Total cycles should include recovery bubbles even when the trace ends
        // immediately after a mispredicted branch. Without this tail accounting,
        // branch recovery only affects future instructions and disappears from
        // the final cycle count if there are no more uops to retire.
        current_cycle_ = std::max(current_cycle_, last_dispatch_cycle_);
    }

    // Periodically cleanup issue counts and RAT to save memory
    if (total_macro_insts_ % 10000 == 0) {
        cleanup_issue_counts(current_cycle_);
        cleanup_rat(current_cycle_);
    }
}

uint64_t IntervalCore::get_next_issue_cycle(uint64_t desired_cycle, InstType type) {
    uint64_t cycle = desired_cycle;
    while (true) {
        auto& tracker = issue_trackers_[cycle];
        if (tracker.total < config_.issue_width) {
            bool port_available = false;
            switch (type) {
                case InstType::ALU:
                    if (tracker.alu < config_.num_alu_ports) {
                        tracker.alu++;
                        port_available = true;
                    }
                    break;
                case InstType::LOAD:
                    if (tracker.load < config_.num_load_ports) {
                        tracker.load++;
                        port_available = true;
                    }
                    break;
                case InstType::STORE:
                    if (tracker.store < config_.num_store_ports) {
                        tracker.store++;
                        port_available = true;
                    }
                    break;
                case InstType::BRANCH:
                    if (tracker.branch < config_.num_branch_ports) {
                        tracker.branch++;
                        port_available = true;
                    }
                    break;
                default:
                    port_available = true; // Fallback
                    break;
            }

            if (port_available) {
                tracker.total++;
                return cycle;
            }
        }
        cycle++;
    }
}

void IntervalCore::cleanup_issue_counts(uint64_t current_cycle) {
    // Remove entries older than current_cycle - 1000 to keep the map small
    if (current_cycle < 1000) return;
    uint64_t threshold = current_cycle - 1000;
    
    for (auto it = issue_trackers_.begin(); it != issue_trackers_.end(); ) {
        if (it->first < threshold) {
            it = issue_trackers_.erase(it);
        } else {
            ++it;
        }
    }
}

void IntervalCore::cleanup_rat(uint64_t current_cycle) {
    if (current_cycle < 1000) return;
    uint64_t threshold = current_cycle - 1000;

    for (auto it = rat_.begin(); it != rat_.end(); ) {
        if (it->second < threshold) {
            it = rat_.erase(it);
        } else {
            ++it;
        }
    }
}

void IntervalCore::finish() {
    if (finish_applied_) {
        return;
    }
    // Ensure all instructions in ROB are retired
    if (!rob_retire_cycles_.empty()) {
        current_cycle_ = std::max(current_cycle_, rob_retire_cycles_.back());
    }
    current_cycle_ = std::max(current_cycle_, last_fetch_cycle_);
    current_cycle_ = std::max(current_cycle_, last_decode_cycle_);
    current_cycle_ = std::max(current_cycle_, last_dispatch_cycle_);
    current_cycle_ = std::max(current_cycle_, last_retire_cycle_);

    if (config_.experimental.enable_mcw_timing) {
        auto ceil_div = [](uint64_t value, uint32_t divisor) -> uint64_t {
            if (divisor == 0) return 0;
            return (value + divisor - 1) / divisor;
        };

        uint64_t base_cycles = std::max<uint64_t>(
            1, ceil_div(total_uops_, std::max<uint32_t>(1, config_.issue_width)));
        uint64_t visible_frontend_miss = total_icache_miss_penalties_;
        uint64_t visible_sq_drain = total_sq_drain_visible_cycles_;
        uint64_t visible_partial_stlf = total_partial_stlf_stall_;

        // MCW timing replaces the old global backend visibility and branch
        // overlap percentages with per-event critical-tail attribution.
        total_backend_stall_visible_cycles_ = mcw_visible_load_miss_cycles_;
        total_branch_memory_overlap_cycles_ = mcw_hidden_branch_recovery_cycles_;
        total_mcw_timing_cycles_ =
            base_cycles +
            mcw_visible_dependency_cycles_ +
            mcw_visible_load_miss_cycles_ +
            mcw_visible_branch_recovery_cycles_ +
            visible_frontend_miss +
            visible_sq_drain +
            visible_partial_stlf;
        current_cycle_ = std::max<uint64_t>(1, total_mcw_timing_cycles_);
    } else {
        total_backend_stall_visible_cycles_ =
            (total_mshr_issue_stall_ *
             config_.experimental.backend_stall_visibility_pct) / 100;
        current_cycle_ += total_backend_stall_visible_cycles_;

        // Sniper's ROB/interval timer does not make branch recovery and backend
        // memory pressure fully additive: if the backend is already waiting on
        // long-latency misses, part of the frontend refill bubble is hidden.
        uint64_t overlap_budget =
            std::min(stall_branch_flush_, total_mshr_issue_stall_);
        total_branch_memory_overlap_cycles_ =
            (overlap_budget *
             config_.experimental.branch_flush_memory_overlap_pct) / 100;
        current_cycle_ -= std::min(current_cycle_, total_branch_memory_overlap_cycles_);
    }
    finish_applied_ = true;
}

void IntervalCore::reset_stats() {
    total_macro_insts_ = 0;
    total_uops_ = 0;
    total_branch_mispredicts_ = 0;
    total_raw_branch_mispredicts_ = 0;
    total_direct_target_proxy_misses_ = 0;
    total_direct_target_visible_misses_ = 0;
    direct_target_visibility_accum_ = 0;
    branch_total_cond_ = 0;
    branch_total_return_ = 0;
    branch_total_indirect_ = 0;
    branch_total_direct_call_ = 0;
    branch_total_direct_jump_ = 0;
    branch_miss_cond_ = 0;
    branch_miss_return_ = 0;
    branch_miss_indirect_ = 0;
    branch_miss_direct_target_ = 0;
    total_icache_miss_penalties_ = 0;
    total_dcache_miss_penalties_ = 0;
    total_partial_stlf_count_ = 0;
    total_partial_stlf_stall_ = 0;
    total_sq_drain_stall_ = 0;
    total_sq_drain_visible_cycles_ = 0;
    total_mshr_issue_stall_ = 0;
    total_backend_stall_visible_cycles_ = 0;
    total_branch_memory_overlap_cycles_ = 0;
    total_mcw_timing_cycles_ = 0;
    finish_applied_ = false;
    stall_rob_full_ = 0;
    stall_iq_full_ = 0;
    stall_lq_full_ = 0;
    stall_sq_full_ = 0;
    stall_raw_dep_ = 0;
    stall_port_busy_ = 0;
    stall_branch_flush_ = 0;
    stall_serialize_ = 0;
    mcw_memory_intervals_.clear();
    mcw_critical_tail_cycle_ = 1;
    mcw_memory_busy_until_ = 1;
    last_uop_complete_cycle_ = 1;
    last_branch_complete_cycle_ = 1;
    mcw_visible_dependency_cycles_ = 0;
    mcw_hidden_dependency_cycles_ = 0;
    mcw_visible_load_miss_cycles_ = 0;
    mcw_hidden_load_miss_cycles_ = 0;
    mcw_visible_branch_recovery_cycles_ = 0;
    mcw_hidden_branch_recovery_cycles_ = 0;
    mcw_load_miss_intervals_ = 0;
    mcw_branch_recovery_intervals_ = 0;
    mcw_dep_stall_with_memory_overlap_ = 0;
    mcw_dep_stall_no_memory_overlap_ = 0;
    mcw_memory_depth_sum_ = 0;
    mcw_memory_depth_samples_ = 0;
    mcw_window_full_evictions_ = 0;
    visible_mem_l1_latency_ = 0;
    visible_mem_l2_latency_ = 0;
    visible_mem_l3_latency_ = 0;
    visible_mem_dram_latency_ = 0;
    visible_mem_l2_bw_stall_ = 0;
    visible_mem_l3_bw_stall_ = 0;
    visible_mem_dram_bw_stall_ = 0;
    visible_mem_mshr_stall_ = 0;
    visible_mem_tlb_latency_ = 0;
    dep_true_blocking_cycles_ = 0;
    dep_apparent_cycles_ = 0;
    dep_interval_memory_overlap_cycles_ = 0;
    dep_interval_total_cycles_ = 0;
    br_mispredict_count_ = 0;
    br_deque_nonempty_at_resolve_ = 0;
    br_window_hit_count_ = 0;
    br_window_tail_sum_ = 0;
    br_busy_until_fallback_count_ = 0;
    br_deque_empty_busy_cover_count_ = 0;
    br_true_overlap_cycles_ = 0;
    br_visible_nonzero_count_ = 0;
    branch_predictor_->reset_stats();
}

void IntervalCore::print_stats() const {
    double ipc = current_cycle_ > 0 ? (double)total_macro_insts_ / current_cycle_ : 0.0;
    double upc = current_cycle_ > 0 ? (double)total_uops_ / current_cycle_ : 0.0;
    double cpi = total_macro_insts_ > 0 ? (double)current_cycle_ / total_macro_insts_ : 0.0;

    auto ceil_div = [](uint64_t value, uint32_t divisor) -> uint64_t {
        if (divisor == 0) return 0;
        return (value + divisor - 1) / divisor;
    };

    // Approximate elapsed-cycle CPI stack. Raw stall counters below can overlap
    // heavily; this section keeps the main visible terms explicit and puts the
    // rest into residual cycles so totals stay easy to audit.
    uint64_t base_cycles = std::max<uint64_t>(
        1, ceil_div(total_uops_, std::max<uint32_t>(1, config_.issue_width)));
    uint64_t hidden_branch_recovery = total_branch_memory_overlap_cycles_;
    uint64_t visible_branch_recovery;
    uint64_t visible_memory_stall;
    if (config_.experimental.enable_mcw_timing) {
        // When MCW timing drives current_cycle_, the CPI decomposition must
        // use MCW-attributed values to stay consistent with total cycles.
        // Raw stall_branch_flush_ counts every frontend bubble cycle without
        // overlap deduction, which overstates visible branch recovery and
        // creates a spurious Overlap/Overcount Delta.
        visible_branch_recovery = mcw_visible_branch_recovery_cycles_;
        visible_memory_stall = mcw_visible_load_miss_cycles_;
    } else {
        visible_branch_recovery =
            stall_branch_flush_ > hidden_branch_recovery
                ? stall_branch_flush_ - hidden_branch_recovery
                : 0;
        visible_memory_stall = total_backend_stall_visible_cycles_;
    }
    uint64_t visible_frontend_miss = total_icache_miss_penalties_;
    uint64_t visible_sq_drain = total_sq_drain_visible_cycles_;
    uint64_t visible_partial_stlf = total_partial_stlf_stall_;
    uint64_t accounted_cycles = base_cycles + visible_branch_recovery +
                                visible_memory_stall + visible_frontend_miss +
                                visible_sq_drain + visible_partial_stlf;
    uint64_t residual_cycles =
        current_cycle_ > accounted_cycles ? current_cycle_ - accounted_cycles : 0;
    uint64_t overlapped_or_overcounted_cycles =
        accounted_cycles > current_cycle_ ? accounted_cycles - current_cycle_ : 0;

    // Split the residual bucket using the relative weight of the remaining raw
    // charged stalls. These counters are not elapsed-cycle clean, so treat the
    // result as an approximate attribution, not a strict timing sum.
    uint64_t residual_raw_total = stall_raw_dep_ + stall_port_busy_ +
                                  stall_rob_full_ + stall_iq_full_ +
                                  stall_lq_full_ + stall_sq_full_ +
                                  stall_serialize_;
    uint64_t approx_dependency_stall = 0;
    uint64_t approx_port_pressure = 0;
    uint64_t approx_structural_other = residual_cycles;
    if (residual_raw_total > 0 && residual_cycles > 0) {
        approx_dependency_stall =
            (residual_cycles * stall_raw_dep_) / residual_raw_total;
        approx_port_pressure =
            (residual_cycles * stall_port_busy_) / residual_raw_total;
        uint64_t assigned = approx_dependency_stall + approx_port_pressure;
        approx_structural_other =
            residual_cycles > assigned ? residual_cycles - assigned : 0;
    }

    auto as_cpi = [&](uint64_t cycles) -> double {
        return total_macro_insts_ > 0 ? (double)cycles / total_macro_insts_ : 0.0;
    };

    std::cout << "\n=== IntervalCore Performance Stats ===\n"
              << "Total Cycles:         " << current_cycle_ << "\n"
              << "Macro-Instructions:   " << total_macro_insts_ << "\n"
              << "Micro-Operations:     " << total_uops_ << "\n"
              << "IPC (Macro):          " << std::fixed << std::setprecision(3) << ipc << "\n"
              << "CPI (Macro):          " << std::fixed << std::setprecision(3) << cpi << "\n"
              << "UPC (Micro):          " << std::fixed << std::setprecision(3) << upc << "\n"
              << "Branch Mispredicts:   " << total_branch_mispredicts_ << "\n"
              << "Raw Branch Mispredicts: " << total_raw_branch_mispredicts_ << "\n"
              << "Direct Target Proxy Misses: " << total_direct_target_proxy_misses_ << "\n"
              << "Direct Target Visible Misses: " << total_direct_target_visible_misses_ << "\n"
              << "--- Branch-Type Breakdown (PMU audit) ---\n"
              << "  Cond Total:     " << branch_total_cond_ << "\n"
              << "  Cond Miss:      " << branch_miss_cond_ << "\n"
              << "  Return Total:   " << branch_total_return_ << "\n"
              << "  Return Miss:    " << branch_miss_return_ << "\n"
              << "  Indirect Total: " << branch_total_indirect_ << "\n"
              << "  Indirect Miss:  " << branch_miss_indirect_ << "\n"
              << "  DirectCall Tot: " << branch_total_direct_call_ << "\n"
              << "  DirectJump Tot: " << branch_total_direct_jump_ << "\n"
              << "  DirTarget Miss: " << branch_miss_direct_target_ << "\n"
              << "  Branch Total:   " << (branch_total_cond_ + branch_total_return_ + branch_total_indirect_ + branch_total_direct_call_ + branch_total_direct_jump_) << "\n"
              << "  Branch Miss Sum:" << (branch_miss_cond_ + branch_miss_return_ + branch_miss_indirect_ + branch_miss_direct_target_) << "\n"
              << "I-Cache Miss Penalty: " << total_icache_miss_penalties_ << " cycles\n"
              << "D-Cache Miss Penalty: " << total_dcache_miss_penalties_ << " cycles\n"
              << "MSHR Issue Stall:     " << total_mshr_issue_stall_ << " cycles\n"
              << "Backend Stall Visible: " << total_backend_stall_visible_cycles_ << " cycles\n"
              << "Branch/Memory Overlap: " << total_branch_memory_overlap_cycles_ << " cycles\n"
              << "MCW Timing Cycles:    " << total_mcw_timing_cycles_ << "\n"
              << "Partial STLF Count:   " << total_partial_stlf_count_ << "\n"
              << "Partial STLF Stall:   " << total_partial_stlf_stall_ << " cycles\n"
              << "SQ Drain Extra Stall: " << total_sq_drain_stall_ << " cycles\n"
              << "SQ Drain Visible:     " << total_sq_drain_visible_cycles_ << " cycles\n"
              << "--- Stall Breakdown (uop-cycles charged) ---\n"
              << "  ROB full:      " << stall_rob_full_ << "\n"
              << "  IQ/RS full:    " << stall_iq_full_ << "\n"
              << "  LQ full:       " << stall_lq_full_ << "\n"
              << "  SQ full:       " << stall_sq_full_ << "\n"
              << "  RAW dep:       " << stall_raw_dep_ << "\n"
              << "  Port busy:     " << stall_port_busy_ << "\n"
              << "  Branch flush:  " << stall_branch_flush_ << "\n"
              << "  Serialize:     " << stall_serialize_ << "\n"
              << "======================================\n";

    std::cout << "\n--- CPI Decomposition (approx elapsed cycles) ---\n"
              << "  Base Cycles:               " << base_cycles
              << " (CPI " << std::fixed << std::setprecision(3)
              << as_cpi(base_cycles) << ")\n"
              << "  Visible Branch Recovery:   " << visible_branch_recovery
              << " (CPI " << as_cpi(visible_branch_recovery) << ")\n"
              << "  Hidden Branch Recovery:    " << hidden_branch_recovery
              << " (CPI " << as_cpi(hidden_branch_recovery) << ")\n"
              << "  Visible Memory Stall:      " << visible_memory_stall
              << " (CPI " << as_cpi(visible_memory_stall) << ")\n"
              << "  Visible Frontend Miss:     " << visible_frontend_miss
              << " (CPI " << as_cpi(visible_frontend_miss) << ")\n"
              << "  Visible SQ Drain:          " << visible_sq_drain
              << " (CPI " << as_cpi(visible_sq_drain) << ")\n"
              << "  Visible Partial STLF:      " << visible_partial_stlf
              << " (CPI " << as_cpi(visible_partial_stlf) << ")\n"
              << "  Dependency Stall:          " << approx_dependency_stall
              << " (CPI " << as_cpi(approx_dependency_stall) << ")\n"
              << "  Port Pressure:             " << approx_port_pressure
              << " (CPI " << as_cpi(approx_port_pressure) << ")\n"
              << "  Structural/Other Cycles:   " << approx_structural_other
              << " (CPI " << as_cpi(approx_structural_other) << ")\n"
              << "  Residual/Other Cycles:     " << residual_cycles
              << " (CPI " << as_cpi(residual_cycles) << ")\n"
              << "  Overlap/Overcount Delta:   " << overlapped_or_overcounted_cycles
              << " (CPI " << as_cpi(overlapped_or_overcounted_cycles) << ")\n"
              << "  Accounted Visible Cycles:  " << accounted_cycles
              << " (CPI " << as_cpi(accounted_cycles) << ")\n"
              << "-----------------------------------------------\n";

    if (config_.experimental.enable_mcw_stats) {
        uint64_t mcw_visible_total =
            mcw_visible_dependency_cycles_ +
            mcw_visible_load_miss_cycles_ +
            mcw_visible_branch_recovery_cycles_;
        uint64_t mcw_hidden_total =
            mcw_hidden_dependency_cycles_ +
            mcw_hidden_load_miss_cycles_ +
            mcw_hidden_branch_recovery_cycles_;
        std::cout << "\n--- MineSim Criticality Window (observation) ---\n"
                  << "  MCW Window Size:            "
                  << config_.experimental.mcw_window_size << "\n"
                  << "  MCW Critical Tail:          "
                  << mcw_critical_tail_cycle_ << "\n"
                  << "  MCW Memory Busy Until:      "
                  << mcw_memory_busy_until_ << "\n"
                  << "  MCW Visible Dependency:     "
                  << mcw_visible_dependency_cycles_
                  << " (CPI " << as_cpi(mcw_visible_dependency_cycles_) << ")\n"
                  << "  MCW Hidden Dependency:      "
                  << mcw_hidden_dependency_cycles_
                  << " (CPI " << as_cpi(mcw_hidden_dependency_cycles_) << ")\n"
                  << "  MCW Load Miss Intervals:    "
                  << mcw_load_miss_intervals_ << "\n"
                  << "  MCW Visible Load Miss:      "
                  << mcw_visible_load_miss_cycles_
                  << " (CPI " << as_cpi(mcw_visible_load_miss_cycles_) << ")\n"
                  << "  MCW Hidden Load Miss:       "
                  << mcw_hidden_load_miss_cycles_
                  << " (CPI " << as_cpi(mcw_hidden_load_miss_cycles_) << ")\n"
                  << "  --- Visible Memory Sub-Component Breakdown ---\n"
                  << "    L2 Latency:       "
                  << visible_mem_l2_latency_
                  << " (CPI " << as_cpi(visible_mem_l2_latency_) << ")\n"
                  << "    L3 Latency:       "
                  << visible_mem_l3_latency_
                  << " (CPI " << as_cpi(visible_mem_l3_latency_) << ")\n"
                  << "    DRAM Latency:     "
                  << visible_mem_dram_latency_
                  << " (CPI " << as_cpi(visible_mem_dram_latency_) << ")\n"
                  << "    L2 BW Stall:      "
                  << visible_mem_l2_bw_stall_
                  << " (CPI " << as_cpi(visible_mem_l2_bw_stall_) << ")\n"
                  << "    L3 BW Stall:      "
                  << visible_mem_l3_bw_stall_
                  << " (CPI " << as_cpi(visible_mem_l3_bw_stall_) << ")\n"
                  << "    DRAM BW Stall:    "
                  << visible_mem_dram_bw_stall_
                  << " (CPI " << as_cpi(visible_mem_dram_bw_stall_) << ")\n"
                  << "    TLB Latency:      "
                  << visible_mem_tlb_latency_
                  << " (CPI " << as_cpi(visible_mem_tlb_latency_) << ")\n"
                  << "  --- L2/L3 BW Visible-vs-Hidden Projection ---\n"
                  << "    L2 BW Stall (raw):     "
                  << mem_->get_total_l2_bw_stall()
                  << " (CPI " << as_cpi(mem_->get_total_l2_bw_stall()) << ")\n"
                  << "    L2 BW Stall (visible): "
                  << visible_mem_l2_bw_stall_
                  << " (CPI " << as_cpi(visible_mem_l2_bw_stall_) << ")\n"
                  << "    L2 BW Stall (hidden):  "
                  << (mem_->get_total_l2_bw_stall() > visible_mem_l2_bw_stall_
                      ? mem_->get_total_l2_bw_stall() - visible_mem_l2_bw_stall_ : 0)
                  << " (CPI " << as_cpi(mem_->get_total_l2_bw_stall() > visible_mem_l2_bw_stall_
                      ? mem_->get_total_l2_bw_stall() - visible_mem_l2_bw_stall_ : 0) << ")\n"
                  << "    L3 BW Stall (raw):     "
                  << mem_->get_total_l3_bw_stall()
                  << " (CPI " << as_cpi(mem_->get_total_l3_bw_stall()) << ")\n"
                  << "    L3 BW Stall (visible): "
                  << visible_mem_l3_bw_stall_
                  << " (CPI " << as_cpi(visible_mem_l3_bw_stall_) << ")\n"
                  << "    L3 BW Stall (hidden):  "
                  << (mem_->get_total_l3_bw_stall() > visible_mem_l3_bw_stall_
                      ? mem_->get_total_l3_bw_stall() - visible_mem_l3_bw_stall_ : 0)
                  << " (CPI " << as_cpi(mem_->get_total_l3_bw_stall() > visible_mem_l3_bw_stall_
                      ? mem_->get_total_l3_bw_stall() - visible_mem_l3_bw_stall_ : 0) << ")\n"
                  << "  --- Prefetch Correlation ---\n"
                  << "    L2 Prefetch Installs:   "
                  << mem_->get_l2_prefetch_installs() << "\n"
                  << "    L2 Prefetch Redundant:  "
                  << mem_->get_l2_prefetch_redundant() << "\n"
                  << "    L3 Prefetch Installs:   "
                  << mem_->get_l3_prefetch_installs() << "\n"
                  << "    L3 Prefetch Redundant:  "
                  << mem_->get_l3_prefetch_redundant() << "\n"
                  << "  MCW Branch Intervals:       "
                  << mcw_branch_recovery_intervals_ << "\n"
                  << "  MCW Visible Branch:         "
                  << mcw_visible_branch_recovery_cycles_
                  << " (CPI " << as_cpi(mcw_visible_branch_recovery_cycles_) << ")\n"
                  << "  MCW Hidden Branch:          "
                  << mcw_hidden_branch_recovery_cycles_
                  << " (CPI " << as_cpi(mcw_hidden_branch_recovery_cycles_) << ")\n"
                  << "  MCW Visible Total:          "
                  << mcw_visible_total
                  << " (CPI " << as_cpi(mcw_visible_total) << ")\n"
                  << "  MCW Hidden Total:           "
                  << mcw_hidden_total
                  << " (CPI " << as_cpi(mcw_hidden_total) << ")\n"
                  << "  MCW DepStall w/ MemOverlap: "
                  << mcw_dep_stall_with_memory_overlap_
                  << " (CPI " << as_cpi(mcw_dep_stall_with_memory_overlap_) << ")\n"
                  << "  MCW DepStall no MemOverlap: "
                  << mcw_dep_stall_no_memory_overlap_
                  << " (CPI " << as_cpi(mcw_dep_stall_no_memory_overlap_) << ")\n"
                  << "  MCW Avg Memory Depth:       "
                  << (mcw_memory_depth_samples_ > 0
                          ? (double)mcw_memory_depth_sum_ / mcw_memory_depth_samples_
                          : 0.0) << "\n"
                  << "  MCW Window Full Evictions:  "
                  << mcw_window_full_evictions_ << "\n"
                  << "  Dep True Blocking:          "
                  << dep_true_blocking_cycles_
                  << " (CPI " << as_cpi(dep_true_blocking_cycles_) << ")\n"
                  << "  Dep Apparent (port-hidden): "
                  << dep_apparent_cycles_
                  << " (CPI " << as_cpi(dep_apparent_cycles_) << ")\n"
                  << "  Dep Interval Total:         "
                  << dep_interval_total_cycles_
                  << " (CPI " << as_cpi(dep_interval_total_cycles_) << ")\n"
                  << "  Dep Interval MemOverlap:    "
                  << dep_interval_memory_overlap_cycles_
                  << " (CPI " << as_cpi(dep_interval_memory_overlap_cycles_) << ")\n"
                  << "  BR Mispredict Count:        "
                  << br_mispredict_count_ << "\n"
                  << "  BR Deque Nonempty@Resolve:  "
                  << br_deque_nonempty_at_resolve_ << "\n"
                  << "  BR Window Hit Count:        "
                  << br_window_hit_count_ << "\n"
                  << "  BR Window Tail Sum:         "
                  << br_window_tail_sum_ << "\n"
                  << "  BR BusyUntil Fallback:      "
                  << br_busy_until_fallback_count_ << "\n"
                  << "  BR DequeEmpty+BusyCover:    "
                  << br_deque_empty_busy_cover_count_ << "\n"
                  << "  BR True Overlap Cycles:     "
                  << br_true_overlap_cycles_
                  << " (CPI " << as_cpi(br_true_overlap_cycles_) << ")\n"
                  << "  BR Visible Nonzero Count:   "
                  << br_visible_nonzero_count_ << "\n"
                  << "-----------------------------------------------\n";
    }
              
    std::cout << "\n--- Branch Predictor Stats ---\n";
    branch_predictor_->print_stats();
    std::cout << "------------------------------\n";
}

} // namespace minesim
