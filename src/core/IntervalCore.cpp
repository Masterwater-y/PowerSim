#include "core/IntervalCore.h"
#include <iostream>
#include <iomanip>
#include <algorithm>

namespace minesim {

IntervalCore::IntervalCore(const MicroArchConfig& config, MemoryHierarchy* mem)
    : config_(config), mem_(mem) {
    branch_predictor_ = BranchPredictor::create(config);
}

void IntervalCore::step(const Instruction& inst, InstDecoder* decoder) {
    total_macro_insts_++;

    // 1. Fetch Stage
    if (fetch_count_ >= config_.fetch_width) {
        last_fetch_cycle_++;
        fetch_count_ = 0;
    }
    
    // Simulate I-Cache/ITLB access
    uint32_t fetch_latency = mem_->fetch_instruction(inst.pc, last_fetch_cycle_);
    // Base L1I latency is assumed to be hidden in the pipeline depth. 
    // Anything longer is an I-Cache or ITLB miss penalty.
    if (fetch_latency > config_.l1i.latency) {
        uint32_t penalty = fetch_latency - config_.l1i.latency;
        last_fetch_cycle_ += penalty;
        total_icache_miss_penalties_ += penalty;
    }
    uint64_t fetch_cycle = last_fetch_cycle_;
    fetch_count_++;

    // 2. Decode Stage
    uint64_t decode_cycle = std::max(fetch_cycle + 1, last_decode_cycle_);
    if (decode_count_ >= config_.decode_width) {
        decode_cycle++;
        decode_count_ = 0;
    }
    last_decode_cycle_ = decode_cycle;
    decode_count_++;

    // Decode to uops
    std::vector<MicroOp> uops = decoder->decode(inst);

    // Branch Prediction
    bool mispredicted = false;
    if (inst.is_conditional_branch) {
        bool predicted_taken = branch_predictor_->predict(inst.pc);
        if (predicted_taken != inst.is_branch_taken) {
            mispredicted = true;
            total_branch_mispredicts_++;
        }
        // Update predictor state
        branch_predictor_->update(inst.pc, inst.is_branch_taken);
    }

    // 3. Process Uops
    for (const auto& uop : uops) {
        total_uops_++;
        
        // a. Dispatch Stage
        uint64_t dispatch_cycle = std::max(last_decode_cycle_ + 1, last_dispatch_cycle_);
        if (dispatch_count_ >= config_.dispatch_width) {
            dispatch_cycle++;
            dispatch_count_ = 0;
        }

        // Serialization Hazard (Drain ROB)
        // If the instruction is serializing, it must wait until all older instructions have retired.
        if (uop.is_serializing && !rob_retire_cycles_.empty()) {
            uint64_t newest_retire = rob_retire_cycles_.back(); // The youngest instruction in ROB
            if (newest_retire > dispatch_cycle) {
                stat_serialization_stalls_ += (newest_retire - dispatch_cycle);
                dispatch_cycle = newest_retire;
                dispatch_count_ = 0;
            }
        }
        
        // Structural Hazard: ROB Full
        if (rob_retire_cycles_.size() >= config_.rob_size) {
            uint64_t oldest_retire = rob_retire_cycles_.front();
            if (oldest_retire > dispatch_cycle) {
                stat_rob_full_stalls_ += (oldest_retire - dispatch_cycle);
                dispatch_cycle = oldest_retire;
                dispatch_count_ = 0; // Reset count since we stalled to a new cycle
            }
        }

        // Clean up SQ and LQ of entries that have already retired BEFORE or AT this dispatch_cycle.
        // Because they no longer occupy SQ/LQ at the time this instruction is dispatched.
        while (!sq_.empty() && sq_.front().retire_cycle <= dispatch_cycle) {
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
            uint64_t oldest_sq_retire = sq_.front().retire_cycle;
            if (oldest_sq_retire > dispatch_cycle) {
                stat_sq_full_stalls_ += (oldest_sq_retire - dispatch_cycle);
                dispatch_cycle = oldest_sq_retire;
                dispatch_count_ = 0;
            }
        }

        // Structural Hazard: LQ Full
        if (uop.type == InstType::LOAD && lq_.size() >= config_.lq_size) {
            uint64_t oldest_lq_retire = lq_.front().retire_cycle;
            if (oldest_lq_retire > dispatch_cycle) {
                stat_lq_full_stalls_ += (oldest_lq_retire - dispatch_cycle);
                dispatch_cycle = oldest_lq_retire;
                dispatch_count_ = 0;
            }
        }

        // Structural Hazard: IQ / RS Full
        // The IQ size limits how many un-issued instructions can wait in the reservation station.
        if (iq_.size() >= config_.iq_size) {
            uint64_t oldest_iq_issue = iq_.front().issue_cycle;
            if (oldest_iq_issue > dispatch_cycle) {
                stat_iq_full_stalls_ += (oldest_iq_issue - dispatch_cycle);
                dispatch_cycle = oldest_iq_issue;
                dispatch_count_ = 0;
            }
        }

        last_dispatch_cycle_ = dispatch_cycle;
        dispatch_count_++;
        
        // b. Issue Stage (Execution)
        
        // 1. Data Dependency Check (Wait for Source Operands)
        uint64_t op_ready_cycle = dispatch_cycle;
        for (uint16_t reg : uop.src_regs) {
            auto it = rat_.find(reg);
            if (it != rat_.end()) {
                op_ready_cycle = std::max(op_ready_cycle, it->second);
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
                        // The data is ready at the store's complete_cycle
                        op_ready_cycle = std::max(op_ready_cycle, it->complete_cycle);
                        is_stlf_hit = true;
                        stat_stlf_hits_++;
                    } else {
                        // Partial overlap: STLF Stall (Store Forwarding Penalty)
                        // Must wait for the store to retire and write to L1D
                        uint64_t old_ready = op_ready_cycle;
                        op_ready_cycle = std::max(op_ready_cycle, it->retire_cycle);
                        if (op_ready_cycle > old_ready) {
                            stat_stlf_stalls_ += (op_ready_cycle - old_ready);
                        }
                    }
                    break; // Only care about the most recent overlapping store
                }
            }
        }
        
