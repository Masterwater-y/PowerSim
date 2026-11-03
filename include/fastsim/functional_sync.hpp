#pragma once
#include <cstdint>
#include <optional>
#include <string>
#include <vector>

namespace fastsim {
struct FunctionalSyncLayout {
    std::uint64_t records=0, warmup_records=0, measurement_records=0;
    std::uint32_t source_core=0;
};
struct FunctionalSyncEvent {
    std::uint32_t core=0, kind=0;
    std::uint64_t ordinal=0, pc=0, address_space=0;
    bool measurement=false;
    bool thread_state=false;
    std::uint64_t arg0=0, arg1=0;
    std::int64_t arg2=0;
    std::vector<std::size_t> predecessors;
};
// Immutable functional graph plus coordinator-owned readiness. No target clocks
// or observations are read by the loader; readiness is supplied by simulation.
class FunctionalSyncPlan {
  public:
    FunctionalSyncPlan(const std::string& path, std::uint32_t cores,
                       bool batch_observations=true);
    FunctionalSyncPlan(const std::string& sync_path, const std::string& state_path,
                       std::uint32_t cores, bool batch_observations=true);
    const FunctionalSyncLayout& layout(std::uint32_t core) const { return layouts_.at(core); }
    const FunctionalSyncEvent& event(std::size_t id) const { return events_.at(id); }
    const std::vector<std::size_t>& stream(std::uint32_t core) const { return streams_.at(core); }
    std::size_t event_count() const { return events_.size(); }
    bool requires_execution_boundary(std::size_t id) const { return execution_boundaries_.at(id); }
    std::size_t execution_boundary_count() const { return execution_boundary_count_; }
    void observe(std::size_t id, std::uint64_t corrected_retire_reference_q16);
    bool can_release(std::size_t id) const;
    std::uint64_t predecessor_ready(std::size_t id) const;
    std::uint64_t release(std::size_t id);
    bool all_released() const { return released_count_==execution_boundary_count_; }
    bool is_thread_state() const { return thread_state_; }
    const std::vector<std::uint64_t>& initial_owners() const { return initial_owners_; }
    const std::vector<std::uint64_t>& task_ids() const { return task_ids_; }
    const std::string& accounting_contract() const { return accounting_contract_; }
    const std::vector<bool>& owner_continuity_to_common_cut() const { return owner_continuity_; }
  private:
    std::vector<FunctionalSyncLayout> layouts_;
    std::vector<FunctionalSyncEvent> events_;
    std::vector<std::vector<std::size_t>> streams_;
    std::vector<std::optional<std::uint64_t>> observed_, released_;
    std::size_t released_count_=0;
    bool thread_state_=false;
    bool batch_observations_=true;
    std::vector<std::uint64_t> initial_owners_, task_ids_;
    std::string accounting_contract_;
    std::vector<bool> owner_continuity_;
    void configure_execution_boundaries();
    std::vector<bool> execution_boundaries_;
    std::vector<std::vector<std::size_t>> execution_predecessors_;
    std::size_t execution_boundary_count_=0;
};
}
