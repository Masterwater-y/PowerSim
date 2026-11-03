#include "fastsim/dram_controller_observation.hpp"

#include <cstdint>
#include <limits>
#include <stdexcept>

namespace {
void require(bool value, const char* message) {
    if (!value) throw std::runtime_error(message);
}
}

void test_dram_controller_observation() {
    fastsim::DramControllerReadObservation observation(2);
    // Arrival includes time spent waiting before a causal FRFCFS selection;
    // ready excludes controller frontend/backend and cache return transport.
    observation.record_read(1, 100, 160, 214);
    observation.record_read(1, 200, 220, 274);
    observation.record_read(0, 300, 300, 354);
    const auto& channels = observation.channels();
    require(channels[1].read_bursts == 2, "channel-local read bursts");
    require(channels[1].arrival_cycle_sum == 300 &&
            channels[1].service_cycle_sum == 380 &&
            channels[1].data_ready_cycle_sum == 488,
            "raw controller timestamp sums");
    require(channels[1].arrival_to_service_cycles == 80 &&
            channels[1].service_to_data_ready_cycles == 108 &&
            channels[1].arrival_to_data_ready_cycles == 188,
            "queue and DRAM service decomposition");
    require(channels[0].read_bursts == 1 &&
            channels[0].arrival_to_service_cycles == 0 &&
            channels[0].conserved() && channels[1].conserved(),
            "independent controllers conserve endpoint and adjacent sums");

    auto snapshot = observation;
    observation.record_read(1, 400, 430, 484);
    observation = snapshot;
    require(observation.channels()[1].read_bursts == 2,
            "discarding speculative state restores observed population");
    auto candidate = snapshot;
    candidate.record_read(1, 500, 510, 564);
    observation.install_channel(candidate, 1);
    require(observation.channels()[1].read_bursts == 3 &&
            observation.channels()[0].read_bursts == 1 &&
            observation.channels()[1].arrival_to_service_cycles == 90,
            "accepted candidate replaces one channel without duplicate counts");
    candidate.record_read(0, 600, 600, 654);
    require(observation.channels()[0].read_bursts == 1,
            "candidate copies cannot mutate installed channel counters");

    bool rejected = false;
    try { observation.record_read(0, 200, 199, 250); }
    catch (const std::invalid_argument&) { rejected = true; }
    require(rejected && observation.channels()[0].read_bursts == 1,
            "backwards service rejected before changing counters");
    rejected = false;
    try { observation.record_read(0, 200, 250, 249); }
    catch (const std::invalid_argument&) { rejected = true; }
    require(rejected && observation.channels()[0].read_bursts == 1,
            "backwards data-ready rejected before changing counters");
    rejected = false;
    try { observation.record_read(2, 0, 0, 1); }
    catch (const std::out_of_range&) { rejected = true; }
    require(rejected, "invalid controller rejected");

    fastsim::DramControllerReadObservation overflow(1);
    const auto maximum = std::numeric_limits<std::uint64_t>::max();
    overflow.record_read(0, maximum - 2, maximum - 1, maximum);
    rejected = false;
    try { overflow.record_read(0, 1, 2, 3); }
    catch (const std::overflow_error&) { rejected = true; }
    require(rejected && overflow.channels()[0].read_bursts == 1 &&
            overflow.channels()[0].data_ready_cycle_sum == maximum &&
            overflow.channels()[0].conserved(),
            "overflow rejects the entire sample without partial accumulation");

    observation.reset();
    require(observation.channels().size() == 2 &&
            observation.channels()[0].read_bursts == 0 &&
            observation.channels()[1].arrival_to_data_ready_cycles == 0 &&
            observation.channels()[1].conserved(),
            "measurement reset retains controller geometry and clears history");
}

#ifdef FASTSIM_DRAM_OBSERVATION_STANDALONE
int main() {
    test_dram_controller_observation();
}
#endif
