#pragma once

#include <cstddef>
#include <cstdint>
#include <limits>
#include <stdexcept>
#include <vector>

namespace fastsim {

// Preparations processed lazily through each channel's observed selection
// frontier. These are not full wall-window refresh counts for inactive ranks.
struct DramRankRefreshObservation {
    std::uint32_t controller=0,rank=0;
    std::uint64_t preparations=0,precharged_ranks=0,idle_skipped=0;
    std::uint64_t next_prepare_tick=0,available_tick=0,observation_start_tick=0;
};

// Controller read bursts, independent of the originating CPU operation's
// load/store direction. All timestamps use the model reference-cycle domain.
// data_ready excludes frontend/backend and cache/NoC response transport.
struct DramReadObservationCounters {
    std::uint64_t read_bursts = 0;
    std::uint64_t arrival_cycle_sum = 0;
    std::uint64_t service_cycle_sum = 0;
    std::uint64_t data_ready_cycle_sum = 0;
    std::uint64_t arrival_to_service_cycles = 0;
    std::uint64_t service_to_data_ready_cycles = 0;
    std::uint64_t arrival_to_data_ready_cycles = 0;

    bool conserved() const {
        return arrival_cycle_sum <= service_cycle_sum &&
            service_cycle_sum <= data_ready_cycle_sum &&
            service_cycle_sum - arrival_cycle_sum == arrival_to_service_cycles &&
            data_ready_cycle_sum - service_cycle_sum == service_to_data_ready_cycles &&
            data_ready_cycle_sum - arrival_cycle_sum == arrival_to_data_ready_cycles &&
            arrival_to_service_cycles <= arrival_to_data_ready_cycles &&
            arrival_to_data_ready_cycles - arrival_to_service_cycles ==
                service_to_data_ready_cycles;
    }
};

// Value-owned controller state: scheduler trials copy this with the bank
// calendar, rollback discards it, and accepted channel installation replaces
// that channel. Never keep an external observer across speculative copies.
class DramControllerReadObservation {
public:
    explicit DramControllerReadObservation(std::size_t channels)
        : channels_(channels) {}

    const std::vector<DramReadObservationCounters>& channels() const {
        return channels_;
    }

    void record_read(std::size_t channel, std::uint64_t arrival,
                     std::uint64_t service, std::uint64_t data_ready) {
        if (service < arrival || data_ready < service) {
            throw std::invalid_argument("backwards DRAM read observation");
        }
        auto next = channels_.at(channel);
        const auto add = [](std::uint64_t& target, std::uint64_t value) {
            if (value > std::numeric_limits<std::uint64_t>::max() - target) {
                throw std::overflow_error("DRAM read observation sum overflow");
            }
            target += value;
        };
        add(next.read_bursts, 1);
        add(next.arrival_cycle_sum, arrival);
        add(next.service_cycle_sum, service);
        add(next.data_ready_cycle_sum, data_ready);
        add(next.arrival_to_service_cycles, service - arrival);
        add(next.service_to_data_ready_cycles, data_ready - service);
        add(next.arrival_to_data_ready_cycles, data_ready - arrival);
        channels_[channel] = next;
    }

    void install_channel(const DramControllerReadObservation& source,
                         std::size_t channel) {
        channels_.at(channel) = source.channels_.at(channel);
    }

    void reset() {
        for (auto& channel : channels_) channel = DramReadObservationCounters{};
    }

private:
    std::vector<DramReadObservationCounters> channels_;
};

}  // namespace fastsim