        // Update uop type breakdown
        if (uop.type == InstType::ALU) stat_uops_alu_++;
        else if (uop.type == InstType::LOAD) stat_uops_load_++;
        else if (uop.type == InstType::STORE) stat_uops_store_++;
        else if (uop.type == InstType::BRANCH) stat_uops_branch_++;
        
        // 2. Structural Hazard Check (Wait for Issue Port/Bandwidth)
        uint64_t issue_cycle = get_next_issue_cycle(op_ready_cycle, uop.type);
        
        // Calculate Uop Execution Latency
        uint32_t exec_latency = 1; // Default ALU/Branch
        if (uop.type == InstType::LOAD) {
            if (is_stlf_hit) {
                exec_latency = 1; // Data forwarded from SQ
            } else {
                uint32_t mem_lat = mem_->read_data(uop.mem_addr, issue_cycle);
                exec_latency = mem_lat;
                if (mem_lat > config_.l1d.latency) {
                    total_dcache_miss_penalties_ += (mem_lat - config_.l1d.latency);
                }
            }
        } else if (uop.type == InstType::STORE) {
            // Store execution only generates address and data.
            // Actual cache write happens at Retire stage!
            exec_latency = 1; // AGU latency
        }
        
        uint64_t complete_cycle = issue_cycle + exec_latency;

        // 3. Update Register Alias Table (RAT) for Destination Operands
        for (uint16_t reg : uop.dst_regs) {
            rat_[reg] = complete_cycle;
        }
        
