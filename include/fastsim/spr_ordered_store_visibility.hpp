#pragma once
#include <algorithm>
#include <cstdint>
#include <limits>
#include <stdexcept>

namespace fastsim {
// One per modeled Sequencer, traversed once in *accepted ordered-store order*.
// Candidate cycles are final FastSim-predicted callback-ready times. The
// returned value is a floor in that model, not a proven bound on gem5 time.
// Once scheduled, a candidate cannot be revised; callers must apply their
// other available timing feedback before invoking this single-pass component.
// It does not model Ruby reissue latency, queue capacity, data merge, cache
// permission or store-buffer release. Physical lines are caller-defined IDs.
class SprOrderedStoreVisibility {
  public:
    struct Result {
        std::uint64_t visibility_cycle=0, delay_cycles=0;
        // This marks only a same-line/same-estimated-cycle opportunity; it
        // does not assert the stores shared a physical Ruby callback.
        bool same_line_at_prior_cycle=false;
    };
    struct Counters {std::uint64_t stores=0,delayed=0,delay_cycles=0;};
    Result schedule(std::uint64_t physical_line,std::uint64_t predicted_ready) {
        std::uint64_t floor=predicted_ready;
        if (valid_) {
            auto ordered_floor=last_cycle_;
            if (physical_line!=last_line_) {
                if (ordered_floor==std::numeric_limits<std::uint64_t>::max())
                    throw std::overflow_error("ordered store visibility overflow");
                ++ordered_floor;
            }
            floor=std::max(floor,ordered_floor);
        }
        const auto delay=floor-predicted_ready;
        const bool same=valid_ && physical_line==last_line_ && floor==last_cycle_;
        valid_=true;last_line_=physical_line;last_cycle_=floor;
        ++counters_.stores;
        if (delay) {++counters_.delayed;counters_.delay_cycles+=delay;}
        return {floor,delay,same};
    }
    // Measurement reset must retain the warmup's ordering frontier.
    void reset_counters() {counters_={};}
    void reset() {*this=SprOrderedStoreVisibility{};}
    const Counters& counters() const {return counters_;}
  private:
    std::uint64_t last_line_=0,last_cycle_=0;
    bool valid_=false;
    Counters counters_{};
};
}
