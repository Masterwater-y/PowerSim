#pragma once

#include "fastsim/config.hpp"
#include <cstdint>
#include <limits>
#include <memory>
#include <vector>

namespace fastsim {

enum class DramServiceKind : std::uint8_t { kRead, kWriteback };
struct DramServiceId {
    std::uint32_t core = 0;
    std::uint64_t sequence = 0;
    std::uint32_t fragment = 0;
    DramServiceKind kind = DramServiceKind::kRead;
    bool operator==(const DramServiceId& other) const;
    bool operator<(const DramServiceId& other) const;
};
struct MixedDramRequest {
    DramServiceId id;
    std::uint64_t arrival = 0;
    // Physical cache-line NUMBER, not byte address; RoRaBaCoCh mapping.
    std::uint64_t line = 0;
};
struct MixedDramCompletion {
    DramServiceId id;
    std::uint64_t arrival = 0, admission = 0, selection = 0;
    // Read queue capacity releases at dram_ready; external response includes
    // frontend+backend latency. For WB these are equal DRAM-data completion.
    std::uint64_t command = 0, dram_ready = 0, response = 0;
    bool row_hit = false, auto_precharged = false;
    // Zero if no corresponding operation; auto PRE refers to the selected
    // row, otherwise PRE refers to the conflicting previously open row.
    std::uint64_t precharge = 0, activation = 0;
    bool operator==(const MixedDramCompletion& other) const;
};
enum class MixedDramScheduler { kRowHitFirstApproximation, kFcfs };
struct MixedDramConfig {
    DramConfig dram;
    // All fields are integer cycles, like DramConfig. No implicit tick or
    // burst conversion: a production burst_cycles=10 stays 10.
    std::uint32_t t_cwl = 22, t_rcd_wr = 22, t_ccd_l_wr = 0;
    std::uint32_t t_rtw = 0, t_wtr = 0, t_wtr_l = 0, t_wr = 0;
    std::string page_policy = "open_adaptive";
    MixedDramScheduler scheduler = MixedDramScheduler::kRowHitFirstApproximation;
};
struct MixedDramStats {
    std::uint64_t submitted = 0, admitted = 0;
    std::uint64_t reads_serviced = 0, writes_serviced = 0;
    std::uint64_t pending_reads = 0, pending_writes = 0;
    std::uint64_t read_responses = 0, waiting_admission = 0;
    std::uint64_t admission_delayed = 0, admission_delay_cycles = 0;
    std::uint64_t row_hits = 0, precharges = 0;
    std::uint64_t row_cap_precharges = 0, adaptive_precharges = 0;
    std::uint64_t direction_switches = 0;
    std::uint64_t projected_batches = 0, projected_late_arrivals = 0;
    std::uint64_t projected_late_cycles = 0, projected_capacity_drains = 0;
    std::uint64_t projected_events = 0, projected_scanned_entries = 0;
};

// Production adapter: preserve supplied cycle-domain primitives, including
// burst_cycles; do not import the mechanism fixture's default timings.
MixedDramConfig make_mixed_dram_config(const DramConfig& dram);

// Internal persistent controller, not a per-UOP global solver. No refresh,
// same-line merging, command-bus arbitration or gem5 hidden-bank FRFCFS.
// Same-tick admissions precede selection, ordered by (arrival, ID). A caller
// certifies all arrivals < frontier are known before advance(frontier).
class MixedDramController {
public:
    // Supported exclusive admission-knowledge horizon. Sixteen uint32 timing
    // units leave headroom for future reservations (proved in reserve).
    static constexpr std::uint64_t max_frontier =
        std::numeric_limits<std::uint64_t>::max() -
        16ull * std::numeric_limits<std::uint32_t>::max();
    MixedDramController(const MixedDramConfig& config, std::uint32_t line_size);
    ~MixedDramController();
    MixedDramController(const MixedDramController&);
    MixedDramController& operator=(const MixedDramController&);
    MixedDramController(MixedDramController&&) noexcept;
    MixedDramController& operator=(MixedDramController&&) noexcept;
    // Takes ownership including future/capacity-blocked arrivals. Throws for
    // invalid line/kind, an active duplicate ID, arrival < prior frontier or
    // arrival >= max_frontier. These validations precede ownership changes.
    // IDs remain active through response; caller guarantees no ID reuse after
    // retirement. There is deliberately no full-ROI completed-ID history.
    void submit(const MixedDramRequest& request);
    // Returns owned newly SELECTED completions; command/response may be >=
    // frontier. No implicit end-of-batch write drain. Equal frontier is legal.
    // Throws invalid_argument BEFORE state changes for backwards frontier,
    // frontier > max_frontier, or an unrepresentable conservative cumulative
    // admission-delay bound: existing delay + sum(frontier-arrival) over due
    // waiting requests. Use smaller frontiers if that bound is too large.
    // Supported calls cannot overflow cycle arithmetic; timestamps never
    // silently saturate. Allocation failures are not transactionally recovered.
    std::vector<MixedDramCompletion> advance(std::uint64_t exclusive_frontier);
    // Separate, approximate contract: reserve every read in the supplied
    // projected batch, without claiming knowledge of future arrivals. Later
    // batches cannot undo selections; late arrivals clamp to the channel's
    // last selection/admission event (not its future command or response).
    // Writes below the low watermark remain owned across batches. Channels
    // progress independently. Do not mix with submit/advance on one instance.
    // IDs must never be reused after completion, as with the strict API.
    // Invalid input is rejected before mutation. Time-range/allocation errors
    // during scheduling require caller rollback; no saturation is performed.
    std::vector<MixedDramCompletion> reserve_projected_batch(
        const std::vector<MixedDramRequest>& requests);
    MixedDramStats stats() const;
private:
    struct State;
    std::unique_ptr<State> state_;
};

} // namespace fastsim