        // c. Retire Stage
        uint64_t retire_cycle = std::max(complete_cycle, last_retire_cycle_);
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
            uint32_t mem_lat = mem_->write_data(uop.mem_addr, retire_cycle);
            if (mem_lat > config_.l1d.latency) {
                total_dcache_miss_penalties_ += (mem_lat - config_.l1d.latency);
            }
            sq_.push_back({dispatch_cycle, complete_cycle, retire_cycle, uop.mem_addr, uop.mem_size});
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
        // The branch resolves at `last_retire_cycle_` (or complete_cycle of the branch uop)
        // Add the penalty to the fetch cycle of the NEXT instruction.
        uint64_t resolve_cycle = last_retire_cycle_; 
        last_fetch_cycle_ = std::max(last_fetch_cycle_, resolve_cycle + config_.branch_mispredict_penalty);
        fetch_count_ = 0;
        
        // Decode and Dispatch also stalled
        last_decode_cycle_ = std::max(last_decode_cycle_, last_fetch_cycle_ + 1);
        decode_count_ = 0;
        last_dispatch_cycle_ = std::max(last_dispatch_cycle_, last_decode_cycle_ + 1);
        dispatch_count_ = 0;
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
    // Ensure all instructions in ROB are retired
    if (!rob_retire_cycles_.empty()) {
        current_cycle_ = std::max(current_cycle_, rob_retire_cycles_.back());
    }
}

void IntervalCore::print_stats() const {
    double ipc = current_cycle_ > 0 ? (double)total_macro_insts_ / current_cycle_ : 0.0;
    double upc = current_cycle_ > 0 ? (double)total_uops_ / current_cycle_ : 0.0;

    std::cout << "\n=== IntervalCore Performance Stats ===\n"
              << "Total Cycles:         " << current_cycle_ << "\n"
              << "Macro-Instructions:   " << total_macro_insts_ << "\n"
              << "Micro-Operations:     " << total_uops_ << "\n"
              << "IPC (Macro):          " << std::fixed << std::setprecision(3) << ipc << "\n"
              << "UPC (Micro):          " << std::fixed << std::setprecision(3) << upc << "\n"
              << "Branch Mispredicts:   " << total_branch_mispredicts_ << "\n"
              << "I-Cache Miss Penalty: " << total_icache_miss_penalties_ << " cycles\n"
              << "D-Cache Miss Penalty: " << total_dcache_miss_penalties_ << " cycles\n"
              << "======================================\n";
              
    std::cout << "\n--- Uop Breakdown ---\n"
              << "ALU:                  " << stat_uops_alu_ << " (" << std::fixed << std::setprecision(1) << (stat_uops_alu_ * 100.0 / total_uops_) << "%)\n"
              << "Load:                 " << stat_uops_load_ << " (" << std::fixed << std::setprecision(1) << (stat_uops_load_ * 100.0 / total_uops_) << "%)\n"
              << "Store:                " << stat_uops_store_ << " (" << std::fixed << std::setprecision(1) << (stat_uops_store_ * 100.0 / total_uops_) << "%)\n"
              << "Branch:               " << stat_uops_branch_ << " (" << std::fixed << std::setprecision(1) << (stat_uops_branch_ * 100.0 / total_uops_) << "%)\n";

    std::cout << "\n--- Structural Hazards & Stalls (Cycles) ---\n"
              << "ROB Full Stalls:      " << stat_rob_full_stalls_ << "\n"
              << "RS/IQ Full Stalls:    " << stat_iq_full_stalls_ << "\n"
              << "LQ Full Stalls:       " << stat_lq_full_stalls_ << "\n"
              << "SQ Full Stalls:       " << stat_sq_full_stalls_ << "\n"
              << "Serialization Stalls: " << stat_serialization_stalls_ << "\n";

    std::cout << "\n--- Memory Forwarding ---\n"
              << "STLF Hits (Forwarded):" << stat_stlf_hits_ << "\n"
              << "STLF Stalls (Penalty):" << stat_stlf_stalls_ << " cycles\n";

    std::cout << "\n--- Branch Predictor Stats ---\n";
    branch_predictor_->print_stats();
    std::cout << "------------------------------\n";
}

} // namespace minesim
