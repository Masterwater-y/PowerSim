#include "fastsim/simulator.hpp"

#include <algorithm>
#include <array>
#include <atomic>
#include <chrono>
#include <cmath>
#include <condition_variable>
#include <cstddef>
#include <cstdint>
#include <deque>
#include <exception>
#include <functional>
#include <limits>
#include <memory>
#include <mutex>
#include <optional>
#include <queue>
#include <stdexcept>
#include <string>
#include <thread>
#include <tuple>
#include <unordered_map>
#include <unordered_set>
#include <utility>

#include "fastsim/cache.hpp"
#include "fastsim/interval_core.hpp"
#include "fastsim/predictor.hpp"

namespace fastsim {
namespace {

constexpr std::uint64_t kCycleUnit = 1ull << 16;

std::uint64_t cycles_to_fixed(std::uint64_t cycles,
                              const char* context = nullptr) {
    if (cycles > std::numeric_limits<std::uint64_t>::max() / kCycleUnit) {
        throw std::overflow_error(
            "simulated cycle count overflow: " +
            std::to_string(cycles) +
            (context == nullptr ? std::string() :
                                  std::string(" in ") + context));
    }
    return cycles * kCycleUnit;
}

std::uint64_t fixed_to_cycle_ceil(std::uint64_t value) {
    return (value + kCycleUnit - 1) / kCycleUnit;
}

void add_scaled_kernel_counter(
    std::uint64_t& target, std::uint64_t value,
    std::uint64_t occurrences) {
    if (value != 0 && occurrences >
            std::numeric_limits<std::uint64_t>::max() / value) {
        throw std::overflow_error("synthetic kernel PMU product overflow");
    }
    const auto increment = value * occurrences;
    if (increment >
        std::numeric_limits<std::uint64_t>::max() - target) {
        throw std::overflow_error("synthetic kernel PMU sum overflow");
    }
    target += increment;
}

void accumulate_kernel_event(
    KernelEventCounters& counters,
    const KernelEventProfile& profile,
    std::uint64_t occurrences = 1) {
    add_scaled_kernel_counter(counters.events, 1, occurrences);
    add_scaled_kernel_counter(
        counters.active_cycles, profile.service_cycles, occurrences);
    add_scaled_kernel_counter(
        counters.blocked_wall_cycles,
        profile.blocked_wall_cycles, occurrences);
    add_scaled_kernel_counter(
        counters.retired_instructions,
        profile.retired_instructions, occurrences);
    add_scaled_kernel_counter(
        counters.retired_uops, profile.retired_uops, occurrences);
    add_scaled_kernel_counter(
        counters.branch.branches, profile.branches, occurrences);
    add_scaled_kernel_counter(
        counters.branch.misses, profile.branch_misses, occurrences);
    add_scaled_kernel_counter(
        counters.l1d.accesses, profile.l1d_accesses, occurrences);
    add_scaled_kernel_counter(
        counters.l1d.misses, profile.l1d_misses, occurrences);
    add_scaled_kernel_counter(
        counters.l1d.hits,
        profile.l1d_accesses - profile.l1d_misses, occurrences);
    add_scaled_kernel_counter(
        counters.l2.accesses, profile.l2_accesses, occurrences);
    add_scaled_kernel_counter(
        counters.l2.misses, profile.l2_misses, occurrences);
    add_scaled_kernel_counter(
        counters.l2.hits,
        profile.l2_accesses - profile.l2_misses, occurrences);
    add_scaled_kernel_counter(
        counters.llc.accesses, profile.llc_accesses, occurrences);
    add_scaled_kernel_counter(
        counters.llc.misses, profile.llc_misses, occurrences);
    add_scaled_kernel_counter(
        counters.llc.hits,
        profile.llc_accesses - profile.llc_misses, occurrences);
    add_scaled_kernel_counter(
        counters.dtlb.accesses, profile.dtlb_accesses, occurrences);
    add_scaled_kernel_counter(
        counters.dtlb.misses, profile.dtlb_misses, occurrences);
    add_scaled_kernel_counter(
        counters.dtlb.hits,
        profile.dtlb_accesses - profile.dtlb_misses, occurrences);
}

class DramModel {
  private:
    struct Address {
        std::uint32_t channel = 0;
        std::uint32_t rank = 0;
        std::uint32_t bank = 0;
        std::uint32_t bank_group = 0;
        std::size_t flat_rank = 0;
        std::size_t flat_bank = 0;
        std::size_t flat_bank_group = 0;
        std::uint64_t row = 0;
    };

    struct BufferedWrite {
        std::uint64_t arrival = 0;
        std::uint64_t line = 0;
        std::uint64_t ordinal = 0;
        Address address;
    };

  public:
    struct Result {
        std::uint64_t completion = 0;
        std::uint64_t queue_cycles = 0;
        std::uint64_t command_at = 0;
        std::uint64_t activation_at = 0;
        std::uint64_t bank_command_at = 0;
        bool row_hit = false;
        bool row_cap_precharged = false;
    };

    struct ControllerStats {
        std::uint64_t write_enqueues = 0;
        std::uint64_t writes_drained = 0;
        std::uint64_t read_bypasses = 0;
        std::uint64_t high_watermark_switches = 0;
        std::uint64_t forced_capacity_drains = 0;
        std::uint64_t turnarounds = 0;
        std::uint64_t write_row_hits = 0;
        std::uint64_t write_row_misses = 0;
        std::uint64_t write_wait_cycles = 0;
        std::uint64_t max_pending = 0;
        std::uint64_t pending_initial = 0;
        std::uint64_t pending_final = 0;

        ControllerStats& operator+=(const ControllerStats& other) {
            write_enqueues += other.write_enqueues;
            writes_drained += other.writes_drained;
            read_bypasses += other.read_bypasses;
            high_watermark_switches +=
                other.high_watermark_switches;
            forced_capacity_drains += other.forced_capacity_drains;
            turnarounds += other.turnarounds;
            write_row_hits += other.write_row_hits;
            write_row_misses += other.write_row_misses;
            write_wait_cycles += other.write_wait_cycles;
            max_pending = std::max(max_pending, other.max_pending);
            pending_initial += other.pending_initial;
            pending_final += other.pending_final;
            return *this;
        }
    };

    struct Request {
        std::size_t id = 0;
        std::uint64_t arrival = 0;
        std::uint64_t line = 0;
        std::uint64_t ordinal = 0;
    };

    struct BatchResult {
        std::vector<Result> results;
        std::vector<std::size_t> service_order;
        std::uint64_t row_hits = 0;
        std::uint64_t row_misses = 0;
        std::uint64_t reordered_requests = 0;
        std::uint64_t max_pending = 0;
        std::uint64_t max_admitted_pending = 0;
        std::uint64_t saturated_selections = 0;
        std::uint64_t page_policy_scanned_requests = 0;
        std::uint64_t outside_window_row_hits = 0;
        std::uint64_t outside_window_bank_conflicts = 0;
        std::uint64_t row_cap_precharges = 0;
        std::uint64_t adaptive_precharges = 0;
    };

    DramModel(const DramConfig& config, std::uint32_t line_size)
        : config_(config),
          line_size_(line_size),
          capacity_lines_(config.size_bytes / line_size),
          banks_(static_cast<std::size_t>(config.channels) *
                 config.ranks_per_channel *
                 config.banks_per_channel),
          bank_group_ready_(
              static_cast<std::size_t>(config.channels) *
              config.ranks_per_channel *
              config.bank_groups_per_rank),
          rank_activations_(
              static_cast<std::size_t>(config.channels) *
              config.ranks_per_channel),
          channel_command_ready_(config.channels),
          channel_last_command_(config.channels),
          channel_last_rank_(
              config.channels,
              std::numeric_limits<std::uint32_t>::max()),
          channel_ready_(config.channels),
          write_queues_(config.channels),
          write_mode_(config.channels),
          reads_this_turn_(config.channels),
          controller_stats_(config.channels) {}

    Result access(std::uint64_t arrival, std::uint64_t line) {
        if (line >= capacity_lines_) {
            throw std::out_of_range(
                "physical memory access exceeds configured DRAM capacity");
        }
        return access_read_decoded(arrival, decode(line));
    }

    void enqueue_write(std::uint64_t arrival, std::uint64_t line) {
        if (line >= capacity_lines_) {
            throw std::out_of_range(
                "physical memory access exceeds configured DRAM capacity");
        }
        const auto address = decode(line);
        if (!config_.separate_write_queue) {
            access_decoded(arrival, address);
            return;
        }

        auto& queue = write_queues_[address.channel];
        auto& counters = controller_stats_[address.channel];
        // A full write buffer is the only point where a dirty eviction must
        // synchronously create capacity. This is intentionally separate from
        // architectural store/SQ completion: the LLC victim has already been
        // accepted, and only controller service order changes here.
        if (queue.size() >= config_.write_buffer_size) {
            ++counters.forced_capacity_drains;
            const auto low_watermark =
                static_cast<std::uint64_t>(
                    config_.write_low_threshold_percent) *
                config_.write_buffer_size / 100u;
            // At capacity the functional stream proves that admission must
            // make progress, but it cannot expose a future read-queue phase.
            // Use gem5's low watermark as a bounded hysteresis point. For a
            // tiny configured watermark this may empty the queue, matching
            // the no-read case in MemCtrl.
            do {
                drain_write_burst(address.channel);
            } while (!queue.empty() &&
                     queue.size() + config_.min_writes_per_switch >=
                         low_watermark);
        }
        queue.push_back(BufferedWrite{
            arrival, line, next_write_ordinal_++, address});
        ++counters.write_enqueues;
        counters.max_pending = std::max<std::uint64_t>(
            counters.max_pending, queue.size());
    }

    ControllerStats controller_stats() const {
        ControllerStats total;
        for (std::uint32_t channel = 0;
             channel < config_.channels; ++channel) {
            auto counters = controller_stats_[channel];
            counters.pending_final = write_queues_[channel].size();
            total += counters;
        }
        return total;
    }

    void reset_controller_stats() {
        controller_stats_.assign(config_.channels, ControllerStats{});
        for (std::uint32_t channel = 0;
             channel < config_.channels; ++channel) {
            controller_stats_[channel].max_pending =
                write_queues_[channel].size();
            controller_stats_[channel].pending_initial =
                write_queues_[channel].size();
        }
    }

    BatchResult schedule_frfcfs(
        const std::vector<Request>& requests,
        std::uint32_t selection_window,
        const std::function<void(
            std::uint32_t,
            const std::function<void(std::uint32_t)>&)>&
            parallel_for_channels = {}) {
        BatchResult batch;
        if (requests.empty()) return batch;
        const auto max_id = std::max_element(
            requests.begin(), requests.end(),
            [](const Request& left, const Request& right) {
                return left.id < right.id;
            })->id;
        batch.results.resize(max_id + 1);
        batch.service_order.reserve(requests.size());

        struct DecodedRequest {
            Request request;
            Address address;
        };
        std::vector<std::vector<DecodedRequest>> by_channel(
            config_.channels);
        for (const auto& request : requests) {
            if (request.line >= capacity_lines_) {
                throw std::out_of_range(
                    "physical memory access exceeds configured DRAM "
                    "capacity");
            }
            const auto address = decode(request.line);
            by_channel[address.channel].push_back(
                DecodedRequest{request, address});
        }

        const auto earlier = [](const DecodedRequest& left,
                                const DecodedRequest& right) {
            if (left.request.arrival != right.request.arrival) {
                return left.request.arrival < right.request.arrival;
            }
            return left.request.ordinal < right.request.ordinal;
        };
        struct ChannelBatchResult {
            std::vector<std::size_t> service_order;
            std::uint64_t row_hits = 0;
            std::uint64_t row_misses = 0;
            std::uint64_t reordered_requests = 0;
            std::uint64_t max_pending = 0;
            std::uint64_t max_admitted_pending = 0;
            std::uint64_t saturated_selections = 0;
            std::uint64_t page_policy_scanned_requests = 0;
            std::uint64_t outside_window_row_hits = 0;
            std::uint64_t outside_window_bank_conflicts = 0;
            std::uint64_t row_cap_precharges = 0;
            std::uint64_t adaptive_precharges = 0;
        };
        std::vector<ChannelBatchResult> channel_results(
            config_.channels);
        const auto schedule_channel = [&](std::uint32_t channel) {
            auto& future_requests = by_channel[channel];
            if (future_requests.empty()) return;
            auto& channel_batch = channel_results[channel];
            channel_batch.service_order.reserve(future_requests.size());
            std::sort(future_requests.begin(), future_requests.end(),
                      earlier);
            std::vector<DecodedRequest> pending;
            pending.reserve(std::min<std::size_t>(
                config_.read_buffer_size, future_requests.size()));
            std::size_t future = 0;
            std::uint64_t controller_time = 0;

            while (future != future_requests.size() || !pending.empty()) {
                if (pending.empty()) {
                    controller_time = std::max(
                        controller_time,
                        future_requests[future].request.arrival);
                    pending.push_back(future_requests[future++]);
                }

                // Requests arriving before the earliest issuable command are
                // visible to FR-FCFS. This is an event jump, not a per-cycle
                // controller scan, and is capped by gem5's read queue size.
                while (true) {
                    while (future != future_requests.size() &&
                           pending.size() < config_.read_buffer_size &&
                           future_requests[future].request.arrival <=
                               controller_time) {
                        pending.push_back(future_requests[future++]);
                    }
                    const auto candidate_count =
                        std::min<std::size_t>(selection_window,
                                             pending.size());
                    auto earliest_command =
                        std::numeric_limits<std::uint64_t>::max();
                    for (std::size_t index = 0;
                         index < candidate_count; ++index) {
                        const auto& request = pending[index];
                        earliest_command = std::min(
                            earliest_command,
                            estimate(request.request.arrival,
                                     request.address).command_at);
                    }
                    if (future == future_requests.size() ||
                        pending.size() >= config_.read_buffer_size ||
                        future_requests[future].request.arrival >
                            earliest_command) {
                        break;
                    }
                    pending.push_back(future_requests[future++]);
                }
                const auto candidate_count =
                    std::min<std::size_t>(selection_window,
                                         pending.size());
                const bool physical_queue_saturated =
                    pending.size() == config_.read_buffer_size;
                if (physical_queue_saturated) {
                    ++channel_batch.saturated_selections;
                }
                channel_batch.max_pending = std::max<std::uint64_t>(
                    channel_batch.max_pending, candidate_count);
                channel_batch.max_admitted_pending =
                    std::max<std::uint64_t>(
                        channel_batch.max_admitted_pending,
                        pending.size());

                // The functional trace exposes only a lower-bound arrival,
                // not gem5's exact memory-controller phase. Within the
                // bounded causal certificate, retain FR-FCFS's row-hit-first
                // rule and then choose the earliest issuable command. The
                // more detailed seamless/hidden-precharge choice was tested
                // separately, but without exact arrival phase it amplified
                // ordering error and reduced throughput.
                bool has_row_hit = false;
                for (std::size_t index = 0;
                     index < candidate_count; ++index) {
                    has_row_hit = has_row_hit ||
                        estimate(pending[index].request.arrival,
                                 pending[index].address).row_hit;
                }
                std::size_t selected = 0;
                auto selected_timing = estimate(
                    pending.front().request.arrival,
                    pending.front().address);
                for (std::size_t index = 1;
                     index < candidate_count; ++index) {
                    const auto candidate_timing = estimate(
                        pending[index].request.arrival,
                        pending[index].address);
                    const bool selected_eligible =
                        !has_row_hit || selected_timing.row_hit;
                    const bool candidate_eligible =
                        !has_row_hit || candidate_timing.row_hit;
                    if (candidate_eligible != selected_eligible) {
                        if (candidate_eligible) {
                            selected = index;
                            selected_timing = candidate_timing;
                        }
                        continue;
                    }
                    if (candidate_timing.command_at <
                            selected_timing.command_at ||
                        (candidate_timing.command_at ==
                             selected_timing.command_at &&
                         earlier(pending[index], pending[selected]))) {
                        selected = index;
                        selected_timing = candidate_timing;
                    }
                }

                const auto canonical = std::min_element(
                    pending.begin(), pending.end(), earlier);
                if (canonical != pending.end() &&
                    canonical->request.id !=
                        pending[selected].request.id) {
                    ++channel_batch.reordered_requests;
                }
                const auto request = pending[selected];
                pending.erase(
                    pending.begin() +
                    static_cast<std::ptrdiff_t>(selected));
                const auto& address = request.address;
                auto result = access_read_decoded(
                    request.request.arrival, address);
                controller_time = std::max(
                    controller_time, result.command_at);
                batch.results[request.request.id] = result;
                channel_batch.service_order.push_back(
                    request.request.id);
                if (result.row_hit) {
                    ++channel_batch.row_hits;
                } else {
                    ++channel_batch.row_misses;
                }
                if (result.row_cap_precharged) {
                    ++channel_batch.row_cap_precharges;
                }

                // gem5's open_adaptive policy closes a row when the read
                // queue contains a conflict but no further hit to that row.
                bool more_hits = false;
                bool bank_conflict = false;
                // In the source-aligned experiment the selection window
                // bounds only which request can be serviced next. gem5's
                // open_adaptive page policy scans the complete admitted read
                // queue after that selection. The production-compatible path
                // retains the historical bounded scan until this experiment
                // passes both the isolated memory and full-suite gates.
                // The independently gated source-alignment path mirrors gem5:
                // resolve the row-access cap first and skip adaptive policy
                // if it already requested auto-precharge. The disabled path
                // preserves the historical comparison behavior.
                if (!result.row_cap_precharged ||
                    !config_.frfcfs_row_cap_single_precharge) {
                    const auto legacy_scan_count =
                        std::min<std::size_t>(candidate_count,
                                             pending.size());
                    const auto scan_count =
                        config_.frfcfs_full_queue_page_policy
                            ? pending.size()
                            : legacy_scan_count;
                    channel_batch.page_policy_scanned_requests +=
                        scan_count;
                    for (std::size_t index = 0;
                         index < scan_count; ++index) {
                        const auto& queued = pending[index];
                        const auto& queued_address = queued.address;
                        if (queued_address.flat_bank != address.flat_bank) {
                            continue;
                        }
                        const bool same_row =
                            queued_address.row == address.row;
                        more_hits = more_hits || same_row;
                        bank_conflict = bank_conflict || !same_row;
                        if (index >= legacy_scan_count) {
                            if (same_row) {
                                ++channel_batch.outside_window_row_hits;
                            } else {
                                ++channel_batch
                                      .outside_window_bank_conflicts;
                            }
                        }
                    }
                    if (!more_hits && bank_conflict) {
                        auto_precharge(address, result.command_at);
                        ++channel_batch.adaptive_precharges;
                    }
                }
            }
        };
        if (parallel_for_channels) {
            parallel_for_channels(config_.channels, schedule_channel);
        } else {
            for (std::uint32_t channel = 0;
                 channel < config_.channels; ++channel) {
                schedule_channel(channel);
            }
        }
        // Channel state is disjoint, but the externally visible service
        // order remains the historical channel-major order.  This merge is
        // therefore bit-for-bit identical to the serial implementation.
        for (const auto& channel_batch : channel_results) {
            batch.service_order.insert(
                batch.service_order.end(),
                channel_batch.service_order.begin(),
                channel_batch.service_order.end());
            batch.row_hits += channel_batch.row_hits;
            batch.row_misses += channel_batch.row_misses;
            batch.reordered_requests +=
                channel_batch.reordered_requests;
            batch.max_pending = std::max(
                batch.max_pending, channel_batch.max_pending);
            batch.max_admitted_pending = std::max(
                batch.max_admitted_pending,
                channel_batch.max_admitted_pending);
            batch.saturated_selections +=
                channel_batch.saturated_selections;
            batch.page_policy_scanned_requests +=
                channel_batch.page_policy_scanned_requests;
            batch.outside_window_row_hits +=
                channel_batch.outside_window_row_hits;
            batch.outside_window_bank_conflicts +=
                channel_batch.outside_window_bank_conflicts;
            batch.row_cap_precharges +=
                channel_batch.row_cap_precharges;
            batch.adaptive_precharges +=
                channel_batch.adaptive_precharges;
        }
        return batch;
    }

  private:
    Result access_read_decoded(std::uint64_t arrival,
                               const Address& address) {
        if (!config_.separate_write_queue) {
            return access_decoded(arrival, address);
        }
        const auto channel = address.channel;
        auto& queue = write_queues_[channel];
        auto& counters = controller_stats_[channel];
        if (write_mode_[channel] && !queue.empty()) {
            drain_write_burst(channel);
            write_mode_[channel] = false;
            reads_this_turn_[channel] = 0;
        } else if (!queue.empty()) {
            // This is the key read-priority edge: buffered dirty victims do
            // not mutate the bank/data-bus calendar until an explicit drain.
            ++counters.read_bypasses;
        }

        auto result = access_decoded(arrival, address);
        ++reads_this_turn_[channel];
        const auto above_high_watermark =
            queue.size() * 100ull >
            static_cast<std::uint64_t>(
                config_.write_high_threshold_percent) *
                config_.write_buffer_size;
        if (above_high_watermark &&
            reads_this_turn_[channel] >=
                config_.min_reads_per_switch) {
            write_mode_[channel] = true;
            reads_this_turn_[channel] = 0;
            ++counters.high_watermark_switches;
        }
        return result;
    }

    void drain_write_burst(std::uint32_t channel) {
        auto& queue = write_queues_[channel];
        auto& counters = controller_stats_[channel];
        if (queue.empty()) return;
        ++counters.turnarounds;
        const auto drain_count = std::min<std::size_t>(
            config_.min_writes_per_switch, queue.size());
        for (std::size_t drained = 0; drained < drain_count; ++drained) {
            std::size_t selected = 0;
            if (config_.scheduler == "frfcfs") {
                bool has_row_hit = false;
                for (const auto& request : queue) {
                    has_row_hit = has_row_hit ||
                        estimate(request.arrival,
                                 request.address).row_hit;
                }
                auto selected_timing = estimate(
                    queue.front().arrival, queue.front().address);
                for (std::size_t index = 1;
                     index < queue.size(); ++index) {
                    const auto candidate_timing = estimate(
                        queue[index].arrival, queue[index].address);
                    const bool selected_eligible =
                        !has_row_hit || selected_timing.row_hit;
                    const bool candidate_eligible =
                        !has_row_hit || candidate_timing.row_hit;
                    if (candidate_eligible != selected_eligible) {
                        if (candidate_eligible) {
                            selected = index;
                            selected_timing = candidate_timing;
                        }
                        continue;
                    }
                    if (candidate_timing.command_at <
                            selected_timing.command_at ||
                        (candidate_timing.command_at ==
                             selected_timing.command_at &&
                         queue[index].ordinal <
                             queue[selected].ordinal)) {
                        selected = index;
                        selected_timing = candidate_timing;
                    }
                }
            }
            const auto request = queue[selected];
            queue.erase(
                queue.begin() + static_cast<std::ptrdiff_t>(selected));
            const auto result = access_decoded(
                request.arrival, request.address);
            ++counters.writes_drained;
            counters.write_wait_cycles +=
                result.command_at > request.arrival
                    ? result.command_at - request.arrival
                    : 0;
            if (result.row_hit) {
                ++counters.write_row_hits;
            } else {
                ++counters.write_row_misses;
            }
        }
    }

    Address decode(std::uint64_t line) const {
        const auto channel =
            static_cast<std::uint32_t>(line & (config_.channels - 1));
        const auto local_line = line / config_.channels;
        const auto lines_per_row =
            std::max<std::uint64_t>(1, config_.row_bytes / line_size_);
        // gem5 RoRaBaCoCh: the AddrRange removes channel striping first;
        // the controller then removes the entire column/row-buffer field
        // before decoding bank, rank, and finally row.
        auto above_column = local_line / lines_per_row;
        const auto bank = static_cast<std::uint32_t>(
            above_column & (config_.banks_per_channel - 1));
        above_column /= config_.banks_per_channel;
        const auto rank = static_cast<std::uint32_t>(
            above_column & (config_.ranks_per_channel - 1));
        above_column /= config_.ranks_per_channel;
        const auto flat_rank =
            static_cast<std::size_t>(channel) *
                config_.ranks_per_channel + rank;
        const auto flat_bank =
            flat_rank *
                config_.banks_per_channel + bank;
        const auto bank_group = static_cast<std::uint32_t>(
            bank % config_.bank_groups_per_rank);
        const auto flat_bank_group =
            flat_rank *
                config_.bank_groups_per_rank + bank_group;
        const auto row = above_column;
        return Address{channel, rank, bank, bank_group, flat_rank,
                       flat_bank, flat_bank_group, row};
    }

    Result estimate(std::uint64_t arrival,
                    const Address& address) const {
        const auto& state = banks_[address.flat_bank];
        const bool row_hit =
            state.row_valid && state.open_row == address.row;
        const auto intrinsic_command_at = arrival +
            (row_hit ? 0u : config_.t_rcd +
                                (state.row_valid ? config_.t_rp : 0u));
        std::uint64_t activation_at = state.activation_at;
        std::uint64_t bank_command_at = 0;
        if (row_hit) {
            bank_command_at = std::max(arrival, state.ready);
        } else if (state.row_valid) {
            const auto precharge_at = std::max(
                {arrival, state.ready, state.precharge_ready});
            activation_at = std::max(
                precharge_at + config_.t_rp,
                state.activate_ready);
            activation_at = std::max(
                activation_at, activation_window_ready(address));
            bank_command_at = activation_at + config_.t_rcd;
        } else {
            activation_at = std::max(
                {arrival, state.ready, state.activate_ready,
                 activation_window_ready(address)});
            bank_command_at = activation_at + config_.t_rcd;
        }

        // gem5 updates every bank's RD-allowed calendar after a column
        // command: tBURST across bank groups, tCCD_L within one group, and
        // tBURST+tCS when switching rank. Keeping this command calendar
        // separate from data return is essential: delaying data on the bus
        // must also move the command that seeds a later tCCD dependency.
        auto command_at = std::max(
            {bank_command_at,
             bank_group_ready_[address.flat_bank_group],
             channel_command_ready_[address.channel]});
        const auto last_rank = channel_last_rank_[address.channel];
        if (last_rank != std::numeric_limits<std::uint32_t>::max() &&
            last_rank != address.rank) {
            command_at = std::max(
                command_at,
                channel_last_command_[address.channel] +
                    config_.burst_cycles + config_.t_cs);
        }
        const auto data_ready = command_at + config_.t_cl;
        const auto bus_start = std::max(
            data_ready, channel_ready_[address.channel]);
        const auto dram_completion = bus_start + config_.burst_cycles;
        const auto completion = dram_completion +
            config_.frontend_latency + config_.backend_latency;
        return Result{completion,
                      (bank_command_at - intrinsic_command_at) +
                          (command_at - bank_command_at) +
                          (bus_start - data_ready),
                      command_at, activation_at, bank_command_at,
                      row_hit};
    }

    Result access_decoded(std::uint64_t arrival,
                          const Address& address) {
        auto result = estimate(arrival, address);
        auto& state = banks_[address.flat_bank];
        const auto dram_completion = result.completion -
            config_.frontend_latency - config_.backend_latency;
        if (!result.row_hit) {
            state.activation_at = result.activation_at;
            state.precharge_ready =
                result.activation_at + config_.t_ras;
            const auto rank_bank_begin =
                address.flat_rank * config_.banks_per_channel;
            for (std::uint32_t bank = 0;
                 bank < config_.banks_per_channel; ++bank) {
                const auto bank_group =
                    bank % config_.bank_groups_per_rank;
                const auto spacing =
                    bank_group == address.bank_group
                        ? (config_.t_rrd_l == 0
                               ? config_.t_rrd
                               : config_.t_rrd_l)
                        : config_.t_rrd;
                auto& peer = banks_[rank_bank_begin + bank];
                peer.activate_ready = std::max(
                    peer.activate_ready,
                    result.activation_at + spacing);
            }
            if (config_.activation_limit != 0) {
                auto& activations =
                    rank_activations_[address.flat_rank];
                activations.push_back(result.activation_at);
                while (activations.size() >
                       config_.activation_limit) {
                    activations.pop_front();
                }
            }
        }
        state.ready = result.command_at + config_.burst_cycles;
        state.precharge_ready = std::max(
            state.precharge_ready,
            result.command_at +
                std::max(config_.burst_cycles, config_.t_rtp));
        bank_group_ready_[address.flat_bank_group] =
            result.command_at + config_.t_ccd_l;
        channel_command_ready_[address.channel] =
            result.command_at + config_.burst_cycles;
        channel_last_command_[address.channel] = result.command_at;
        channel_last_rank_[address.channel] = address.rank;
        state.open_row = address.row;
        state.row_valid = true;
        if (!result.row_hit) state.row_accesses = 0;
        ++state.row_accesses;
        channel_ready_[address.channel] = dram_completion;
        if (config_.max_accesses_per_row != 0 &&
            state.row_accesses >= config_.max_accesses_per_row) {
            auto_precharge(address, result.command_at);
            result.row_cap_precharged = true;
        }
        return result;
    }

    void auto_precharge(const Address& address,
                        std::uint64_t command_at) {
        auto& state = banks_[address.flat_bank];
        const auto precharge_at = std::max(
            {state.ready,
             state.precharge_ready,
             command_at +
                 std::max(config_.burst_cycles, config_.t_rtp)});
        state.ready = precharge_at + config_.t_rp;
        state.row_valid = false;
        state.row_accesses = 0;
    }

    std::uint64_t activation_window_ready(
        const Address& address) const {
        if (config_.activation_limit == 0 || config_.t_xaw == 0) {
            return 0;
        }
        const auto& activations =
            rank_activations_[address.flat_rank];
        if (activations.size() < config_.activation_limit) return 0;
        return activations.front() + config_.t_xaw;
    }

    struct Bank {
        std::uint64_t ready = 0;
        std::uint64_t open_row = 0;
        std::uint64_t activation_at = 0;
        std::uint64_t activate_ready = 0;
        std::uint64_t precharge_ready = 0;
        std::uint32_t row_accesses = 0;
        bool row_valid = false;
    };

    DramConfig config_;
    std::uint32_t line_size_ = 64;
    std::uint64_t capacity_lines_ = 0;
    std::vector<Bank> banks_;
    std::vector<std::uint64_t> bank_group_ready_;
    std::vector<std::deque<std::uint64_t>> rank_activations_;
    std::vector<std::uint64_t> channel_command_ready_;
    std::vector<std::uint64_t> channel_last_command_;
    std::vector<std::uint32_t> channel_last_rank_;
    std::vector<std::uint64_t> channel_ready_;
    std::vector<std::vector<BufferedWrite>> write_queues_;
    std::vector<bool> write_mode_;
    std::vector<std::uint32_t> reads_this_turn_;
    std::vector<ControllerStats> controller_stats_;
    std::uint64_t next_write_ordinal_ = 0;
};

struct DirectoryEntry {
    std::array<std::uint64_t, 4> sharers{};
    std::int16_t owner = -1;
    bool modified = false;

    bool has(std::uint32_t core) const {
        return (sharers[core / 64] & (1ull << (core % 64))) != 0;
    }
    void add(std::uint32_t core) {
        sharers[core / 64] |= 1ull << (core % 64);
    }
    void remove(std::uint32_t core) {
        sharers[core / 64] &= ~(1ull << (core % 64));
        if (owner == static_cast<std::int16_t>(core)) {
            owner = -1;
            modified = false;
        }
    }
    bool empty() const {
        return std::all_of(sharers.begin(), sharers.end(),
                           [](std::uint64_t word) { return word == 0; });
    }
};

enum class SharedTimingPath {
    kLocal,
    kPermissionUpgrade,
    kRemoteSupply,
    kLlcHit,
    kMergedMemory,
    kMemory,
};

// Cache/directory state is committed by the canonical path.  This compact
// descriptor is sufficient to replay only queue timing when a corrected
// arrival schedule is certified to preserve every non-commuting path order.
struct SharedTimingDescriptor {
    SharedTimingPath path = SharedTimingPath::kLocal;
    std::uint64_t line = 0;
    std::uint64_t canonical_queue_cycles = 0;
    std::uint32_t cha = 0;
    std::uint32_t local_latency = 0;
    // Canonical cache/CHA replay supplies these memory-stage boundary times.
    // The interval FR-FCFS repair may change only MSHR admission and DRAM
    // service, leaving the certified cache/directory path and CHA order intact.
    std::uint64_t canonical_tag_ready = 0;
    std::uint64_t canonical_dram_arrival = 0;
    std::uint64_t canonical_dram_completion = 0;
    std::uint64_t canonical_fill_completion = 0;
    std::uint64_t canonical_memory_queue_cycles = 0;
    bool replayable = true;
    bool uncore_request = false;
};

struct SharedAccessResult {
    HitLevel level = HitLevel::kUnknown;
    std::uint64_t completion = 0;
    bool uncore_request = false;
    SharedTimingDescriptor timing;
};

class SharedSystem {
  public:
    struct TimingState {
        DramModel dram;
        std::vector<std::uint64_t> cha_ready;
        std::vector<std::uint64_t> llc_mshr_ready;
        std::unordered_map<std::uint64_t, std::uint64_t>
            llc_transient_ready;
    };

    struct TimingReplayResult {
        std::uint64_t completion = 0;
        std::uint64_t queue_cycles = 0;
        std::uint32_t cha = 0;
    };

    struct Transaction {
        CacheTransaction llc;
        CacheCounters llc_counters;
        std::vector<ChaCounters> cha_counters;
        DramModel dram;
        std::vector<std::uint64_t> cha_ready;
        std::vector<std::uint64_t> llc_mshr_ready;
        std::unordered_map<std::uint64_t,
                           std::optional<std::uint64_t>>
            llc_transient_before;
        std::priority_queue<
            std::pair<std::uint64_t, std::uint64_t>,
            std::vector<std::pair<std::uint64_t, std::uint64_t>>,
            std::greater<>> llc_transient_expiry;
        std::vector<PrivateTransaction> private_caches;
        std::unordered_map<std::uint64_t,
                           std::optional<DirectoryEntry>> directory_before;
    };

    SharedSystem(const SimulatorConfig& config,
                 std::vector<std::unique_ptr<PrivateHierarchy>>& private_caches,
                 SimulationStats& stats)
        : config_(config),
          private_caches_(private_caches),
          stats_(stats),
          llc_(config.llc),
          dram_(config.dram, config.llc.line_size),
          cha_ready_(config.cha_count),
          // Ruby's L3 controllers each own their own TBE pool.  Keep the
          // FastSim miss lanes partitioned by home slice as well; treating
          // this as one machine-wide pool creates an artificial C16/C32
          // serialization point.
          llc_mshr_ready_(static_cast<std::size_t>(config.cha_count) *
                          config.llc_mshrs) {}

    Transaction begin_transaction(std::size_t event_hint = 0) {
        Transaction transaction{
            llc_.begin_transaction(event_hint), stats_.llc, stats_.cha, dram_,
            cha_ready_, llc_mshr_ready_, {}, llc_transient_expiry_, {}, {}};
        transaction.private_caches.reserve(private_caches_.size());
        const auto per_core_hint = private_caches_.empty()
            ? event_hint
            : (event_hint + private_caches_.size() - 1) /
                  private_caches_.size();
        for (auto& cache : private_caches_) {
            transaction.private_caches.push_back(
                cache->begin_transaction(per_core_hint));
        }
        transaction.directory_before.reserve(event_hint);
        transaction.llc_transient_before.reserve(event_hint);
        return transaction;
    }

    TimingState capture_timing_state() const {
        return TimingState{
            dram_, cha_ready_, llc_mshr_ready_, llc_transient_ready_};
    }

    void install_timing_state(TimingState state) {
        dram_ = std::move(state.dram);
        cha_ready_ = std::move(state.cha_ready);
        llc_mshr_ready_ = std::move(state.llc_mshr_ready);
        llc_transient_ready_ = std::move(state.llc_transient_ready);
    }

    void install_memory_timing(
        DramModel dram, std::vector<std::uint64_t> llc_mshr_ready,
        const std::vector<std::array<std::uint64_t, 3>>& fill_remaps) {
        dram_ = std::move(dram);
        llc_mshr_ready_ = std::move(llc_mshr_ready);
        for (const auto& remap : fill_remaps) {
            const auto it = llc_transient_ready_.find(remap[0]);
            if (it != llc_transient_ready_.end() &&
                it->second == remap[1]) {
                it->second = remap[2];
                llc_transient_expiry_.push({remap[2], remap[0]});
            }
        }
    }

    TimingReplayResult replay_timing(
        const SharedTimingDescriptor& descriptor,
        std::uint64_t issue_cycle, TimingState& state) const {
        if (!descriptor.replayable) {
            throw std::logic_error(
                "attempted to replay an uncertified shared timing path");
        }
        if (!descriptor.uncore_request) {
            return TimingReplayResult{
                issue_cycle + descriptor.local_latency, 0,
                descriptor.cha};
        }

        const auto cha = descriptor.cha;
        const auto arrival = issue_cycle + config_.noc_one_way_latency;
        const auto cha_start = std::max(arrival, state.cha_ready[cha]);
        std::uint64_t queue_cycles = cha_start - arrival;
        state.cha_ready[cha] = cha_start + config_.llc_service_cycles;
        const auto tag_ready = cha_start + config_.llc.hit_latency;

        if (descriptor.path == SharedTimingPath::kPermissionUpgrade) {
            return TimingReplayResult{
                state.cha_ready[cha] + config_.noc_one_way_latency,
                queue_cycles, cha};
        }
        if (descriptor.path == SharedTimingPath::kRemoteSupply) {
            return TimingReplayResult{
                tag_ready + config_.noc_one_way_latency +
                    config_.l2.hit_latency,
                queue_cycles, cha};
        }
        if (descriptor.path == SharedTimingPath::kLlcHit) {
            return TimingReplayResult{
                tag_ready + config_.noc_one_way_latency,
                queue_cycles, cha};
        }
        if (descriptor.path == SharedTimingPath::kMergedMemory) {
            const auto fill = state.llc_transient_ready.find(
                descriptor.line);
            const auto fill_ready =
                fill == state.llc_transient_ready.end()
                    ? descriptor.canonical_fill_completion
                    : fill->second;
            return TimingReplayResult{
                std::max(tag_ready, fill_ready) +
                    config_.noc_one_way_latency,
                queue_cycles, cha};
        }
        if (descriptor.path != SharedTimingPath::kMemory) {
            throw std::logic_error("invalid uncore timing path");
        }

        const auto mshr_offset =
            static_cast<std::size_t>(cha) * config_.llc_mshrs;
        const auto mshr_begin =
            state.llc_mshr_ready.begin() +
            static_cast<std::ptrdiff_t>(mshr_offset);
        const auto mshr_end =
            mshr_begin + static_cast<std::ptrdiff_t>(config_.llc_mshrs);
        if (mshr_begin == mshr_end) {
            throw std::logic_error("LLC has no timing-replay MSHR entries");
        }
        const auto memory_path_ready =
            tag_ready + config_.directory_memory_latency;
        const auto mshr_ready = *mshr_begin;
        const auto dram_arrival = std::max(
            memory_path_ready, mshr_ready);
        queue_cycles += dram_arrival - memory_path_ready;
        const auto dram_result =
            state.dram.access(dram_arrival, descriptor.line);
        queue_cycles += dram_result.queue_cycles;
        const auto fill_completion = dram_result.completion +
            config_.llc_fill_response_latency;
        std::pop_heap(mshr_begin, mshr_end, std::greater<>{});
        *(mshr_end - 1) = fill_completion;
        std::push_heap(mshr_begin, mshr_end, std::greater<>{});
        state.llc_transient_ready[descriptor.line] =
            fill_completion;
        return TimingReplayResult{
            fill_completion + config_.noc_one_way_latency,
            queue_cycles, cha};
    }

    void restore(const Transaction& transaction) {
        llc_.restore(transaction.llc);
        stats_.llc = transaction.llc_counters;
        stats_.cha = transaction.cha_counters;
        dram_ = transaction.dram;
        cha_ready_ = transaction.cha_ready;
        llc_mshr_ready_ = transaction.llc_mshr_ready;
        for (const auto& undo : transaction.llc_transient_before) {
            if (undo.second.has_value()) {
                llc_transient_ready_[undo.first] = *undo.second;
            } else {
                llc_transient_ready_.erase(undo.first);
            }
        }
        llc_transient_expiry_ = transaction.llc_transient_expiry;
        for (std::size_t core = 0;
             core < transaction.private_caches.size(); ++core) {
            private_caches_[core]->restore(
                transaction.private_caches[core]);
        }
        for (const auto& undo : transaction.directory_before) {
            if (undo.second.has_value()) {
                directory_[undo.first] = *undo.second;
            } else {
                directory_.erase(undo.first);
            }
        }
    }

    std::array<std::uint64_t, 4> sharers_of(
        std::uint64_t line) const {
        const auto it = directory_.find(line);
        return it == directory_.end()
                   ? std::array<std::uint64_t, 4>{}
                   : it->second.sharers;
    }

    // Apply the functional cache-residency effect of a kernel page fill
    // without advancing target time or charging user/kernel PMU. The normal
    // demand immediately following this seed is still replayed and counted.
    void seed_page_fault_page(
        std::uint32_t core, std::uint64_t first_line,
        std::uint32_t line_count, Transaction* transaction = nullptr) {
        if (core >= private_caches_.size()) {
            throw std::out_of_range("page-fault seed core is out of range");
        }
        for (std::uint32_t offset = 0; offset < line_count; ++offset) {
            const auto line = first_line + offset;
            if (line < first_line) {
                throw std::overflow_error("page-fault seed line overflow");
            }

            if (config_.coherence) {
                snapshot_directory(line, transaction);
                auto& entry = directory_[line];
                for_each_sharer(entry, [&](std::uint32_t sharer) {
                    if (sharer == core) return;
                    auto* private_transaction =
                        transaction == nullptr
                            ? nullptr
                            : &transaction->private_caches[sharer];
                    private_caches_[sharer]->invalidate(
                        line, private_transaction);
                });
                entry.sharers = {};
                entry.add(core);
                entry.owner = static_cast<std::int16_t>(core);
                entry.modified = true;
            }

            CoreCounters ignored_private;
            auto* private_transaction =
                transaction == nullptr
                    ? nullptr
                    : &transaction->private_caches[core];
            const auto private_result = private_caches_[core]->access(
                line, true, ignored_private, private_transaction);
            if (private_result.l2_evicted) {
                state_only_private_evict(
                    core, private_result.l2_evicted_line,
                    private_result.l2_evicted_dirty, transaction);
            }
            state_only_llc_insert(line, false, transaction);
        }
    }

    bool private_evict(std::uint32_t core, std::uint64_t line, bool dirty,
                       std::uint64_t issue_cycle,
                       Transaction* transaction = nullptr) {
        snapshot_directory(line, transaction);
        const auto it = directory_.find(line);
        if (it != directory_.end()) {
            it->second.remove(core);
            if (it->second.empty() && !llc_.contains(line)) {
                directory_.erase(it);
            }
        }
        bool issued_dirty_dram_write = false;
        if (dirty) {
            CacheCounters ignored;
            auto* llc_transaction = transaction == nullptr
                                        ? nullptr
                                        : &transaction->llc;
            const auto llc_result = llc_.access(
                line, true, ignored, llc_transaction);
            if (llc_result.evicted) {
                issued_dirty_dram_write = handle_llc_eviction(
                    llc_result.evicted_line, llc_result.evicted_dirty,
                    issue_cycle + config_.noc_one_way_latency,
                    cha_of(llc_result.evicted_line), transaction);
            }
        }
        return issued_dirty_dram_write;
    }

    SharedAccessResult access(std::uint32_t core, std::uint64_t line,
                              bool write, HitLevel private_level,
                              std::uint64_t issue_cycle,
                              Transaction* transaction = nullptr) {
        expire_transients(issue_cycle, transaction);
        const bool private_miss =
            private_level == HitLevel::kLlc ||
            private_level == HitLevel::kUnknown;

        if (!config_.coherence && !private_miss) {
            return local_result(private_level, issue_cycle);
        }
        if (!write && !private_miss) {
            return local_result(private_level, issue_cycle);
        }
        if (config_.coherence && write && !private_miss) {
            const auto it = directory_.find(line);
            if (it != directory_.end() &&
                it->second.owner == static_cast<std::int16_t>(core)) {
                return local_result(private_level, issue_cycle);
            }
        }

        snapshot_directory(line, transaction);
        auto& entry = directory_[line];
        const bool permission_upgrade =
            config_.coherence && write && !private_miss &&
            entry.owner != static_cast<std::int16_t>(core);

        const auto cha = cha_of(line);
        auto& counters = stats_.cha[cha];
        const auto queue_cycles_before = counters.queue_cycles;
        SharedTimingDescriptor timing;
        timing.line = line;
        timing.cha = cha;
        timing.uncore_request = true;
        ++counters.requests;
        if (write) {
            ++counters.writes;
        } else {
            ++counters.reads;
        }
        const auto arrival = issue_cycle + config_.noc_one_way_latency;
        const auto cha_start = std::max(arrival, cha_ready_[cha]);
        counters.queue_cycles += cha_start - arrival;
        cha_ready_[cha] = cha_start + config_.llc_service_cycles;
        const auto tag_ready = cha_start + config_.llc.hit_latency;

        const auto transient = llc_transient_ready_.find(line);
        const bool merged_memory =
            transient != llc_transient_ready_.end() &&
            tag_ready < transient->second;

        bool remote_supply = false;
        if (config_.coherence && write) {
            for_each_sharer(entry, [&](std::uint32_t sharer) {
                if (sharer != core) {
                    auto* private_transaction =
                        transaction == nullptr
                            ? nullptr
                            : &transaction->private_caches[sharer];
                    if (private_caches_[sharer]->invalidate(
                            line, private_transaction)) {
                        ++counters.invalidations;
                    }
                }
            });
            if (entry.owner >= 0 &&
                entry.owner != static_cast<std::int16_t>(core)) {
                remote_supply = true;
            }
            if (permission_upgrade) {
                ++counters.upgrades;
            }
            entry.sharers = {};
            entry.add(core);
            entry.owner = static_cast<std::int16_t>(core);
            entry.modified = true;
        } else if (config_.coherence) {
            if (entry.owner >= 0 &&
                entry.owner != static_cast<std::int16_t>(core)) {
                remote_supply = true;
                entry.add(static_cast<std::uint32_t>(entry.owner));
                entry.owner = -1;
                entry.modified = false;
            } else if (!merged_memory && !entry.empty() && !entry.has(core) &&
                       !llc_.contains(line)) {
                remote_supply = true;
            }
            entry.add(core);
        }

        if (permission_upgrade) {
            SharedAccessResult result;
            result.level = private_level;
            result.completion =
                cha_ready_[cha] + config_.noc_one_way_latency;
            result.uncore_request = true;
            timing.path = SharedTimingPath::kPermissionUpgrade;
            timing.canonical_queue_cycles =
                counters.queue_cycles - queue_cycles_before;
            result.timing = timing;
            return result;
        }

        if (merged_memory && !remote_supply) {
            ++stats_.llc.accesses;
            ++stats_.llc.misses;
            ++counters.llc_misses;
            ++counters.llc_merged_misses;
            const auto wait_cycles = transient->second - tag_ready;
            counters.llc_merged_wait_cycles += wait_cycles;
            counters.llc_merged_wait_max_cycles = std::max(
                counters.llc_merged_wait_max_cycles, wait_cycles);

            SharedAccessResult result;
            result.level = HitLevel::kMemory;
            result.completion =
                transient->second + config_.noc_one_way_latency;
            result.uncore_request = true;
            timing.path = SharedTimingPath::kMergedMemory;
            timing.canonical_tag_ready = tag_ready;
            timing.canonical_fill_completion = transient->second;
            timing.canonical_queue_cycles =
                counters.queue_cycles - queue_cycles_before;
            result.timing = timing;
            return result;
        }

        auto* llc_transaction = transaction == nullptr
                                    ? nullptr
                                    : &transaction->llc;
        const auto llc_result = llc_.access(
            line, write, stats_.llc, llc_transaction);
        if (llc_result.hit) {
            ++counters.llc_hits;
        } else {
            ++counters.llc_misses;
        }
        bool issued_dirty_dram_write = false;
        if (llc_result.evicted) {
            issued_dirty_dram_write = handle_llc_eviction(
                llc_result.evicted_line, llc_result.evicted_dirty,
                tag_ready, cha_of(llc_result.evicted_line), transaction);
        }

        SharedAccessResult result;
        result.uncore_request = true;
        if (remote_supply) {
            ++counters.remote_supplies;
            result.level = HitLevel::kRemote;
            result.completion =
                tag_ready + config_.noc_one_way_latency +
                config_.l2.hit_latency;
            timing.path = SharedTimingPath::kRemoteSupply;
            timing.replayable = !issued_dirty_dram_write;
            timing.canonical_queue_cycles =
                counters.queue_cycles - queue_cycles_before;
            result.timing = timing;
            return result;
        }
        if (llc_result.hit) {
            result.level = HitLevel::kLlc;
            result.completion =
                tag_ready + config_.noc_one_way_latency;
            timing.path = SharedTimingPath::kLlcHit;
            timing.replayable = !issued_dirty_dram_write;
            timing.canonical_queue_cycles =
                counters.queue_cycles - queue_cycles_before;
            result.timing = timing;
            return result;
        }

        ++counters.dram_reads;
        ++counters.llc_unique_fills;
        const auto mshr_offset =
            static_cast<std::size_t>(cha) * config_.llc_mshrs;
        const auto mshr_begin =
            llc_mshr_ready_.begin() +
            static_cast<std::ptrdiff_t>(mshr_offset);
        const auto mshr_end =
            mshr_begin + static_cast<std::ptrdiff_t>(config_.llc_mshrs);
        if (mshr_begin == mshr_end) {
            throw std::logic_error("LLC has no MSHR entries");
        }
        // Every slice range is maintained as a min-heap.  TBE counts in the
        // captured Ruby setup are 256, so a linear scan on every miss would
        // unnecessarily make host cost proportional to a target parameter.
        const auto memory_path_ready =
            tag_ready + config_.directory_memory_latency;
        const auto mshr_ready = *mshr_begin;
        const auto dram_arrival = std::max(
            memory_path_ready, mshr_ready);
        counters.queue_cycles += dram_arrival - memory_path_ready;
        const auto dram_result = dram_.access(dram_arrival, line);
        counters.queue_cycles += dram_result.queue_cycles;
        const auto fill_completion = dram_result.completion +
            config_.llc_fill_response_latency;
        std::pop_heap(mshr_begin, mshr_end, std::greater<>{});
        *(mshr_end - 1) = fill_completion;
        std::push_heap(mshr_begin, mshr_end, std::greater<>{});
        snapshot_transient(line, transaction);
        llc_transient_ready_[line] = fill_completion;
        llc_transient_expiry_.push({fill_completion, line});
        result.level = HitLevel::kMemory;
        result.completion =
            fill_completion + config_.noc_one_way_latency;
        timing.path = SharedTimingPath::kMemory;
        timing.replayable = !issued_dirty_dram_write;
        timing.canonical_tag_ready = memory_path_ready;
        timing.canonical_dram_arrival = dram_arrival;
        timing.canonical_dram_completion = dram_result.completion;
        timing.canonical_fill_completion = fill_completion;
        timing.canonical_memory_queue_cycles =
            (dram_arrival - memory_path_ready) +
            dram_result.queue_cycles;
        timing.canonical_queue_cycles =
            counters.queue_cycles - queue_cycles_before;
        result.timing = timing;
        return result;
    }

  private:
    void state_only_llc_evict(
        std::uint64_t line, Transaction* transaction) {
        snapshot_directory(line, transaction);
        const auto it = directory_.find(line);
        if (it == directory_.end()) return;
        if (config_.inclusive_llc) {
            for_each_sharer(it->second, [&](std::uint32_t sharer) {
                auto* private_transaction =
                    transaction == nullptr
                        ? nullptr
                        : &transaction->private_caches[sharer];
                private_caches_[sharer]->invalidate(
                    line, private_transaction);
            });
            directory_.erase(it);
        } else if (it->second.empty()) {
            directory_.erase(it);
        }
    }

    void state_only_llc_insert(
        std::uint64_t line, bool dirty, Transaction* transaction) {
        CacheCounters ignored;
        auto* llc_transaction =
            transaction == nullptr ? nullptr : &transaction->llc;
        const auto result = llc_.access(
            line, dirty, ignored, llc_transaction);
        if (result.evicted) {
            state_only_llc_evict(result.evicted_line, transaction);
        }
    }

    void state_only_private_evict(
        std::uint32_t core, std::uint64_t line, bool dirty,
        Transaction* transaction) {
        if (config_.coherence) {
            snapshot_directory(line, transaction);
            const auto it = directory_.find(line);
            if (it != directory_.end()) {
                it->second.remove(core);
                if (it->second.empty() && !llc_.contains(line)) {
                    directory_.erase(it);
                }
            }
        }
        if (dirty) state_only_llc_insert(line, true, transaction);
    }

    void expire_transients(std::uint64_t cycle,
                           Transaction* transaction) {
        while (!llc_transient_expiry_.empty() &&
               llc_transient_expiry_.top().first <= cycle) {
            const auto [completion, line] =
                llc_transient_expiry_.top();
            llc_transient_expiry_.pop();
            const auto it = llc_transient_ready_.find(line);
            if (it != llc_transient_ready_.end() &&
                it->second == completion) {
                snapshot_transient(line, transaction);
                llc_transient_ready_.erase(it);
            }
        }
    }

    void snapshot_transient(std::uint64_t line,
                            Transaction* transaction) {
        if (transaction == nullptr ||
            transaction->llc_transient_before.find(line) !=
                transaction->llc_transient_before.end()) {
            return;
        }
        const auto it = llc_transient_ready_.find(line);
        if (it == llc_transient_ready_.end()) {
            transaction->llc_transient_before.emplace(
                line, std::nullopt);
        } else {
            transaction->llc_transient_before.emplace(line, it->second);
        }
    }

    void snapshot_directory(std::uint64_t line,
                            Transaction* transaction) {
        if (transaction == nullptr ||
            transaction->directory_before.find(line) !=
                transaction->directory_before.end()) {
            return;
        }
        const auto it = directory_.find(line);
        if (it == directory_.end()) {
            transaction->directory_before.emplace(line, std::nullopt);
        } else {
            transaction->directory_before.emplace(line, it->second);
        }
    }

    SharedAccessResult local_result(HitLevel level,
                                    std::uint64_t issue_cycle) const {
        const auto latency =
            level == HitLevel::kL1 ? config_.l1d.hit_latency
                                   : config_.l2.hit_latency;
        SharedTimingDescriptor timing;
        timing.path = SharedTimingPath::kLocal;
        timing.local_latency = latency;
        return SharedAccessResult{
            level, issue_cycle + latency, false, timing};
    }

    std::uint32_t cha_of(std::uint64_t line) const {
        const auto mixed =
            config_.cha_xor_hash
                ? line ^ (line >> 11) ^ (line >> 23)
                : line;
        return static_cast<std::uint32_t>(
            mixed & (config_.cha_count - 1));
    }

    template <typename Callback>
    void for_each_sharer(const DirectoryEntry& entry,
                         Callback&& callback) {
        for (std::uint32_t group = 0; group < entry.sharers.size(); ++group) {
            auto word = entry.sharers[group];
            while (word != 0) {
                const auto bit =
                    static_cast<std::uint32_t>(__builtin_ctzll(word));
                const auto core = group * 64 + bit;
                if (core < private_caches_.size()) callback(core);
                word &= word - 1;
            }
        }
    }

    bool handle_llc_eviction(std::uint64_t line, bool dirty,
                             std::uint64_t issue_cycle,
                             std::uint32_t source_cha,
                             Transaction* transaction = nullptr) {
        snapshot_directory(line, transaction);
        auto it = directory_.find(line);
        if (it != directory_.end() && config_.inclusive_llc) {
            for_each_sharer(it->second, [&](std::uint32_t sharer) {
                auto* private_transaction =
                    transaction == nullptr
                        ? nullptr
                        : &transaction->private_caches[sharer];
                if (private_caches_[sharer]->invalidate(
                        line, private_transaction)) {
                    ++stats_.cha[source_cha].invalidations;
                }
            });
            directory_.erase(it);
        } else if (it != directory_.end() && it->second.empty()) {
            directory_.erase(it);
        }
        if (dirty) {
            ++stats_.cha[source_cha].dram_writes;
            if (config_.dram.separate_write_queue) {
                dram_.enqueue_write(issue_cycle, line);
            } else {
                const auto result = dram_.access(issue_cycle, line);
                stats_.cha[source_cha].queue_cycles +=
                    result.queue_cycles;
            }
        }
        return dirty;
    }

  public:
    void reset_dram_controller_stats() {
        dram_.reset_controller_stats();
    }

    void export_dram_controller_stats(SimulationStats& output) const {
        const auto counters = dram_.controller_stats();
        output.dram_write_queue_enqueues = counters.write_enqueues;
        output.dram_write_queue_drained = counters.writes_drained;
        output.dram_write_queue_read_bypasses = counters.read_bypasses;
        output.dram_write_queue_high_watermark_switches =
            counters.high_watermark_switches;
        output.dram_write_queue_forced_capacity_drains =
            counters.forced_capacity_drains;
        output.dram_write_queue_turnarounds = counters.turnarounds;
        output.dram_write_queue_row_hits = counters.write_row_hits;
        output.dram_write_queue_row_misses = counters.write_row_misses;
        output.dram_write_queue_wait_cycles = counters.write_wait_cycles;
        output.dram_write_queue_max_pending = counters.max_pending;
        output.dram_write_queue_pending_initial =
            counters.pending_initial;
        output.dram_write_queue_pending_final = counters.pending_final;
    }

  private:
    const SimulatorConfig& config_;
    std::vector<std::unique_ptr<PrivateHierarchy>>& private_caches_;
    SimulationStats& stats_;
    SetAssociativeCache llc_;
    DramModel dram_;
    std::vector<std::uint64_t> cha_ready_;
    std::vector<std::uint64_t> llc_mshr_ready_;
    std::unordered_map<std::uint64_t, std::uint64_t>
        llc_transient_ready_;
    std::priority_queue<
        std::pair<std::uint64_t, std::uint64_t>,
        std::vector<std::pair<std::uint64_t, std::uint64_t>>,
        std::greater<>> llc_transient_expiry_;
    std::unordered_map<std::uint64_t, DirectoryEntry> directory_;
};

struct ChunkMemoryEvent {
    std::uint64_t line = 0;
    std::uint64_t delta_q16 = 0;
    std::uint32_t ordinal = 0;
    std::uint32_t uop_index = 0;
    std::uint32_t lower_bound_latency = 0;
    bool write = false;
    bool atomic = false;
    bool blocks_retirement = false;
    bool page_fault_state_fill = false;
    std::uint64_t page_first_line = 0;
};

struct ChunkUopBound {
    std::array<std::uint32_t, 4> producer_dists{};
    std::array<std::uint8_t, kTrackedRegisterClasses>
        destination_class_counts{};
    std::uint64_t sequence = 0;
    std::uint64_t rename_q16 = 0;
    std::uint64_t dispatch_q16 = 0;
    std::uint64_t issue_q16 = 0;
    std::uint64_t completion_q16 = 0;
    std::uint64_t retire_q16 = 0;
    std::uint32_t first_memory = 0;
    std::uint32_t fu_occupancy_cycles = 1;
    std::uint16_t memory_count = 0;
    IntervalFuPool fu_pool = IntervalFuPool::kInteger;
    bool memory_read = false;
    bool memory_write = false;
    bool dispatch_load = false;
    bool dispatch_store = false;
    bool serialize_before = false;
    bool serialize_after = false;
};

// A checkpoint-local, event-driven resource calendar. It is instantiated only
// when a response causal cone enters the checkpoint. UOPs keep their certified
// lower-bound issue floor but can be inserted at non-monotonic cycles, so an
// independent younger UOP may still bypass a delayed older UOP. Work is O(UOPs
// plus actual collisions), with no target-cycle scan or workload-derived knob.
class SparseIssueResourceCalendar {
  public:
    explicit SparseIssueResourceCalendar(const SimulatorConfig& config)
        : issue_width_(config.issue_width),
          writeback_width_(config.writeback_width),
          load_ports_(config.cache_load_ports),
          store_ports_(config.cache_store_ports),
          fu_capacity_{
              config.integer_alu_units,
              config.integer_multiply_units,
              config.float_simple_units,
              config.float_complex_units,
              config.simd_units,
              config.predicate_units,
              config.memory_units,
              config.system_units} {}

    std::uint64_t allocate_issue(const ChunkUopBound& bound,
                                 std::uint64_t earliest) {
        auto cycle = earliest;
        while (true) {
            if (count(issue_, cycle) >= issue_width_ ||
                (bound.memory_write &&
                 count(store_port_, cycle) >= store_ports_) ||
                (!bound.memory_write && bound.memory_read &&
                 count(load_port_, cycle) >= load_ports_) ||
                !fu_available(bound, cycle)) {
                ++cycle;
                continue;
            }
            increment(issue_, cycle);
            reserve_fu(bound, cycle);
            reserve_port(bound, cycle);
            return cycle;
        }
    }

    std::uint64_t allocate_writeback(std::uint64_t earliest) {
        auto cycle = earliest;
        while (count(writeback_, cycle) >= writeback_width_) ++cycle;
        increment(writeback_, cycle);
        return cycle;
    }

  private:
    using Counts = std::unordered_map<std::uint64_t, std::uint32_t>;

    static std::uint32_t count(const Counts& counts,
                               std::uint64_t cycle) {
        const auto found = counts.find(cycle);
        return found == counts.end() ? 0u : found->second;
    }

    static void increment(Counts& counts, std::uint64_t cycle) {
        ++counts[cycle];
    }

    std::size_t pool_index(const ChunkUopBound& bound) const {
        const auto pool = static_cast<std::size_t>(bound.fu_pool);
        if (pool >= fu_.size()) {
            throw std::logic_error("invalid interval FU pool");
        }
        return pool;
    }

    bool fu_available(const ChunkUopBound& bound,
                      std::uint64_t issue_cycle) const {
        const auto pool = pool_index(bound);
        const auto occupancy = std::max(1u, bound.fu_occupancy_cycles);
        for (std::uint32_t offset = 0; offset < occupancy; ++offset) {
            if (count(fu_[pool], issue_cycle + offset) >=
                fu_capacity_[pool]) {
                return false;
            }
        }
        return true;
    }

    void reserve_fu(const ChunkUopBound& bound,
                    std::uint64_t issue_cycle) {
        const auto pool = pool_index(bound);
        const auto occupancy = std::max(1u, bound.fu_occupancy_cycles);
        for (std::uint32_t offset = 0; offset < occupancy; ++offset) {
            increment(fu_[pool], issue_cycle + offset);
        }
    }

    void reserve_port(const ChunkUopBound& bound,
                      std::uint64_t issue_cycle) {
        Counts* port = nullptr;
        if (bound.memory_write) {
            port = &store_port_;
        } else if (bound.memory_read) {
            port = &load_port_;
        }
        if (port == nullptr) return;
        increment(*port, issue_cycle);
    }

    std::uint32_t issue_width_ = 1;
    std::uint32_t writeback_width_ = 1;
    std::uint32_t load_ports_ = 1;
    std::uint32_t store_ports_ = 1;
    std::array<std::uint32_t,
               static_cast<std::size_t>(IntervalFuPool::kCount)>
        fu_capacity_{};
    Counts issue_;
    Counts writeback_;
    Counts load_port_;
    Counts store_port_;
    std::array<Counts,
               static_cast<std::size_t>(IntervalFuPool::kCount)> fu_;
};

enum class CriticalCause : std::uint8_t {
    kUnattributed,
    kRenameFreeList,
    kDispatchBandwidth,
    kRobCapacity,
    kIqCapacity,
    kLqCapacity,
    kSqCapacity,
    kDependency,
    kSequencer,
    kL1Mshr,
    kL2Mshr,
    kMemoryResponse,
    kCommitBandwidth,
    kTsoStore,
};

struct SparseRobEntry {
    std::uint64_t sequence = std::numeric_limits<std::uint64_t>::max();
    std::uint64_t completion_cycle = 0;
    std::uint64_t completion_extension_cycles = 0;
    std::uint64_t retire_cycle = 0;
    bool completion_extended = false;
};

struct ResponseRenameRelease {
    std::uint64_t cycle = 0;
    std::array<std::uint8_t, kTrackedRegisterClasses> counts{};
};

struct SparseScoreboardCounters {
    std::uint64_t seeds = 0;
    std::uint64_t materialized_uops = 0;
    std::uint64_t absorbed_edges = 0;
    std::uint64_t cross_epoch_edges = 0;
    std::uint64_t rob_crossings = 0;
    std::uint64_t lq_crossings = 0;
    std::uint64_t sq_crossings = 0;
    std::uint64_t block_summary_checkpoints = 0;
    std::uint64_t block_summary_uops = 0;
    std::uint64_t block_summary_rob_writes = 0;
    std::uint64_t block_summary_rob_writes_avoided = 0;
    std::uint64_t resource_candidates = 0;
    std::uint64_t resource_issue_moves = 0;
    std::uint64_t resource_issue_collision_cycles = 0;
    std::uint64_t resource_writeback_moves = 0;
    std::uint64_t resource_writeback_collision_cycles = 0;
    std::uint64_t head_suffix_anchors = 0;
    std::uint64_t head_suffix_recoveries = 0;
    std::uint64_t head_suffix_uops = 0;
    std::uint64_t head_suffix_open_checkpoints = 0;
    std::uint64_t activity_candidates = 0;
    std::uint64_t activity_certified_segments = 0;
    std::uint64_t activity_certified_uops = 0;
    std::uint64_t activity_fallback_segments = 0;
};

struct CoreChunk {
    std::vector<ChunkMemoryEvent> memory;
    std::vector<ChunkUopBound> uops;
    CoreCounters counters;
    std::uint64_t tail_q16 = 0;
    std::uint64_t bound_end_q16 = 0;
    bool interval_bound = false;
    bool reached_end = false;
    bool measurement_boundary = false;
};

enum class ThreadRunState : std::uint8_t {
    kRunnable,
    kFinished,
};

struct ThreadFunctionalCounters {
    std::uint64_t records = 0;
    std::uint64_t retired_uops = 0;
    std::uint64_t retired_instructions = 0;
    std::uint64_t serializing_uops = 0;
    std::uint64_t syscall_uops = 0;

    void add(const CoreCounters& counters) {
        records += counters.records;
        retired_uops += counters.retired_uops;
        retired_instructions += counters.retired_instructions;
        serializing_uops += counters.serializing_uops;
        syscall_uops += counters.syscall_uops;
    }
};

// The current FS trace contract is deliberately single-process.  Tokens are
// local to one stream, so process identity must be reconstructed from the
// portable virtual page in the cold `.vmap` before producer threads run.
// Choosing one deterministic owner prevents the same process page from being
// classified once per core while avoiding host-thread scheduling as an input.
struct ProcessMemoryState {
    struct Page {
        bool initial_pte_state_valid = false;
        bool initial_pte_present = false;
        bool measurement_pte_state_valid = false;
        bool measurement_pte_present = false;
        std::uint32_t owner_thread_id = 0;
        std::uint32_t owner_token = 0;
        std::uint64_t owner_first_record_ordinal = 0;
    };

    void observe(std::uint32_t thread_id,
                 const VirtualPageMapping& mapping) {
        const auto candidate = std::make_tuple(
            mapping.first_record_ordinal, thread_id, mapping.token);
        const auto [it, inserted] = pages.emplace(
            mapping.virtual_page,
            Page{mapping.initial_pte_state_valid,
                 mapping.initial_pte_present,
                 mapping.measurement_pte_state_valid,
                 mapping.measurement_pte_present, thread_id, mapping.token,
                 mapping.first_record_ordinal});
        if (inserted) return;

        auto& page = it->second;
        if (page.initial_pte_state_valid &&
            mapping.initial_pte_state_valid &&
            page.initial_pte_present != mapping.initial_pte_present) {
            throw std::runtime_error(
                "conflicting initial PTE state for process virtual page " +
                std::to_string(mapping.virtual_page));
        }
        if (!page.initial_pte_state_valid &&
            mapping.initial_pte_state_valid) {
            page.initial_pte_state_valid = true;
            page.initial_pte_present = mapping.initial_pte_present;
        }
        if (page.measurement_pte_state_valid &&
            mapping.measurement_pte_state_valid &&
            page.measurement_pte_present !=
                mapping.measurement_pte_present) {
            throw std::runtime_error(
                "conflicting measurement PTE state for process virtual "
                "page " + std::to_string(mapping.virtual_page));
        }
        if (!page.measurement_pte_state_valid &&
            mapping.measurement_pte_state_valid) {
            page.measurement_pte_state_valid = true;
            page.measurement_pte_present =
                mapping.measurement_pte_present;
        }
        const auto owner = std::make_tuple(
            page.owner_first_record_ordinal, page.owner_thread_id,
            page.owner_token);
        if (candidate < owner) {
            page.owner_thread_id = thread_id;
            page.owner_token = mapping.token;
            page.owner_first_record_ordinal =
                mapping.first_record_ordinal;
        }
    }

    const Page* find(std::uint64_t virtual_page) const {
        const auto found = pages.find(virtual_page);
        return found == pages.end() ? nullptr : &found->second;
    }

    bool owns(const Page& page, std::uint32_t thread_id,
              std::uint32_t token) const {
        return page.owner_thread_id == thread_id &&
            page.owner_token == token;
    }

    std::unordered_map<std::uint64_t, Page> pages;
};

struct ThreadState {
    explicit ThreadState(ThreadTraceBinding binding)
        : thread_id(binding.thread_id),
          initial_core(binding.initial_core),
          bound_core(binding.initial_core),
          address_space_id(binding.address_space_id),
          trace(std::move(binding.trace)) {}

    std::uint32_t thread_id = 0;
    std::uint32_t initial_core = 0;
    std::uint32_t bound_core = 0;
    std::uint64_t address_space_id = 0;
    std::unique_ptr<TraceSource> trace;
    ThreadRunState run_state = ThreadRunState::kRunnable;
    bool measurement_phase = false;
    ThreadFunctionalCounters functional_total;
    // Tokens are local to a trace stream. Keeping this state with the thread
    // is deterministic under parallel producers and naturally survives the
    // functional-warmup measurement reset.
    std::unordered_set<std::uint32_t> seen_virtual_page_tokens;
    std::uint64_t page_fault_probability_accumulator = 0;
    std::uint64_t page_fault_background_write_probability_accumulator = 0;
    std::uint64_t
        page_fault_syscall_semantic_fallback_write_probability_accumulator =
            0;
    std::unordered_map<std::uint64_t, std::array<std::uint64_t, 2>>
        page_fault_allocation_probability_accumulators;
    bool page_fault_allocation_armed = false;
    std::uint64_t page_fault_allocation_syscall_number = 0;
    std::uint64_t page_fault_records_since_allocation = 0;
    struct VirtualPageRange {
        std::uint64_t begin = 0;
        std::uint64_t end = 0;
    };
    std::vector<VirtualPageRange> demand_faultable_mappings;
};

void add_virtual_page_range(
    std::vector<ThreadState::VirtualPageRange>& ranges,
    std::uint64_t begin, std::uint64_t end) {
    if (begin >= end) return;
    std::vector<ThreadState::VirtualPageRange> merged;
    merged.reserve(ranges.size() + 1);
    bool inserted = false;
    for (const auto& range : ranges) {
        if (range.end < begin) {
            merged.push_back(range);
        } else if (end < range.begin) {
            if (!inserted) {
                merged.push_back({begin, end});
                inserted = true;
            }
            merged.push_back(range);
        } else {
            begin = std::min(begin, range.begin);
            end = std::max(end, range.end);
        }
    }
    if (!inserted) merged.push_back({begin, end});
    ranges = std::move(merged);
}

void remove_virtual_page_range(
    std::vector<ThreadState::VirtualPageRange>& ranges,
    std::uint64_t begin, std::uint64_t end) {
    if (begin >= end) return;
    std::vector<ThreadState::VirtualPageRange> remaining;
    remaining.reserve(ranges.size() + 1);
    for (const auto& range : ranges) {
        if (range.end <= begin || range.begin >= end) {
            remaining.push_back(range);
            continue;
        }
        if (range.begin < begin) remaining.push_back({range.begin, begin});
        if (range.end > end) remaining.push_back({end, range.end});
    }
    ranges = std::move(remaining);
}

bool contains_virtual_page(
    const std::vector<ThreadState::VirtualPageRange>& ranges,
    std::uint64_t page) {
    for (const auto& range : ranges) {
        if (page < range.begin) return false;
        if (page < range.end) return true;
    }
    return false;
}

std::pair<std::uint64_t, std::uint64_t> syscall_page_range(
    std::uint64_t address, std::uint64_t length) {
    constexpr std::uint64_t kPageBits = 12;
    if (length == 0 || address > UINT64_MAX - (length - 1)) {
        throw std::runtime_error("invalid syscall virtual-memory range");
    }
    const auto begin = address >> kPageBits;
    const auto last = (address + length - 1) >> kPageBits;
    if (last == UINT64_MAX) {
        throw std::runtime_error("syscall virtual-memory page range overflows");
    }
    return {begin, last + 1};
}

void apply_syscall_page_fault_semantics(
    ThreadState& thread, const TraceRecord& record,
    const SyscallMetadata* metadata) {
    constexpr std::uint64_t kLinuxX86Mmap = 9;
    constexpr std::uint64_t kLinuxX86Munmap = 11;
    constexpr std::uint64_t kMapTypeMask = 0x3;
    constexpr std::uint64_t kMapPopulate = 0x8000;
    const auto number = record.syscall_number();
    if (number != kLinuxX86Mmap && number != kLinuxX86Munmap) return;
    if (metadata == nullptr ||
        !metadata->has(kSyscallArgumentsValid) ||
        !metadata->has(kSyscallReturnValueValid) ||
        !metadata->has(kSyscallFailureValid)) {
        throw std::runtime_error(
            "syscall-semantic page-fault model requires complete mmap/munmap "
            "metadata");
    }
    if (metadata->failed) return;
    if (number == kLinuxX86Mmap) {
        if (metadata->argument_count < 6 || metadata->arguments[1] == 0 ||
            (metadata->arguments[3] & kMapTypeMask) == 0) {
            throw std::runtime_error(
                "successful mmap metadata contradicts Linux x86-64 ABI");
        }
        const auto range = syscall_page_range(
            metadata->return_value_raw, metadata->arguments[1]);
        if ((metadata->arguments[3] & kMapPopulate) != 0) {
            remove_virtual_page_range(
                thread.demand_faultable_mappings,
                range.first, range.second);
        } else {
            add_virtual_page_range(
                thread.demand_faultable_mappings,
                range.first, range.second);
        }
        return;
    }
    if (metadata->argument_count < 2 ||
        metadata->return_value_raw != 0) {
        throw std::runtime_error(
            "successful munmap metadata contradicts Linux x86-64 ABI");
    }
    const auto range = syscall_page_range(
        metadata->arguments[0], metadata->arguments[1]);
    remove_virtual_page_range(
        thread.demand_faultable_mappings, range.first, range.second);
}

struct HardwareCoreState {
    HardwareCoreState(std::uint32_t id, const SimulatorConfig& config)
        : core_id(id), predictor(config.branch) {
        if (config.core_model == "interval_bound" ||
            config.core_model == "interval_weave") {
            interval = std::make_unique<IntervalCoreModel>(config);
        }
    }

    std::uint32_t core_id = 0;
    std::optional<std::uint32_t> resident_thread;
    BranchPredictor predictor;
    std::unique_ptr<IntervalCoreModel> interval;
    CoreCounters total;
    std::uint64_t last_bound_memory_issue_q16 = 0;
};

std::vector<ThreadTraceBinding> bind_dense_threads(
    std::vector<std::unique_ptr<TraceSource>> traces) {
    std::vector<ThreadTraceBinding> threads;
    threads.reserve(traces.size());
    for (std::size_t index = 0; index < traces.size(); ++index) {
        ThreadTraceBinding binding;
        binding.thread_id = static_cast<std::uint32_t>(index);
        binding.initial_core = static_cast<std::uint32_t>(index);
        // The compatibility manifest has no process identity.  Zero denotes
        // one shared/unspecified address space; explicit callers can provide
        // distinct IDs without changing the scheduler API later.
        binding.address_space_id = 0;
        binding.trace = std::move(traces[index]);
        threads.push_back(std::move(binding));
    }
    return threads;
}

}  // namespace

#ifdef FASTSIM_ENABLE_TEST_HOOKS
testing::DramScheduleProbeResult testing::run_dram_schedule_probe(
    const DramConfig& config, std::uint32_t line_size,
    const std::vector<DramScheduleRequest>& requests,
    std::uint32_t selection_window) {
    DramModel model(config, line_size);
    std::vector<DramModel::Request> model_requests;
    model_requests.reserve(requests.size());
    for (std::size_t index = 0; index < requests.size(); ++index) {
        model_requests.push_back(DramModel::Request{
            index, requests[index].arrival, requests[index].line,
            requests[index].ordinal});
    }
    const auto batch = model.schedule_frfcfs(
        model_requests, selection_window);
    DramScheduleProbeResult result;
    result.completions.reserve(batch.results.size());
    result.command_cycles.reserve(batch.results.size());
    result.row_hits.reserve(batch.results.size());
    for (const auto& request : batch.results) {
        result.completions.push_back(request.completion);
        result.command_cycles.push_back(request.command_at);
        result.row_hits.push_back(request.row_hit ? 1u : 0u);
    }
    result.service_order = batch.service_order;
    result.max_selection_candidates = batch.max_pending;
    result.max_admitted_pending = batch.max_admitted_pending;
    result.page_policy_scanned_requests =
        batch.page_policy_scanned_requests;
    result.outside_window_row_hits =
        batch.outside_window_row_hits;
    result.outside_window_bank_conflicts =
        batch.outside_window_bank_conflicts;
    result.row_cap_precharges = batch.row_cap_precharges;
    result.adaptive_precharges = batch.adaptive_precharges;
    return result;
}

testing::DramControllerProbeResult testing::run_dram_controller_probe(
    const DramConfig& config, std::uint32_t line_size,
    const std::vector<DramControllerProbeEvent>& events) {
    DramModel model(config, line_size);
    DramControllerProbeResult result;
    result.completions.reserve(events.size());
    for (const auto& event : events) {
        if (event.write) {
            model.enqueue_write(event.arrival, event.line);
            result.completions.push_back(0);
        } else {
            result.completions.push_back(
                model.access(event.arrival, event.line).completion);
        }
    }
    const auto counters = model.controller_stats();
    result.write_enqueues = counters.write_enqueues;
    result.writes_drained = counters.writes_drained;
    result.read_bypasses = counters.read_bypasses;
    result.high_watermark_switches =
        counters.high_watermark_switches;
    result.forced_capacity_drains =
        counters.forced_capacity_drains;
    result.turnarounds = counters.turnarounds;
    result.write_row_hits = counters.write_row_hits;
    result.write_row_misses = counters.write_row_misses;
    result.max_pending = counters.max_pending;
    result.pending_final = counters.pending_final;
    return result;
}
#endif

class Simulator::Impl {
  public:
    Impl(SimulatorConfig config,
         std::vector<ThreadTraceBinding> threads)
        : config_(std::move(config)) {
        config_.validate();
        if (threads.empty() || threads.size() > config_.cores) {
            throw std::invalid_argument(
                "thread count must be in [1, configured core count]");
        }
        const auto boundary_streams = static_cast<std::size_t>(
            std::count_if(
                threads.begin(), threads.end(), [](const auto& binding) {
                    return binding.trace != nullptr &&
                           binding.trace->has_measurement_boundary();
                }));
        if (boundary_streams != 0 && boundary_streams != threads.size()) {
            throw std::invalid_argument(
                "functional warmup requires a measurement boundary on "
                "every active trace");
        }
        measurement_warmup_enabled_ = boundary_streams == threads.size();
        if (measurement_warmup_enabled_ &&
            (config_.core_model != "interval_weave" ||
             config_.interval_scheduler != "time_epoch")) {
            throw std::invalid_argument(
                "functional warmup currently requires interval_weave with "
                "the time_epoch scheduler");
        }
        if (measurement_warmup_enabled_ &&
            config_.response_rename_feedback) {
            throw std::invalid_argument(
                "functional warmup is not compatible with response rename "
                "feedback");
        }
        if (config_.interval_private_preview ||
            config_.interval_parallel_feedback ||
            (config_.dram.scheduler == "frfcfs" &&
             config_.dram.channels > 1)) {
            domain_worker_count_ = std::min<std::uint32_t>(
                static_cast<std::uint32_t>(threads.size()),
                config_.domain_workers == 0 ? 8u
                                            : config_.domain_workers);
        } else {
            domain_worker_count_ = 0;
        }
        initialize_stats(threads.size());
        cores_.reserve(config_.cores);
        threads_.reserve(threads.size());
        private_caches_.reserve(config_.cores);
        chunk_queues_.resize(config_.cores);
        current_chunks_.resize(config_.cores);
        current_indices_.resize(config_.cores);
        current_uop_indices_.resize(config_.cores);
        ready_q16_.resize(config_.cores);
        interval_gap_q16_.resize(config_.cores);
        response_iq_ready_cycles_.resize(config_.cores);
        response_rename_releases_.resize(config_.cores);
        response_rename_live_.resize(config_.cores);
        response_rename_cycle_.resize(config_.cores);
        response_rename_used_.resize(config_.cores);
        response_rename_cause_.resize(
            config_.cores, CriticalCause::kUnattributed);
        response_rename_counters_.resize(config_.cores);
        response_dispatch_cycle_.resize(config_.cores);
        response_dispatch_used_.resize(config_.cores);
        response_dispatch_cause_.resize(
            config_.cores, CriticalCause::kUnattributed);
        if (config_.response_queue_feedback) {
            for (auto& ready : response_iq_ready_cycles_) {
                ready.resize(config_.iq_entries, 0);
            }
        }
        response_rob_ready_cycles_.resize(config_.cores);
        response_lq_ready_cycles_.resize(config_.cores);
        response_sq_ready_cycles_.resize(config_.cores);
        response_commit_cycle_.resize(config_.cores);
        response_commit_used_.resize(config_.cores);
        response_commit_cause_.resize(
            config_.cores, CriticalCause::kUnattributed);
        response_store_drain_ready_cycle_.resize(config_.cores);
        response_rob_next_slot_.resize(config_.cores);
        response_lq_next_slot_.resize(config_.cores);
        response_sq_next_slot_.resize(config_.cores);
        response_sparse_rob_entries_.resize(config_.cores);
        response_rob_head_suffix_open_.resize(config_.cores, 0);
        if (config_.response_rob_lsq_feedback ||
            config_.response_sparse_scoreboard) {
            for (auto& ready : response_rob_ready_cycles_) {
                ready.resize(config_.rob_entries, 0);
            }
            for (auto& ready : response_lq_ready_cycles_) {
                ready.resize(config_.lq_entries, 0);
            }
            for (auto& ready : response_sq_ready_cycles_) {
                ready.resize(config_.sq_entries, 0);
            }
        }
        if (config_.response_sparse_scoreboard) {
            for (auto& entries : response_sparse_rob_entries_) {
                entries.resize(config_.rob_entries);
            }
        }
        sequencer_ready_cycles_.resize(config_.cores);
        if (config_.ruby_sequencer_max_outstanding != 0) {
            for (auto& ready : sequencer_ready_cycles_) {
                ready.resize(
                    config_.ruby_sequencer_max_outstanding, 0);
            }
        }
        // An unbound hardware core is idle and therefore already complete.
        // Active static bindings below turn these slots back into producers.
        finished_.assign(config_.cores, true);
        producer_finished_.assign(config_.cores, true);
        const auto certificate_l1_sets = static_cast<std::size_t>(
            config_.l1d.size_bytes /
            (static_cast<std::uint64_t>(config_.l1d.associativity) *
             config_.l1d.line_size));
        const auto certificate_l2_sets = static_cast<std::size_t>(
            config_.l2.size_bytes /
            (static_cast<std::uint64_t>(config_.l2.associativity) *
             config_.l2.line_size));
        // Cache set counts are powers of two. Components indexed by their
        // common low bits keep both L1 and L2 replacement state independent,
        // including unusual configurations with fewer L2 than L1 sets.
        certificate_component_sets_ =
            std::min(certificate_l1_sets, certificate_l2_sets);
        certificate_component_next_rank_.resize(
            static_cast<std::size_t>(config_.cores) *
            certificate_component_sets_);
        for (std::uint32_t core = 0; core < config_.cores; ++core) {
            cores_.push_back(
                std::make_unique<HardwareCoreState>(core, config_));
            private_caches_.push_back(std::make_unique<PrivateHierarchy>(
                config_.l1d, config_.l2));
        }
        std::unordered_set<std::uint32_t> thread_ids;
        std::unordered_set<std::uint32_t> bound_cores;
        for (auto& binding : threads) {
            if (!binding.trace) {
                throw std::invalid_argument("thread binding has no trace");
            }
            if (binding.initial_core >= config_.cores) {
                throw std::invalid_argument(
                    "thread initial core exceeds configured core count");
            }
            if (!thread_ids.insert(binding.thread_id).second) {
                throw std::invalid_argument("duplicate thread ID");
            }
            if (!bound_cores.insert(binding.initial_core).second) {
                throw std::invalid_argument(
                    "stage-1 static scheduling permits at most one thread "
                    "per core");
            }
            const auto core = binding.initial_core;
            cores_[core]->resident_thread = binding.thread_id;
            finished_[core] = false;
            producer_finished_[core] = false;
            if (config_.page_fault_initial_pte_state_model) {
                const auto* mappings =
                    binding.trace->all_virtual_page_mappings();
                if (mappings == nullptr) {
                    throw std::invalid_argument(
                        "page_fault.initial_pte_state_model requires a "
                        "preloaded FST virtual-page map on every trace");
                }
                for (const auto& [token, mapping] : *mappings) {
                    if (token != mapping.token) {
                        throw std::runtime_error(
                            "virtual-page map key/token mismatch");
                    }
                    process_memory_.observe(binding.thread_id, mapping);
                }
            }
            threads_.push_back(
                std::make_unique<ThreadState>(std::move(binding)));
        }
        stats_.trace_worker_threads = threads_.size();
        shared_ = std::make_unique<SharedSystem>(
            config_, private_caches_, stats_);
    }

    SimulationStats run() {
        const auto start = std::chrono::steady_clock::now();
        auto measurement_start = start;
        launch_workers();
        try {
            run_model_phase();
            if (measurement_warmup_enabled_) {
                stop_workers();
                prepare_measurement_phase();
                measurement_start = std::chrono::steady_clock::now();
                launch_workers();
                run_model_phase();
            }
            finalize_response_rename();
        } catch (...) {
            stop_workers();
            throw;
        }
        stop_workers();
        const auto end = std::chrono::steady_clock::now();
        stats_.wall_time_ns = static_cast<std::uint64_t>(
            std::chrono::duration_cast<std::chrono::nanoseconds>(end - start)
                .count());
        stats_.functional_warmup_wall_ns = measurement_warmup_enabled_
            ? static_cast<std::uint64_t>(
                  std::chrono::duration_cast<std::chrono::nanoseconds>(
                      measurement_start - start).count())
            : 0;
        stats_.measurement_wall_ns = static_cast<std::uint64_t>(
            std::chrono::duration_cast<std::chrono::nanoseconds>(
                end - measurement_start).count());
        stats_.max_resident_chunks = max_resident_chunks_;
        for (std::uint32_t core = 0; core < config_.cores; ++core) {
            std::uint64_t absolute_q16 = 0;
            if (cores_[core]->interval) {
                absolute_q16 = cycles_to_fixed(
                    cores_[core]->interval->last_retire_cycle()) +
                    interval_gap_q16_[core];
                stats_.committed_pipeline_audit[core] =
                    cores_[core]->interval->committed_pipeline_audit();
            } else {
                absolute_q16 = ready_q16_[core];
            }
            const auto origin_q16 = measurement_origin_q16_.empty()
                ? 0u
                : measurement_origin_q16_[core];
            if (absolute_q16 < origin_q16) {
                throw std::logic_error(
                    "core completion precedes functional measurement "
                    "barrier");
            }
            cores_[core]->total.cycles = fixed_to_cycle_ceil(
                absolute_q16 - origin_q16);
            if (config_.irq_event_model) {
                // The deadline is defined over foreground user time. All
                // synthetic kernel service is excluded from the deadline
                // base, preventing syscalls, faults, or IRQs from recursively
                // creating additional IRQs.
                const auto non_irq_kernel_cycles =
                    cores_[core]->total.syscall_kernel.active_cycles +
                    cores_[core]->total.page_fault_kernel.active_cycles;
                if (non_irq_kernel_cycles > cores_[core]->total.cycles) {
                    throw std::logic_error(
                        "synthetic kernel service exceeds core timeline");
                }
                const auto foreground_cycles =
                    cores_[core]->total.cycles - non_irq_kernel_cycles;
                const auto events = foreground_cycles /
                    config_.irq_period_cycles;
                KernelEventCounters irq_delta;
                accumulate_kernel_event(
                    irq_delta, config_.irq_event_profile, events);
                if (irq_delta.active_cycles >
                    std::numeric_limits<std::uint64_t>::max() -
                        cores_[core]->total.cycles) {
                    throw std::overflow_error(
                        "periodic IRQ service exceeds core cycle range");
                }
                cores_[core]->total.cycles += irq_delta.active_cycles;
                cores_[core]->total.irq_kernel += irq_delta;
            }
            stats_.cores[core] = cores_[core]->total;
        }
        for (std::size_t index = 0; index < threads_.size(); ++index) {
            const auto& thread = *threads_[index];
            auto& output = stats_.threads[index];
            output.thread_id = thread.thread_id;
            output.initial_core = thread.initial_core;
            output.final_core = thread.bound_core;
            output.address_space_id = thread.address_space_id;
            output.records = thread.functional_total.records;
            output.retired_uops = thread.functional_total.retired_uops;
            output.retired_instructions =
                thread.functional_total.retired_instructions;
            output.serializing_uops =
                thread.functional_total.serializing_uops;
            output.syscall_uops = thread.functional_total.syscall_uops;
            // Stage 1 has a static injective binding, so the resident core's
            // elapsed target time is also this thread's elapsed target time.
            output.cycles = cores_[thread.bound_core]->total.cycles;
        }
        shared_->export_dram_controller_stats(stats_);
        return stats_;
    }

    ~Impl() { stop_workers(); }

  private:
    void initialize_stats(std::size_t thread_count) {
        stats_ = SimulationStats{};
        stats_.cores.resize(config_.cores);
        stats_.o3.resize(config_.cores);
        stats_.response_rename.resize(config_.cores);
        stats_.committed_pipeline_audit.resize(config_.cores);
        stats_.sequencer.resize(config_.cores);
        stats_.response_critical_cycles.resize(config_.cores);
        stats_.response_residuals.resize(config_.cores);
        stats_.cha.resize(config_.cha_count);
        stats_.threads.resize(thread_count);
        stats_.trace_worker_threads = thread_count;
    }

    void run_model_phase() {
        if (config_.core_model == "interval_weave") {
            run_interval_weave();
        } else {
            PendingQueue queue;
            for (std::uint32_t core = 0; core < config_.cores; ++core) {
                prime_core(core, queue);
            }
            while (!queue.empty()) {
                const auto pending = queue.top();
                queue.pop();
                process_memory_event(pending);
                advance_core(pending.core, queue);
            }
        }
        if (!all_finished()) {
            throw std::logic_error(
                "causal frontier drained before every core finished");
        }
    }

    void prepare_measurement_phase() {
        if (!measurement_warmup_enabled_) {
            throw std::logic_error(
                "measurement phase requested without functional warmup");
        }
        if (resident_chunks_ != 0 ||
            std::any_of(
                current_chunks_.begin(), current_chunks_.end(),
                [](const auto& chunk) { return chunk != nullptr; })) {
            throw std::logic_error(
                "functional warmup barrier retained producer chunks");
        }
        for (const auto& thread : threads_) {
            if (!thread->trace->measurement_boundary_pending()) {
                throw std::runtime_error(
                    "active trace ended before the common functional "
                    "warmup boundary: " + thread->trace->description());
            }
        }

        CoreCounters warmup_total;
        for (const auto& core : cores_) warmup_total += core->total;
        const auto warmup_memory_events = stats_.batch_memory_events;

        auto barrier_q16 = phase_global_time_q16_;
        for (const auto& core : cores_) {
            if (!core->interval) continue;
            const auto absolute_q16 = cycles_to_fixed(
                core->interval->last_retire_cycle()) +
                interval_gap_q16_[core->core_id];
            barrier_q16 = std::max(barrier_q16, absolute_q16);
        }
        for (const auto& core : cores_) {
            if (!core->resident_thread.has_value() || !core->interval) {
                continue;
            }
            const auto absolute_q16 = cycles_to_fixed(
                core->interval->last_retire_cycle()) +
                interval_gap_q16_[core->core_id];
            interval_gap_q16_[core->core_id] +=
                barrier_q16 - absolute_q16;
            core->interval->reset_measurement_audit();
        }
        phase_global_time_q16_ = barrier_q16;
        measurement_origin_q16_.assign(config_.cores, 0);
        for (const auto& thread : threads_) {
            measurement_origin_q16_[thread->bound_core] = barrier_q16;
        }

        initialize_stats(threads_.size());
        shared_->reset_dram_controller_stats();
        stats_.functional_warmup_enabled = true;
        stats_.functional_warmup_records = warmup_total.records;
        stats_.functional_warmup_uops = warmup_total.retired_uops;
        stats_.functional_warmup_instructions =
            warmup_total.retired_instructions;
        stats_.functional_warmup_memory_events = warmup_memory_events;
        stats_.functional_warmup_barrier_cycles =
            fixed_to_cycle_ceil(barrier_q16);

        std::fill(finished_.begin(), finished_.end(), true);
        std::fill(producer_finished_.begin(), producer_finished_.end(), true);
        for (auto& core : cores_) core->total = CoreCounters{};
        for (auto& thread : threads_) {
            thread->trace->start_measurement();
            thread->measurement_phase = true;
            thread->functional_total = ThreadFunctionalCounters{};
            // Probability accumulators are deterministic sampling phase, not
            // architectural residency. Reset them with measurement counters
            // so reported candidate counts reproduce the selected event
            // count exactly; keep seen pages and allocation recency state.
            thread->page_fault_probability_accumulator = 0;
            thread->page_fault_background_write_probability_accumulator = 0;
            thread
                ->page_fault_syscall_semantic_fallback_write_probability_accumulator =
                0;
            thread->page_fault_allocation_probability_accumulators.clear();
            thread->run_state = ThreadRunState::kRunnable;
            finished_[thread->bound_core] = false;
            producer_finished_[thread->bound_core] = false;
        }
        std::fill(current_indices_.begin(), current_indices_.end(), 0);
        std::fill(current_uop_indices_.begin(), current_uop_indices_.end(), 0);
        max_resident_chunks_ = 0;
        stopping_ = false;
        worker_error_ = nullptr;
        domain_stopping_ = false;
        domain_task_ = DomainPhaseTask::kNone;
        domain_generation_ = 0;
        domain_workers_pending_ = 0;
        domain_error_ = nullptr;
    }

    void finalize_response_rename() {
        if (!config_.response_rename_feedback ||
            config_.core_model != "interval_weave") {
            return;
        }
        for (std::uint32_t core = 0; core < config_.cores; ++core) {
            auto& releases = response_rename_releases_[core];
            auto& live = response_rename_live_[core];
            auto& counters = response_rename_counters_[core];
            const auto final_retire = response_commit_cycle_[core];
            while (!releases.empty()) {
                const auto release = releases.front();
                if (release.cycle > final_retire) {
                    throw std::logic_error(
                        "response rename release exceeds final ordered "
                        "retirement");
                }
                releases.pop_front();
                for (std::size_t class_index = 0;
                     class_index < live.size(); ++class_index) {
                    const auto count = release.counts[class_index];
                    if (count > live[class_index]) {
                        throw std::logic_error(
                            "response rename final release underflow");
                    }
                    live[class_index] -= count;
                    counters.released[class_index] += count;
                }
            }
            counters.live = live;
            if (!counters.conserved() ||
                std::any_of(
                    live.begin(), live.end(),
                    [](std::uint64_t count) { return count != 0; })) {
                throw std::logic_error(
                    "response rename final accounting is not conserved");
            }
            stats_.response_rename[core] = counters;
        }
    }

    struct Pending {
        std::uint64_t issue_q16 = 0;
        std::uint32_t core = 0;
        std::uint32_t index = 0;
    };

    struct PendingLater {
        bool operator()(const Pending& left,
                        const Pending& right) const {
            if (left.issue_q16 != right.issue_q16) {
                return left.issue_q16 > right.issue_q16;
            }
            if (left.core != right.core) return left.core > right.core;
            return left.index > right.index;
        }
    };

    using PendingQueue =
        std::priority_queue<Pending, std::vector<Pending>, PendingLater>;

    struct BatchPending {
        std::uint64_t issue_q16 = 0;
        std::uint64_t corrected_issue_q16 = 0;
        std::size_t proposed_rank = 0;
        std::uint32_t core = 0;
        std::uint32_t index = 0;
    };

    struct PathConflictPlan {
        std::unordered_set<std::uint64_t> cross_core_lines;
        std::uint64_t component_events = 0;
        std::uint64_t max_component_events = 0;

        bool empty() const { return cross_core_lines.empty(); }
    };

    // A state certificate is a per-core, per-private-set prefix rather than a
    // single epoch bit. The common low set-index bits of L1 and L2 form an
    // independent private-state component. A conflict therefore repairs only
    // the affected component suffix instead of discarding a core or epoch.
    struct PrivatePreviewPlan {
        std::vector<std::size_t> component_rank_limit;
        std::uint64_t safe_events = 0;
        std::uint64_t unsafe_events = 0;
        std::uint64_t safe_cores = 0;
        std::uint64_t unsafe_cores = 0;

        bool any_safe() const { return safe_events != 0; }
        bool all_safe() const { return unsafe_events == 0; }
    };

    struct CausalClosurePlan {
        std::vector<BatchPending> timing_order;
        std::uint64_t closure_components = 0;
        std::uint64_t closure_events = 0;
        bool replayable = true;
        bool changed = false;
    };

    struct EpochAccessorSlot {
        std::uint64_t line = 0;
        std::array<std::uint64_t, 4> cores{};
        std::uint32_t generation = 0;
    };

    struct MemoryReplay {
        std::uint64_t exposed_cycles = 0;
        // Capacity resources release at the full response latency even when
        // memory_exposure puts only part of it on the critical path.
        std::uint64_t latency_cycles = 0;
        std::uint64_t l2_evicted_line = 0;
        HitLevel private_level = HitLevel::kUnknown;
        bool l2_evicted = false;
        bool l2_evicted_dirty = false;
        bool shared_escape = false;
        bool private_previewed = false;
        SharedTimingDescriptor shared_timing;
    };

    struct TimingFeedback {
        std::vector<std::vector<std::uint64_t>> issue_extra_q16;
        std::vector<std::uint64_t> interval_extra_q16;
        std::vector<CriticalCause> interval_critical_cause;
        std::vector<std::vector<std::uint64_t>> sequencer_ready_cycles;
        std::vector<SequencerCounters> sequencer_counters;
        std::vector<std::vector<std::uint64_t>> iq_ready_cycles;
        std::vector<O3QueueCounters> o3_counters;
        std::vector<std::deque<ResponseRenameRelease>> rename_releases;
        std::vector<std::array<std::uint64_t, kTrackedRegisterClasses>>
            rename_live;
        std::vector<std::uint64_t> rename_cycle;
        std::vector<std::uint32_t> rename_used;
        std::vector<CriticalCause> rename_cause;
        std::vector<ResponseRenameCounters> rename_counters;
        std::vector<std::uint64_t> dispatch_cycle;
        std::vector<std::uint32_t> dispatch_used;
        std::vector<CriticalCause> dispatch_cause;
        std::vector<std::vector<std::uint64_t>> rob_ready_cycles;
        std::vector<std::vector<std::uint64_t>> lq_ready_cycles;
        std::vector<std::vector<std::uint64_t>> sq_ready_cycles;
        std::vector<std::uint32_t> rob_next_slot;
        std::vector<std::uint32_t> lq_next_slot;
        std::vector<std::uint32_t> sq_next_slot;
        std::vector<std::uint64_t> commit_cycle;
        std::vector<std::uint32_t> commit_used;
        std::vector<CriticalCause> commit_cause;
        std::vector<std::uint64_t> store_drain_ready_cycle;
        std::vector<std::vector<SparseRobEntry>> sparse_rob_entries;
        std::vector<SparseScoreboardCounters> sparse_counters;
        std::vector<ResponseResidualCounters> response_residuals;
        // One byte per accepted UOP identifies the bounded retirement suffix
        // whose shared issues may be retimed.  The open bit is checkpointed
        // separately so an episode cannot disappear at a Q boundary.
        std::vector<std::vector<std::uint8_t>> rob_head_suffix_uops;
        // Do not use vector<bool>: parallel per-core feedback writes would
        // share packed machine words and race despite distinct core indices.
        std::vector<std::uint8_t> rob_head_suffix_open;
    };

    enum class DomainPhaseTask {
        kNone,
        kPrivatePreview,
        kTimingFeedback,
        kDramChannel,
    };

    static std::uint64_t saturating_add(std::uint64_t left,
                                        std::uint64_t right) {
        if (right > std::numeric_limits<std::uint64_t>::max() - left) {
            return std::numeric_limits<std::uint64_t>::max();
        }
        return left + right;
    }

    static void record_response_critical_cycles(
        ResponseCriticalCycleCounters& counters,
        CriticalCause cause, std::uint64_t cycles) {
        if (cycles == 0) return;
        counters.total_cycles += cycles;
        switch (cause) {
            case CriticalCause::kRenameFreeList:
                counters.rename_free_list_cycles += cycles;
                break;
            case CriticalCause::kDispatchBandwidth:
                counters.dispatch_bandwidth_cycles += cycles;
                break;
            case CriticalCause::kRobCapacity:
                counters.rob_capacity_cycles += cycles;
                break;
            case CriticalCause::kIqCapacity:
                counters.iq_capacity_cycles += cycles;
                break;
            case CriticalCause::kLqCapacity:
                counters.lq_capacity_cycles += cycles;
                break;
            case CriticalCause::kSqCapacity:
                counters.sq_capacity_cycles += cycles;
                break;
            case CriticalCause::kDependency:
                counters.dependency_cycles += cycles;
                break;
            case CriticalCause::kSequencer:
                counters.sequencer_cycles += cycles;
                break;
            case CriticalCause::kL1Mshr:
                counters.l1_mshr_cycles += cycles;
                break;
            case CriticalCause::kL2Mshr:
                counters.l2_mshr_cycles += cycles;
                break;
            case CriticalCause::kMemoryResponse:
                counters.memory_response_cycles += cycles;
                break;
            case CriticalCause::kCommitBandwidth:
                counters.commit_bandwidth_cycles += cycles;
                break;
            case CriticalCause::kTsoStore:
                counters.tso_store_cycles += cycles;
                break;
            case CriticalCause::kUnattributed:
                counters.unattributed_cycles += cycles;
                break;
        }
        if (counters.classified_cycles() != counters.total_cycles) {
            throw std::logic_error(
                "response critical-cycle attribution is not conserved");
        }
    }

    bool response_timing_retime_enabled() const {
        return config_.interval_response_retime ||
               config_.interval_rob_head_suffix_replay;
    }

    void record_response_retime_noop() {
        if (config_.interval_rob_head_suffix_replay) {
            ++stats_.rob_head_suffix_noop_epochs;
        } else {
            ++stats_.response_retime_noop_epochs;
        }
    }

    void record_response_retime_candidate(std::uint64_t moved) {
        if (config_.interval_rob_head_suffix_replay) {
            ++stats_.rob_head_suffix_candidate_epochs;
            stats_.rob_head_suffix_moved_shared_events += moved;
        } else {
            ++stats_.response_retime_candidate_epochs;
            stats_.response_retime_moved_shared_events += moved;
        }
    }

    void record_response_retime_replayed(std::uint64_t events) {
        if (config_.interval_rob_head_suffix_replay) {
            stats_.rob_head_suffix_replayed_events += events;
        } else {
            stats_.response_retime_replayed_events += events;
        }
    }

    void record_response_retime_stable() {
        if (config_.interval_rob_head_suffix_replay) {
            ++stats_.rob_head_suffix_stable_epochs;
        } else {
            ++stats_.response_retime_stable_epochs;
        }
    }

    void record_response_retime_fallback() {
        if (config_.interval_rob_head_suffix_replay) {
            ++stats_.rob_head_suffix_fallback_epochs;
        } else {
            ++stats_.response_retime_fallback_epochs;
        }
    }

    void record_response_retime_wall_ns(std::uint64_t elapsed) {
        if (config_.interval_rob_head_suffix_replay) {
            stats_.rob_head_suffix_wall_ns += elapsed;
        } else {
            stats_.response_retime_wall_ns += elapsed;
        }
    }

    static std::uint64_t mix_line(std::uint64_t value) {
        value ^= value >> 30;
        value *= 0xbf58476d1ce4e5b9ull;
        value ^= value >> 27;
        value *= 0x94d049bb133111ebull;
        value ^= value >> 31;
        return value;
    }

    void prepare_epoch_accessors(std::size_t event_count) {
        std::size_t capacity = 8;
        while (capacity < event_count * 2) capacity <<= 1;
        if (epoch_accessor_slots_.size() < capacity) {
            epoch_accessor_slots_.assign(capacity, EpochAccessorSlot{});
            epoch_accessor_generation_ = 1;
            return;
        }
        ++epoch_accessor_generation_;
        if (epoch_accessor_generation_ == 0) {
            for (auto& slot : epoch_accessor_slots_) {
                slot.generation = 0;
            }
            epoch_accessor_generation_ = 1;
        }
    }

    std::array<std::uint64_t, 4>& add_epoch_accessor(
        std::uint64_t line) {
        const auto mask = epoch_accessor_slots_.size() - 1;
        auto index = static_cast<std::size_t>(mix_line(line)) & mask;
        while (true) {
            auto& slot = epoch_accessor_slots_[index];
            if (slot.generation != epoch_accessor_generation_) {
                slot.generation = epoch_accessor_generation_;
                slot.line = line;
                slot.cores = {};
                return slot.cores;
            }
            if (slot.line == line) return slot.cores;
            index = (index + 1) & mask;
        }
    }

    const std::array<std::uint64_t, 4>* find_epoch_accessors(
        std::uint64_t line) const {
        const auto mask = epoch_accessor_slots_.size() - 1;
        auto index = static_cast<std::size_t>(mix_line(line)) & mask;
        while (true) {
            const auto& slot = epoch_accessor_slots_[index];
            if (slot.generation != epoch_accessor_generation_) {
                return nullptr;
            }
            if (slot.line == line) return &slot.cores;
            index = (index + 1) & mask;
        }
    }

    // All capacity calendars are min-heaps and a newly scheduled completion
    // cannot precede the slot it replaces. Replacing the root and sifting it
    // down costs one heap traversal instead of pop_heap + push_heap's two.
    static void replace_min(std::vector<std::uint64_t>& heap,
                            std::uint64_t value) {
        if (heap.empty()) {
            throw std::logic_error("cannot replace an empty capacity heap");
        }
        heap.front() = value;
        std::size_t parent = 0;
        while (true) {
            const auto left = parent * 2 + 1;
            if (left >= heap.size()) break;
            const auto right = left + 1;
            const auto child =
                right < heap.size() && heap[right] < heap[left]
                    ? right
                    : left;
            if (heap[parent] <= heap[child]) break;
            std::swap(heap[parent], heap[child]);
            parent = child;
        }
    }

    static std::uint64_t count_inversions(
        const std::vector<std::size_t>& ranks) {
        auto coordinates = ranks;
        std::sort(coordinates.begin(), coordinates.end());
        coordinates.erase(
            std::unique(coordinates.begin(), coordinates.end()),
            coordinates.end());
        std::vector<std::uint64_t> fenwick(coordinates.size() + 1, 0);
        std::uint64_t inversions = 0;
        std::uint64_t seen = 0;
        for (const auto value : ranks) {
            const auto rank = static_cast<std::size_t>(
                std::lower_bound(coordinates.begin(), coordinates.end(),
                                 value) - coordinates.begin());
            std::uint64_t prefix = 0;
            for (auto index = rank + 1; index > 0;
                 index &= index - 1) {
                prefix += fenwick[index];
            }
            inversions += seen - prefix;
            for (auto index = rank + 1; index < fenwick.size();
                 index += index & (~index + 1)) {
                ++fenwick[index];
            }
            ++seen;
        }
        return inversions;
    }

    void launch_workers() {
        if (!workers_.empty()) return;
        workers_.reserve(threads_.size());
        for (std::size_t thread = 0; thread < threads_.size(); ++thread) {
            workers_.emplace_back(
                [this, thread] { worker_loop(thread); });
        }
    }

    void ensure_domain_workers() {
        if (!domain_workers_.empty()) return;
        if (domain_worker_count_ == 0) {
            throw std::logic_error(
                "domain phase requested without configured workers");
        }
        stats_.domain_worker_threads = domain_worker_count_;
        domain_workers_.reserve(domain_worker_count_);
        for (std::uint32_t worker = 0; worker < domain_worker_count_;
             ++worker) {
            domain_workers_.emplace_back(
                [this] { domain_worker_loop(); });
        }
    }

    MemoryReplay preview_memory_event(
        std::uint32_t core, const ChunkMemoryEvent& event,
        PrivateTransaction* transaction = nullptr) {
        const auto private_result = private_caches_[core]->access(
            event.line, event.write, cores_[core]->total, transaction);
        MemoryReplay replay;
        replay.private_level = private_result.level;
        replay.l2_evicted = private_result.l2_evicted;
        replay.l2_evicted_dirty = private_result.l2_evicted_dirty;
        replay.l2_evicted_line = private_result.l2_evicted_line;
        if (private_result.level == HitLevel::kL1 ||
            private_result.level == HitLevel::kL2) {
            const auto latency = private_result.level == HitLevel::kL1
                                     ? config_.l1d.hit_latency
                                     : config_.l2.hit_latency;
            replay.latency_cycles = latency;
            const auto exposed_latency =
                latency > event.lower_bound_latency
                    ? latency - event.lower_bound_latency
                    : 0;
            replay.exposed_cycles = static_cast<std::uint64_t>(
                std::ceil(static_cast<double>(exposed_latency) *
                          config_.memory_exposure));
        }
        return replay;
    }

    void preview_core(std::uint32_t core) {
        const auto begin = (*preview_begin_)[core];
        const auto end = (*preview_end_)[core];
        if (begin == end) return;
        const auto& chunk = *current_chunks_[core];
        auto& feedback = (*preview_feedback_)[core];
        for (auto uop = begin; uop < end; ++uop) {
            const auto& bound = chunk.uops[uop];
            for (std::uint32_t offset = 0;
                 offset < bound.memory_count; ++offset) {
                const auto event_index =
                    static_cast<std::size_t>(bound.first_memory) + offset;
                if (!feedback[event_index].private_previewed) continue;
                auto replay = preview_memory_event(
                    core, chunk.memory[event_index],
                    preview_transaction_ == nullptr
                        ? nullptr
                        : &preview_transaction_->private_caches[core]);
                replay.private_previewed = true;
                feedback[event_index] = replay;
            }
        }
    }

    void run_private_preview(
        const std::vector<std::size_t>& accepted_begin,
        const std::vector<std::size_t>& accepted_end,
        std::vector<std::vector<MemoryReplay>>& feedback,
        SharedSystem::Transaction* transaction = nullptr) {
        ensure_domain_workers();
        {
            std::lock_guard<std::mutex> lock(queue_mutex_);
            if (worker_error_) std::rethrow_exception(worker_error_);
        }
        const auto started = std::chrono::steady_clock::now();
        {
            std::lock_guard<std::mutex> lock(domain_mutex_);
            preview_begin_ = &accepted_begin;
            preview_end_ = &accepted_end;
            preview_feedback_ = &feedback;
            preview_transaction_ = transaction;
            domain_task_ = DomainPhaseTask::kPrivatePreview;
            domain_next_core_.store(0, std::memory_order_relaxed);
            domain_task_count_ = config_.cores;
            domain_index_task_ = nullptr;
            domain_workers_pending_ = domain_worker_count_;
            domain_error_ = nullptr;
            ++domain_generation_;
        }
        domain_cv_.notify_all();
        std::exception_ptr error;
        {
            std::unique_lock<std::mutex> lock(domain_mutex_);
            domain_done_cv_.wait(lock, [&] {
                return domain_workers_pending_ == 0;
            });
            error = domain_error_;
            domain_task_ = DomainPhaseTask::kNone;
            preview_begin_ = nullptr;
            preview_end_ = nullptr;
            preview_feedback_ = nullptr;
            preview_transaction_ = nullptr;
        }
        const auto ended = std::chrono::steady_clock::now();
        ++stats_.domain_phase_calls;
        stats_.domain_phase_wall_ns += static_cast<std::uint64_t>(
            std::chrono::duration_cast<std::chrono::nanoseconds>(
                ended - started).count());
        if (error) std::rethrow_exception(error);
    }

    void run_parallel_timing_feedback(
        const std::vector<std::vector<MemoryReplay>>& event_feedback,
        const std::vector<std::size_t>& accepted_begin,
        const std::vector<std::size_t>& accepted_end,
        TimingFeedback& timing) {
        ensure_domain_workers();
        {
            std::lock_guard<std::mutex> lock(queue_mutex_);
            if (worker_error_) std::rethrow_exception(worker_error_);
        }
        const auto started = std::chrono::steady_clock::now();
        {
            std::lock_guard<std::mutex> lock(domain_mutex_);
            timing_event_feedback_ = &event_feedback;
            timing_begin_ = &accepted_begin;
            timing_end_ = &accepted_end;
            timing_output_ = &timing;
            domain_task_ = DomainPhaseTask::kTimingFeedback;
            domain_next_core_.store(0, std::memory_order_relaxed);
            domain_task_count_ = config_.cores;
            domain_index_task_ = nullptr;
            domain_workers_pending_ = domain_worker_count_;
            domain_error_ = nullptr;
            ++domain_generation_;
        }
        domain_cv_.notify_all();
        std::exception_ptr error;
        {
            std::unique_lock<std::mutex> lock(domain_mutex_);
            domain_done_cv_.wait(lock, [&] {
                return domain_workers_pending_ == 0;
            });
            error = domain_error_;
            domain_task_ = DomainPhaseTask::kNone;
            timing_event_feedback_ = nullptr;
            timing_begin_ = nullptr;
            timing_end_ = nullptr;
            timing_output_ = nullptr;
        }
        const auto ended = std::chrono::steady_clock::now();
        ++stats_.domain_phase_calls;
        stats_.domain_phase_wall_ns += static_cast<std::uint64_t>(
            std::chrono::duration_cast<std::chrono::nanoseconds>(
                ended - started).count());
        if (error) std::rethrow_exception(error);
    }

    void run_parallel_channel_tasks(
        std::uint32_t task_count,
        const std::function<void(std::uint32_t)>& task) {
        if (task_count == 0) return;
        if (domain_worker_count_ <= 1 || task_count == 1) {
            for (std::uint32_t index = 0; index < task_count; ++index) {
                task(index);
            }
            return;
        }
        ensure_domain_workers();
        {
            std::lock_guard<std::mutex> lock(queue_mutex_);
            if (worker_error_) std::rethrow_exception(worker_error_);
        }
        {
            std::lock_guard<std::mutex> lock(domain_mutex_);
            domain_task_ = DomainPhaseTask::kDramChannel;
            domain_next_core_.store(0, std::memory_order_relaxed);
            domain_task_count_ = task_count;
            domain_index_task_ = &task;
            domain_workers_pending_ = domain_worker_count_;
            domain_error_ = nullptr;
            ++domain_generation_;
        }
        domain_cv_.notify_all();
        std::exception_ptr error;
        {
            std::unique_lock<std::mutex> lock(domain_mutex_);
            domain_done_cv_.wait(lock, [&] {
                return domain_workers_pending_ == 0;
            });
            error = domain_error_;
            domain_task_ = DomainPhaseTask::kNone;
            domain_task_count_ = 0;
            domain_index_task_ = nullptr;
        }
        if (error) std::rethrow_exception(error);
    }

    void stop_workers() {
        {
            std::lock_guard<std::mutex> lock(queue_mutex_);
            stopping_ = true;
        }
        {
            std::lock_guard<std::mutex> lock(domain_mutex_);
            domain_stopping_ = true;
        }
        producer_cv_.notify_all();
        consumer_cv_.notify_all();
        domain_cv_.notify_all();
        for (auto& worker : workers_) {
            if (worker.joinable()) worker.join();
        }
        workers_.clear();
        for (auto& worker : domain_workers_) {
            if (worker.joinable()) worker.join();
        }
        domain_workers_.clear();
    }

    void worker_loop(std::size_t thread_index) {
        auto& thread = *threads_[thread_index];
        const auto core = thread.bound_core;
        while (true) {
            {
                std::unique_lock<std::mutex> lock(queue_mutex_);
                producer_cv_.wait(lock, [&] {
                    return stopping_ ||
                           (!producer_finished_[core] &&
                            chunk_queues_[core].size() <
                                config_.lookahead_chunks);
                });
                if (stopping_) return;
            }
            try {
                auto chunk = produce_thread_chunk(thread_index);
                const bool reached_end = chunk->reached_end;
                {
                    std::lock_guard<std::mutex> lock(queue_mutex_);
                    chunk_queues_[core].push_back(std::move(chunk));
                    ++resident_chunks_;
                    max_resident_chunks_ =
                        std::max(max_resident_chunks_, resident_chunks_);
                    if (reached_end) {
                        producer_finished_[core] = true;
                        thread.run_state = ThreadRunState::kFinished;
                    }
                }
                consumer_cv_.notify_one();
            } catch (...) {
                {
                    std::lock_guard<std::mutex> lock(queue_mutex_);
                    if (!worker_error_) {
                        worker_error_ = std::current_exception();
                    }
                    producer_finished_[core] = true;
                }
                consumer_cv_.notify_all();
                return;
            }
        }
    }

    void domain_worker_loop() {
        std::uint64_t observed_generation = 0;
        while (true) {
            DomainPhaseTask task = DomainPhaseTask::kNone;
            {
                std::unique_lock<std::mutex> lock(domain_mutex_);
                domain_cv_.wait(lock, [&] {
                    return domain_stopping_ ||
                           domain_generation_ != observed_generation;
                });
                if (domain_stopping_) return;
                observed_generation = domain_generation_;
                task = domain_task_;
            }

            try {
                while (true) {
                    const auto core = domain_next_core_.fetch_add(
                        1, std::memory_order_relaxed);
                    if (core >= domain_task_count_) break;
                    if (task == DomainPhaseTask::kPrivatePreview) {
                        preview_core(core);
                    } else if (task == DomainPhaseTask::kTimingFeedback) {
                        compute_core_timing_feedback(
                            core, *timing_event_feedback_, *timing_begin_,
                            *timing_end_, *timing_output_);
                    } else if (task == DomainPhaseTask::kDramChannel) {
                        (*domain_index_task_)(core);
                    } else {
                        throw std::logic_error(
                            "domain worker received an empty phase task");
                    }
                }
            } catch (...) {
                std::lock_guard<std::mutex> lock(domain_mutex_);
                if (!domain_error_) {
                    domain_error_ = std::current_exception();
                }
            }

            {
                std::lock_guard<std::mutex> lock(domain_mutex_);
                if (--domain_workers_pending_ == 0) {
                    domain_done_cv_.notify_one();
                }
            }
        }
    }

    std::unique_ptr<CoreChunk> produce_thread_chunk(
        std::size_t thread_index) {
        auto& thread = *threads_[thread_index];
        const auto core_id = thread.bound_core;
        auto& core = *cores_[core_id];
        auto chunk = std::make_unique<CoreChunk>();
        chunk->interval_bound = core.interval != nullptr;

        const auto base_per_uop =
            kCycleUnit / config_.issue_width;
        std::uint64_t pending_q16 = 0;
        std::uint32_t retired_this_chunk = 0;
        std::uint32_t event_ordinal = 0;
        TraceRecord record;
        chunk->memory.reserve(
            static_cast<std::size_t>(config_.chunk_instructions / 3 + 8));
        if (core.interval) {
            chunk->uops.reserve(config_.chunk_instructions);
        }

        while (retired_this_chunk < config_.chunk_instructions) {
            if (!thread.trace->next(record)) {
                chunk->reached_end = true;
                chunk->measurement_boundary =
                    thread.trace->measurement_boundary_pending();
                break;
            }
            const auto* syscall_metadata =
                thread.trace->current_syscall_metadata();
            if (thread.page_fault_allocation_armed &&
                thread.page_fault_records_since_allocation != UINT64_MAX) {
                ++thread.page_fault_records_since_allocation;
            }
            ++chunk->counters.records;
            bool branch_miss = false;
            BranchPredictionResult branch_prediction;
            if (record.retires()) {
                ++chunk->counters.retired_uops;
                ++retired_this_chunk;
                if (record.is_serializing()) {
                    ++chunk->counters.serializing_uops;
                }
                if (record.is_syscall()) {
                    ++chunk->counters.syscall_uops;
                    if (const auto* profile =
                            config_.syscall_kernel_event_profile(
                                record.syscall_number())) {
                        accumulate_kernel_event(
                            chunk->counters.syscall_kernel, *profile);
                    }
                    if (config_.page_fault_allocation_syscalls.count(
                            record.syscall_number()) != 0) {
                        thread.page_fault_allocation_armed = true;
                        thread.page_fault_allocation_syscall_number =
                            record.syscall_number();
                        thread.page_fault_records_since_allocation = 0;
                    }
                    if (config_.page_fault_syscall_semantic_model) {
                        apply_syscall_page_fault_semantics(
                            thread, record, syscall_metadata);
                    }
                }
                if (!has_flag(record.flags, kMicroOp) ||
                    has_flag(record.flags, kLastMicroOp)) {
                    ++chunk->counters.retired_instructions;
                }
            }

            if (has_flag(record.flags, kBranch)) {
                if (has_flag(record.flags, kBranchOutcomeValid)) {
                    branch_prediction = core.predictor.process(
                        record, chunk->counters.branch,
                        config_.l1i_enabled &&
                                config_.l1i_speculative_path_state
                            ? thread.trace.get()
                            : nullptr,
                        config_.rob_entries);
                    if (branch_prediction.miss) {
                        branch_miss = true;
                        chunk->counters.branch_penalty_cycles +=
                            config_.branch.mispredict_penalty;
                    }
                } else {
                    ++chunk->counters.branches_without_outcome;
                }
            }

            if (record.is_memory() &&
                config_.require_virtual_page_token &&
                (!has_flag(record.flags, kVirtualPageToken) ||
                 record.virtual_page_token() == 0)) {
                constexpr std::uint64_t kBasePageBytes = 4096;
                const auto page_offset =
                    record.address & (kBasePageBytes - 1);
                const bool provably_cross_page =
                    record.size != 0 &&
                    page_offset + record.size > kBasePageBytes;
                if (!config_.allow_cross_page_without_virtual_token ||
                    !provably_cross_page) {
                    throw std::runtime_error(
                        "core " + std::to_string(core_id) +
                        " memory record lacks a virtual-page token while "
                        "trace.require_virtual_page_token=true");
                }
            }

            bool selected_page_fault = false;
            bool boundary_inflight_page_fault = false;
            if (record.retires() && record.is_memory()) {
                if (!has_flag(record.flags, kVirtualPageToken) ||
                    record.virtual_page_token() == 0) {
                    ++chunk->counters.page_fault_untracked_accesses;
                } else if (thread.seen_virtual_page_tokens.insert(
                               record.virtual_page_token()).second) {
                    const VirtualPageMapping* mapping = nullptr;
                    if (config_.page_fault_syscall_semantic_model ||
                        config_.page_fault_initial_pte_state_model) {
                        mapping = thread.trace->virtual_page_mapping(
                            record.virtual_page_token());
                        if (mapping == nullptr) {
                            ++chunk->counters
                                  .page_fault_virtual_page_map_misses;
                            throw std::runtime_error(
                                "page-fault semantic/initial-PTE models "
                                "require a complete FST virtual-page map");
                        }
                    }

                    bool process_page_owner = true;
                    bool pte_state_decision = false;
                    if (config_.page_fault_initial_pte_state_model) {
                        const auto* process_page =
                            process_memory_.find(mapping->virtual_page);
                        if (process_page == nullptr) {
                            throw std::runtime_error(
                                "initial-PTE process catalog is missing "
                                "virtual page " +
                                std::to_string(mapping->virtual_page));
                        }
                        process_page_owner = process_memory_.owns(
                            *process_page, thread.thread_id,
                            record.virtual_page_token());
                        if (!process_page_owner) {
                            ++chunk->counters
                                  .page_fault_process_shared_duplicate_pages;
                        } else if (thread.measurement_phase) {
                            if (process_page
                                    ->measurement_pte_state_valid) {
                                pte_state_decision = true;
                                ++chunk->counters
                                      .page_fault_measurement_pte_known_pages;
                                if (process_page
                                        ->measurement_pte_present) {
                                    ++chunk->counters
                                          .page_fault_measurement_pte_present_pages;
                                } else {
                                    ++chunk->counters
                                          .page_fault_measurement_pte_nonpresent_pages;
                                    if (mapping
                                            ->measurement_boundary_inflight_fault) {
                                        // The producer saw this precise #PF
                                        // enter before the global marker. Its
                                        // retried access is measured, but the
                                        // oracle deliberately starts at that
                                        // first user commit and excludes the
                                        // already-running handler. Preserve
                                        // only the handler's page-fill state.
                                        boundary_inflight_page_fault = true;
                                        ++chunk->counters
                                              .page_fault_measurement_boundary_inflight_suppressed;
                                    } else {
                                        selected_page_fault = true;
                                        ++chunk->counters
                                              .page_fault_measurement_pte_selected;
                                    }
                                }
                            } else {
                                ++chunk->counters
                                      .page_fault_measurement_pte_unknown_pages;
                            }
                        } else if (process_page
                                       ->initial_pte_state_valid) {
                            pte_state_decision = true;
                            ++chunk->counters
                                  .page_fault_initial_pte_known_pages;
                            if (process_page->initial_pte_present) {
                                ++chunk->counters
                                      .page_fault_initial_pte_present_pages;
                            } else {
                                ++chunk->counters
                                      .page_fault_initial_pte_nonpresent_pages;
                                selected_page_fault = true;
                                ++chunk->counters
                                      .page_fault_initial_pte_selected;
                            }
                        } else {
                            ++chunk->counters
                                  .page_fault_initial_pte_unknown_pages;
                        }
                    }

                    if (process_page_owner) {
                        bool syscall_semantic_candidate = false;
                        if (!pte_state_decision &&
                            config_.page_fault_syscall_semantic_model) {
                            syscall_semantic_candidate =
                                contains_virtual_page(
                                    thread.demand_faultable_mappings,
                                    mapping->virtual_page);
                            if (syscall_semantic_candidate) {
                                ++chunk->counters
                                      .page_fault_syscall_semantic_candidates;
                                if (record.is_write()) {
                                    ++chunk->counters
                                          .page_fault_syscall_semantic_write_candidates;
                                }
                            } else if (record.is_write()) {
                                ++chunk->counters
                                      .page_fault_syscall_semantic_fallback_write_candidates;
                            }
                        }
                        ++chunk->counters
                              .page_fault_first_touch_candidates;
                        if (record.is_write()) {
                            ++chunk->counters
                                  .page_fault_first_touch_write_candidates;
                        }
                        if (thread.page_fault_allocation_armed) {
                            const auto distance =
                                thread.page_fault_records_since_allocation;
                            auto& syscall_candidates = chunk->counters
                                .page_fault_allocation_by_syscall
                                    [thread
                                         .page_fault_allocation_syscall_number];
                            for (std::size_t index = 0;
                                 index <
                                 kPageFaultAllocationRecencyUpperBounds.size();
                                 ++index) {
                                if (distance <=
                                    kPageFaultAllocationRecencyUpperBounds
                                        [index]) {
                                    ++chunk->counters
                                          .page_fault_allocation_recency_candidates
                                              [index];
                                    ++syscall_candidates
                                          .recency_candidates[index];
                                    if (record.is_write()) {
                                        ++chunk->counters
                                              .page_fault_allocation_recency_write_candidates
                                                  [index];
                                        ++syscall_candidates
                                              .recency_write_candidates[index];
                                    }
                                    break;
                                }
                            }
                        }
                        const bool allocation_candidate =
                            thread.page_fault_allocation_armed &&
                            !config_.page_fault_allocation_syscalls.empty() &&
                            (config_
                                     .page_fault_allocation_window_records ==
                                 0 ||
                             thread.page_fault_records_since_allocation <=
                                 config_
                                     .page_fault_allocation_window_records);
                        if (allocation_candidate) {
                            ++chunk->counters
                                  .page_fault_allocation_candidates;
                        } else if (record.is_write()) {
                            ++chunk->counters
                                  .page_fault_background_candidates;
                            ++chunk->counters
                                  .page_fault_background_write_candidates;
                        } else {
                            ++chunk->counters
                                  .page_fault_background_candidates;
                            ++chunk->counters
                                  .page_fault_background_read_candidates;
                        }
                        if (config_.page_fault_event_model ||
                            config_.page_fault_cache_state_model) {
                            if (pte_state_decision) {
                                // The phase-appropriate exact snapshot
                                // decision was made above.
                            } else if (
                                config_.page_fault_syscall_semantic_model) {
                                selected_page_fault =
                                    syscall_semantic_candidate;
                                if (!selected_page_fault &&
                                    record.is_write()) {
                                    auto& accumulator = thread
                                        .page_fault_syscall_semantic_fallback_write_probability_accumulator;
                                    accumulator += config_
                                        .page_fault_syscall_semantic_fallback_write_probability_ppm;
                                    if (accumulator >= 1'000'000) {
                                        accumulator -= 1'000'000;
                                        selected_page_fault = true;
                                        ++chunk->counters
                                              .page_fault_syscall_semantic_fallback_write_selected;
                                    }
                                }
                            } else {
                                auto* probability_accumulator = &thread
                                    .page_fault_probability_accumulator;
                                auto probability_ppm =
                                    config_.page_fault_probability_ppm;
                                if (allocation_candidate) {
                                    const auto channel =
                                        record.is_write() ? 1u : 0u;
                                    probability_accumulator = &thread
                                        .page_fault_allocation_probability_accumulators
                                            [thread
                                                 .page_fault_allocation_syscall_number]
                                            [channel];
                                    probability_ppm = config_
                                        .page_fault_allocation_probability_for(
                                            thread
                                                .page_fault_allocation_syscall_number,
                                            record.is_write());
                                } else if (record.is_write()) {
                                    probability_accumulator = &thread
                                        .page_fault_background_write_probability_accumulator;
                                    probability_ppm = config_
                                        .page_fault_background_write_probability_ppm;
                                }
                                *probability_accumulator += probability_ppm;
                                if (*probability_accumulator >= 1'000'000) {
                                    *probability_accumulator -= 1'000'000;
                                    selected_page_fault = true;
                                }
                            }
                            if (selected_page_fault &&
                                config_.page_fault_event_model) {
                                accumulate_kernel_event(
                                    chunk->counters.page_fault_kernel,
                                    config_.page_fault_event_profile);
                            }
                        }
                    }
                }
            }

            IntervalTiming interval_timing;
            std::size_t interval_uop_index = 0;
            if (record.retires()) {
                if (core.interval) {
                    if (config_.response_rename_feedback &&
                        !record.has_destination_class_counts()) {
                        throw std::runtime_error(
                            "core.response_rename_feedback requires FST "
                            "destination class counts");
                    }
                    if (selected_page_fault &&
                        config_.page_fault_event_model) {
                        core.interval->inject_kernel_pause(
                            config_.page_fault_event_profile.service_cycles);
                    }
                    interval_timing =
                        core.interval->schedule(
                            record, branch_miss,
                            branch_prediction.predicted_taken,
                            branch_prediction.predicted_target,
                            branch_prediction.target_available,
                            thread.trace.get(),
                            branch_prediction.speculative_path.empty()
                                ? nullptr
                                : &branch_prediction.speculative_path);
                    chunk->counters.syscall_drain_cycles +=
                        interval_timing.syscall_drain_cycles;
                    chunk->counters.syscall_service_cycles +=
                        interval_timing.syscall_service_cycles;
                    chunk->counters.syscall_restart_cycles +=
                        interval_timing.syscall_restart_cycles;
                    chunk->counters.branch_shadow_uops +=
                        interval_timing.branch_shadow_uops;
                    chunk->counters.branch_shadow_cycles +=
                        interval_timing.branch_shadow_cycles;
                    if (interval_timing.fetch_buffer_transition) {
                        ++chunk->counters.fetch_buffer_transitions;
                        chunk->counters.fetch_buffer_refill_delay_cycles +=
                            interval_timing.fetch_buffer_refill_delay_cycles;
                        chunk->counters.fetch_block_response_wait_cycles +=
                            interval_timing.fetch_block_response_wait_cycles;
                        chunk->counters.fetch_block_response_hidden_cycles +=
                            interval_timing.fetch_block_response_hidden_cycles;
                        chunk->counters.fetch_block_response_exposed_cycles +=
                            interval_timing.fetch_block_response_exposed_cycles;
                        chunk->counters
                            .fetch_block_response_to_resume_cycles +=
                            interval_timing
                                .fetch_block_response_to_resume_cycles;
                        chunk->counters
                            .fetch_block_request_to_resume_cycles +=
                            interval_timing
                                .fetch_block_request_to_resume_cycles;
                    }
                    if (interval_timing.l1i_access) {
                        ++chunk->counters.l1i.accesses;
                        if (interval_timing.l1i_hit) {
                            ++chunk->counters.l1i.hits;
                        }
                        if (interval_timing.l1i_miss) {
                            ++chunk->counters.l1i.misses;
                        }
                        if (interval_timing.l1i_eviction) {
                            ++chunk->counters.l1i.evictions;
                        }
                        chunk->counters.l1i_miss_stall_cycles +=
                            interval_timing.l1i_miss_stall_cycles;
                    }
                    if (interval_timing.l1i_speculative_entry_access) {
                        ++chunk->counters.l1i_speculative_entry_accesses;
                        if (interval_timing.l1i_speculative_entry_hit) {
                            ++chunk->counters.l1i_speculative_entry_hits;
                        }
                        if (interval_timing.l1i_speculative_entry_miss) {
                            ++chunk->counters.l1i_speculative_entry_misses;
                        }
                        if (interval_timing.l1i_speculative_entry_eviction) {
                            ++chunk->counters
                                  .l1i_speculative_entry_evictions;
                        }
                    }
                    if (interval_timing.l1i_speculative_entry_untracked) {
                        ++chunk->counters
                              .l1i_speculative_entry_untracked;
                    }
                    chunk->counters.l1i_speculative_path_records +=
                        interval_timing.l1i_speculative_path_records;
                    chunk->counters.l1i_speculative_path_accesses +=
                        interval_timing.l1i_speculative_path_accesses;
                    chunk->counters.l1i_speculative_path_hits +=
                        interval_timing.l1i_speculative_path_hits;
                    chunk->counters.l1i_speculative_path_misses +=
                        interval_timing.l1i_speculative_path_misses;
                    chunk->counters.l1i_speculative_path_evictions +=
                        interval_timing.l1i_speculative_path_evictions;
                    chunk->counters
                        .l1i_speculative_path_static_instructions +=
                        interval_timing
                            .l1i_speculative_path_static_instructions;
                    chunk->counters
                        .l1i_speculative_path_operand_instructions +=
                        interval_timing
                            .l1i_speculative_path_operand_instructions;
                    chunk->counters
                        .l1i_speculative_path_read_registers +=
                        interval_timing
                            .l1i_speculative_path_read_registers;
                    chunk->counters
                        .l1i_speculative_path_write_registers +=
                        interval_timing
                            .l1i_speculative_path_write_registers;
                    chunk->counters
                        .l1i_speculative_path_operand_segments +=
                        interval_timing
                            .l1i_speculative_path_operand_segments;
                    chunk->counters.l1i_speculative_path_raw_edges +=
                        interval_timing.l1i_speculative_path_raw_edges;
                    chunk->counters
                        .l1i_speculative_path_dependent_instructions +=
                        interval_timing
                            .l1i_speculative_path_dependent_instructions;
                    chunk->counters
                        .l1i_speculative_path_chain_depth_sum +=
                        interval_timing
                            .l1i_speculative_path_chain_depth_sum;
                    chunk->counters
                        .l1i_speculative_path_chain_depth_max = std::max(
                            chunk->counters
                                .l1i_speculative_path_chain_depth_max,
                            interval_timing
                                .l1i_speculative_path_chain_depth_max);
                    chunk->counters
                        .l1i_speculative_path_operand_rob_prefix_uops_q16 +=
                        interval_timing
                            .l1i_speculative_path_operand_rob_prefix_uops_q16;
                    chunk->counters
                        .l1i_speculative_path_operand_rob_capped_instructions +=
                        interval_timing
                            .l1i_speculative_path_operand_rob_capped_instructions;
                    chunk->counters
                        .l1i_speculative_path_operand_rob_capped_read_registers +=
                        interval_timing
                            .l1i_speculative_path_operand_rob_capped_read_registers;
                    chunk->counters
                        .l1i_speculative_path_operand_rob_capped_write_registers +=
                        interval_timing
                            .l1i_speculative_path_operand_rob_capped_write_registers;
                    chunk->counters
                        .l1i_speculative_path_operand_rob_capped_memory_instructions +=
                        interval_timing
                            .l1i_speculative_path_operand_rob_capped_memory_instructions;
                    chunk->counters
                        .l1i_speculative_path_operand_rob_capped_memory_instructions_max_per_path =
                        std::max(
                            chunk->counters
                                .l1i_speculative_path_operand_rob_capped_memory_instructions_max_per_path,
                            interval_timing
                                .l1i_speculative_path_operand_rob_capped_memory_instructions);
                    chunk->counters
                        .l1i_speculative_path_operand_rob_capped_write_registers_max_per_path =
                        std::max(
                            chunk->counters
                                .l1i_speculative_path_operand_rob_capped_write_registers_max_per_path,
                            interval_timing
                                .l1i_speculative_path_operand_rob_capped_write_registers);
                    chunk->counters
                        .l1i_speculative_path_operand_rob_capped_raw_edges +=
                        interval_timing
                            .l1i_speculative_path_operand_rob_capped_raw_edges;
                    chunk->counters
                        .l1i_speculative_path_operand_rob_capped_dependent_instructions +=
                        interval_timing
                            .l1i_speculative_path_operand_rob_capped_dependent_instructions;
                    chunk->counters
                        .l1i_speculative_path_operand_rob_capped_chain_depth_sum +=
                        interval_timing
                            .l1i_speculative_path_operand_rob_capped_chain_depth_sum;
                    chunk->counters
                        .l1i_speculative_path_operand_rob_capped_chain_depth_max =
                        std::max(
                            chunk->counters
                                .l1i_speculative_path_operand_rob_capped_chain_depth_max,
                            interval_timing
                                .l1i_speculative_path_operand_rob_capped_chain_depth_max);
                    chunk->counters
                        .l1i_speculative_path_memory_instructions +=
                        interval_timing
                            .l1i_speculative_path_memory_instructions;
                    chunk->counters
                        .l1i_speculative_path_memory_page_known +=
                        interval_timing
                            .l1i_speculative_path_memory_page_known;
                    chunk->counters
                        .l1i_speculative_path_memory_page_unstable +=
                        interval_timing
                            .l1i_speculative_path_memory_page_unstable;
                    chunk->counters
                        .l1i_speculative_path_memory_page_transition_samples +=
                        interval_timing
                            .l1i_speculative_path_memory_page_transition_samples;
                    chunk->counters
                        .l1i_speculative_path_memory_page_transition_score_ppm +=
                        interval_timing
                            .l1i_speculative_path_memory_page_transition_score_ppm;
                    chunk->counters
                        .l1i_speculative_path_profiled_instructions +=
                        interval_timing
                            .l1i_speculative_path_profiled_instructions;
                    for (std::size_t profile_pool = 0;
                         profile_pool < kSpeculativeProfilePoolCount;
                         ++profile_pool) {
                        chunk->counters
                            .l1i_speculative_path_profile_uops_q16[
                                profile_pool] +=
                            interval_timing
                                .l1i_speculative_path_profile_uops_q16[
                                    profile_pool];
                    }
                    chunk->counters
                        .l1i_speculative_path_profile_rob_capped_uops_q16 +=
                        interval_timing
                            .l1i_speculative_path_profile_rob_capped_uops_q16;
                    chunk->counters
                        .l1i_speculative_path_conditional_stops +=
                        interval_timing
                            .l1i_speculative_path_conditional_stops;
                    chunk->counters
                        .l1i_speculative_path_indirect_stops +=
                        interval_timing.l1i_speculative_path_indirect_stops;
                    chunk->counters
                        .l1i_speculative_path_static_map_misses +=
                        interval_timing
                            .l1i_speculative_path_static_map_misses;
                    if (interval_timing.l1i_speculative_path_unknown_edge) {
                        ++chunk->counters
                              .l1i_speculative_path_unknown_edges;
                    }
                    chunk->counters.speculative_dtlb.accesses +=
                        interval_timing.speculative_dtlb_accesses;
                    chunk->counters.speculative_dtlb.hits +=
                        interval_timing.speculative_dtlb_hits;
                    chunk->counters.speculative_dtlb.misses +=
                        interval_timing.speculative_dtlb_misses;
                    chunk->counters.speculative_dtlb.untracked +=
                        interval_timing.speculative_dtlb_untracked;
                    if (interval_timing.dtlb_access) {
                        ++chunk->counters.dtlb.accesses;
                        if (interval_timing.dtlb_hit) {
                            ++chunk->counters.dtlb.hits;
                        }
                        if (interval_timing.dtlb_miss) {
                            ++chunk->counters.dtlb.misses;
                        }
                        if (interval_timing.dtlb_untracked) {
                            ++chunk->counters.dtlb.untracked;
                        }
                    }
                    if (interval_timing.dtlb_timing_access) {
                        ++chunk->counters.dtlb_timing.accesses;
                        if (interval_timing.dtlb_timing_hit) {
                            ++chunk->counters.dtlb_timing.hits;
                        }
                        if (interval_timing.dtlb_timing_miss &&
                            !interval_timing.dtlb_timing_merged_miss) {
                            ++chunk->counters.dtlb_timing.misses;
                        }
                        if (interval_timing.dtlb_timing_merged_miss) {
                            ++chunk->counters.dtlb_timing.merged_misses;
                        }
                        if (interval_timing.dtlb_timing_untracked) {
                            ++chunk->counters.dtlb_timing.untracked;
                        }
                        chunk->counters.dtlb_timing.walk_delay_cycles +=
                            interval_timing.translation_delay_cycles;
                    }
                    interval_uop_index = chunk->uops.size();
                    ChunkUopBound bound;
                    bound.producer_dists = record.producer_dists;
                    bound.destination_class_counts =
                        record.destination_class_counts();
                    bound.sequence = core.interval->retired_uops() - 1;
                    if (config_.response_batch_timing_encode) {
                        const auto maximum_cycle = std::max({
                            interval_timing.rename_cycle,
                            interval_timing.dispatch_cycle,
                            interval_timing.issue_cycle,
                            interval_timing.completion_cycle,
                            interval_timing.retire_cycle});
                        (void)cycles_to_fixed(
                            maximum_cycle,
                            "interval timing descriptor");
                        bound.rename_q16 =
                            interval_timing.rename_cycle * kCycleUnit;
                        bound.dispatch_q16 =
                            interval_timing.dispatch_cycle * kCycleUnit;
                        bound.issue_q16 =
                            interval_timing.issue_cycle * kCycleUnit;
                        bound.completion_q16 =
                            interval_timing.completion_cycle * kCycleUnit;
                        bound.retire_q16 =
                            interval_timing.retire_cycle * kCycleUnit;
                    } else {
                        bound.rename_q16 = cycles_to_fixed(
                            interval_timing.rename_cycle);
                        bound.dispatch_q16 = cycles_to_fixed(
                            interval_timing.dispatch_cycle);
                        bound.issue_q16 = cycles_to_fixed(
                            interval_timing.issue_cycle);
                        bound.completion_q16 = cycles_to_fixed(
                            interval_timing.completion_cycle);
                        bound.retire_q16 = cycles_to_fixed(
                            interval_timing.retire_cycle);
                    }
                    bound.first_memory = static_cast<std::uint32_t>(
                        chunk->memory.size());
                    bound.fu_occupancy_cycles =
                        interval_timing.fu_occupancy_cycles;
                    bound.fu_pool = interval_timing.fu_pool;
                    bound.memory_read = has_flag(record.flags, kLoad) ||
                        has_flag(record.flags, kAtomic);
                    bound.memory_write = record.is_write();
                    bound.serialize_before = record.is_syscall();
                    bound.serialize_after = record.is_serializing();
                    chunk->uops.push_back(bound);
                } else {
                    if (selected_page_fault &&
                        config_.page_fault_event_model) {
                        pending_q16 += cycles_to_fixed(
                            config_.page_fault_event_profile.service_cycles,
                            "synthetic page-fault service");
                    }
                    pending_q16 += base_per_uop;
                    if (record.is_syscall()) {
                        // Scalar mode is a serialized compatibility model: an
                        // older-work drain is implicit, while explicit SE
                        // service and restart costs remain observable.
                        const auto service =
                            config_.syscall_service_cycles(
                                record.syscall_number());
                        pending_q16 += cycles_to_fixed(
                            static_cast<std::uint64_t>(
                                service) +
                            config_.syscall_restart_latency);
                        chunk->counters.syscall_service_cycles +=
                            service;
                        chunk->counters.syscall_restart_cycles +=
                            config_.syscall_restart_latency;
                    }
                    if (branch_miss) {
                        pending_q16 += cycles_to_fixed(
                            config_.branch.mispredict_penalty);
                    }
                }
            }

            if (!record.is_memory()) continue;
            if (core.interval && !record.retires()) {
                throw std::runtime_error(
                    thread.trace->description() +
                    ": interval core requires retiring memory records");
            }
            if (record.size == 0) {
                throw std::runtime_error(
                    thread.trace->description() +
                    ": memory record has zero access size");
            }
            const auto bytes = static_cast<std::uint32_t>(record.size);
            const auto line_size = config_.l1d.line_size;
            const bool usable_address =
                has_flag(record.flags, kPhysicalAddress) ||
                !config_.strict_physical_address;
            if (!usable_address) {
                ++chunk->counters.unknown_addresses;
                throw std::runtime_error(
                    "core " + std::to_string(core_id) +
                    " memory record lacks a physical address while "
                    "trace.strict_physical_address=true");
            }
            const auto first_line = record.address / line_size;
            const auto span = static_cast<std::uint64_t>(bytes - 1);
            if (record.address >
                std::numeric_limits<std::uint64_t>::max() - span) {
                throw std::runtime_error(
                    "memory reference address range overflows uint64");
            }
            const auto last_byte = record.address + span;
            const auto last_line = last_byte / line_size;
            const bool fill_page_state =
                (selected_page_fault || boundary_inflight_page_fault) &&
                config_.page_fault_cache_state_model;
            constexpr std::uint64_t kBasePageBytes = 4096;
            const auto page_first_line =
                (record.address / kBasePageBytes) *
                (kBasePageBytes / line_size);
            if (fill_page_state) {
                ++chunk->counters.page_fault_cache_state_pages;
                chunk->counters.page_fault_cache_state_lines +=
                    kBasePageBytes / line_size;
            }
            for (auto line = first_line; line <= last_line; ++line) {
                if (line >= config_.dram.size_bytes / line_size) {
                    if (!config_.allow_mmio_escape) {
                        throw std::runtime_error(
                            "physical memory access exceeds configured DRAM "
                            "capacity; set trace.allow_mmio_escape=true only "
                            "for a full-system MMIO-aware profile");
                    }
                    ++chunk->counters.mmio_escape_accesses;
                    if (line == std::numeric_limits<std::uint64_t>::max()) {
                        break;
                    }
                    continue;
                }
                ++chunk->counters.memory_accesses;
                auto event_time = pending_q16;
                if (core.interval) {
                    event_time = cycles_to_fixed(
                        interval_timing.issue_cycle);
                    // The functional trace does not contain enough memory
                    // disambiguation information to reorder memory UOPs.
                    // Preserve their per-core program order while retaining
                    // the dependency/FU lower bound.
                    const auto issue_bound = event_time;
                    event_time = std::max(
                        event_time, core.last_bound_memory_issue_q16);
                    if (event_time > issue_bound) {
                        ++chunk->counters.memory_order_clamp_events;
                        chunk->counters.memory_order_clamp_cycles +=
                            fixed_to_cycle_ceil(event_time - issue_bound);
                    }
                    core.last_bound_memory_issue_q16 = event_time;
                }
                chunk->memory.push_back(ChunkMemoryEvent{
                    line, event_time, event_ordinal++,
                    static_cast<std::uint32_t>(interval_uop_index),
                    core.interval
                        ? static_cast<std::uint32_t>(
                              interval_timing.completion_cycle -
                              interval_timing.issue_cycle)
                        : 0u,
                    record.is_write(),
                    has_flag(record.flags, kAtomic),
                    has_flag(record.flags, kLoad) ||
                        has_flag(record.flags, kAtomic),
                    fill_page_state && line == first_line,
                    page_first_line});
                if (core.interval) {
                    auto& bound = chunk->uops[interval_uop_index];
                    auto& count = bound.memory_count;
                    if (count == std::numeric_limits<std::uint16_t>::max()) {
                        throw std::overflow_error(
                            "memory UOP spans too many cache lines");
                    }
                    ++count;
                    const auto& event = chunk->memory.back();
                    bound.dispatch_store =
                        bound.dispatch_store || event.write;
                    bound.dispatch_load =
                        bound.dispatch_load || !event.write;
                }
                if (!core.interval) pending_q16 = 0;
                if (line == std::numeric_limits<std::uint64_t>::max()) break;
            }
        }
        if (core.interval) {
            chunk->bound_end_q16 = cycles_to_fixed(
                core.interval->last_retire_cycle());
        } else {
            chunk->tail_q16 = pending_q16;
        }
        if (thread.trace->measurement_boundary_pending()) {
            chunk->reached_end = true;
            chunk->measurement_boundary = true;
        }
        thread.functional_total.add(chunk->counters);
        return chunk;
    }

    std::unique_ptr<CoreChunk> acquire_chunk(std::uint32_t core) {
        std::unique_lock<std::mutex> lock(queue_mutex_);
        if (chunk_queues_[core].empty() && !worker_error_) {
            ++stats_.frontier_waits;
        }
        consumer_cv_.wait(lock, [&] {
            return stopping_ || worker_error_ ||
                   !chunk_queues_[core].empty();
        });
        if (worker_error_) std::rethrow_exception(worker_error_);
        if (stopping_ || chunk_queues_[core].empty()) {
            throw std::runtime_error(
                "per-core producer stopped before end-of-trace");
        }
        auto chunk = std::move(chunk_queues_[core].front());
        chunk_queues_[core].pop_front();
        --resident_chunks_;
        lock.unlock();
        producer_cv_.notify_all();
        ++stats_.chunks_consumed;
        return chunk;
    }

    void load_interval_chunk(std::uint32_t core) {
        while (!finished_[core]) {
            auto chunk = acquire_chunk(core);
            if (!chunk->interval_bound) {
                throw std::logic_error(
                    "interval weave received a scalar core chunk");
            }
            cores_[core]->total += chunk->counters;
            current_indices_[core] = 0;
            current_uop_indices_[core] = 0;
            if (chunk->uops.empty()) {
                if (!chunk->memory.empty()) {
                    throw std::logic_error(
                        "interval chunk has memory without retiring UOPs");
                }
                if (chunk->reached_end) {
                    finished_[core] = true;
                    return;
                }
                continue;
            }
            current_chunks_[core] = std::move(chunk);
            return;
        }
    }

    // Append one producer microbatch to the resident time-epoch buffer.  UOP
    // and memory indices are rebased so the existing feedback path can span
    // producer chunk boundaries without treating them as synchronization
    // points.
    void append_epoch_chunk(std::uint32_t core) {
        if (finished_[core]) return;
        auto incoming = acquire_chunk(core);
        if (!incoming->interval_bound) {
            throw std::logic_error(
                "time epoch received a scalar core chunk");
        }
        cores_[core]->total += incoming->counters;

        if (!current_chunks_[core]) {
            current_chunks_[core] = std::move(incoming);
            current_uop_indices_[core] = 0;
            current_indices_[core] = 0;
            ++stats_.epoch_lookahead_chunks;
            if (current_chunks_[core]->reached_end &&
                current_chunks_[core]->uops.empty()) {
                current_chunks_[core].reset();
                finished_[core] = true;
            }
            return;
        }
        auto& target = *current_chunks_[core];
        if (target.reached_end) {
            throw std::logic_error(
                "time epoch appended beyond end-of-trace");
        }
        if (incoming->memory.size() >
                std::numeric_limits<std::uint32_t>::max() -
                    target.memory.size() ||
            incoming->uops.size() >
                std::numeric_limits<std::uint32_t>::max() -
                    target.uops.size()) {
            throw std::overflow_error(
                "time-epoch resident buffer exceeds uint32 indices");
        }
        const auto memory_offset = target.memory.size();
        const auto uop_offset = target.uops.size();
        target.memory.reserve(memory_offset + incoming->memory.size());
        target.uops.reserve(uop_offset + incoming->uops.size());
        for (auto bound : incoming->uops) {
            bound.first_memory +=
                static_cast<std::uint32_t>(memory_offset);
            target.uops.push_back(std::move(bound));
        }
        for (auto event : incoming->memory) {
            event.uop_index +=
                static_cast<std::uint32_t>(uop_offset);
            event.ordinal = static_cast<std::uint32_t>(target.memory.size());
            target.memory.push_back(std::move(event));
        }
        target.bound_end_q16 = incoming->bound_end_q16;
        target.reached_end = incoming->reached_end;
        ++stats_.epoch_lookahead_chunks;

        if (target.reached_end &&
            current_uop_indices_[core] == target.uops.size()) {
            current_chunks_[core].reset();
            current_uop_indices_[core] = 0;
            current_indices_[core] = 0;
            finished_[core] = true;
        }
    }

    void compact_epoch_buffer(std::uint32_t core) {
        auto& resident = current_chunks_[core];
        if (!resident) return;
        auto& chunk = *resident;
        const auto consumed = current_uop_indices_[core];
        if (consumed == 0) return;
        if (consumed > chunk.uops.size()) {
            throw std::logic_error(
                "time-epoch UOP cursor exceeds resident buffer");
        }
        if (consumed == chunk.uops.size()) {
            const bool reached_end = chunk.reached_end;
            resident.reset();
            current_uop_indices_[core] = 0;
            current_indices_[core] = 0;
            if (reached_end) finished_[core] = true;
            return;
        }
        constexpr std::size_t kMinimumCompactionUops = 4096;
        if (consumed < kMinimumCompactionUops &&
            consumed * 2 < chunk.uops.size()) {
            return;
        }

        const auto consumed_memory = static_cast<std::size_t>(
            chunk.uops[consumed].first_memory);
        if (consumed_memory > chunk.memory.size()) {
            throw std::logic_error(
                "time-epoch memory cursor exceeds resident buffer");
        }
        chunk.uops.erase(chunk.uops.begin(),
                         chunk.uops.begin() +
                             static_cast<std::ptrdiff_t>(consumed));
        chunk.memory.erase(
            chunk.memory.begin(),
            chunk.memory.begin() +
                static_cast<std::ptrdiff_t>(consumed_memory));
        for (auto& bound : chunk.uops) {
            bound.first_memory -=
                static_cast<std::uint32_t>(consumed_memory);
        }
        for (auto& event : chunk.memory) {
            event.uop_index -= static_cast<std::uint32_t>(consumed);
        }
        current_uop_indices_[core] = 0;
        current_indices_[core] = 0;
    }

    // Decode far enough that the last resident dispatch is beyond the epoch
    // horizon.  Dispatch is monotonic, so no unseen UOP (and therefore no
    // unseen memory event) can issue inside the covered epoch.
    void ensure_epoch_lookahead(std::uint64_t horizon_q16) {
        // Pull at most one microbatch per core in each round.  Draining one
        // core all the way to the horizon would leave every other producer
        // blocked behind its full two-chunk queue and serialize bound work.
        while (true) {
            bool all_covered = true;
            for (std::uint32_t core = 0; core < config_.cores; ++core) {
                if (finished_[core]) continue;
                if (current_chunks_[core]) {
                    const auto& chunk = *current_chunks_[core];
                    if (chunk.reached_end ||
                        (!chunk.uops.empty() &&
                         saturating_add(
                             chunk.uops.back().dispatch_q16,
                             interval_gap_q16_[core]) > horizon_q16)) {
                        continue;
                    }
                }
                all_covered = false;
                append_epoch_chunk(core);
            }
            if (all_covered) return;
        }
    }

    void audit_batch_order(
        const std::vector<BatchPending>& batch,
        const std::vector<std::vector<std::uint64_t>>& issue_extra_q16,
        const std::vector<std::size_t>& accepted_begin) {
        if (!config_.interval_full_order_audit &&
            !config_.interval_same_line_order_audit) {
            return;
        }
        if (batch.size() < 2) return;
        const auto corrected = [&](BatchPending pending,
                                   std::size_t rank) {
            pending.proposed_rank = rank;
            const auto& event = current_chunks_[pending.core]
                                    ->memory[pending.index];
            const auto position = static_cast<std::size_t>(event.uop_index) -
                                  accepted_begin[pending.core];
            pending.corrected_issue_q16 = saturating_add(
                pending.issue_q16,
                issue_extra_q16[pending.core][position]);
            return pending;
        };
        const auto corrected_later = [](const BatchPending& left,
                                        const BatchPending& right) {
            if (left.corrected_issue_q16 != right.corrected_issue_q16) {
                return left.corrected_issue_q16 <
                       right.corrected_issue_q16;
            }
            if (left.core != right.core) return left.core < right.core;
            return left.index < right.index;
        };

        if (!config_.interval_full_order_audit) {
            struct LineGroup {
                BatchPending first;
                std::size_t conflict =
                    std::numeric_limits<std::size_t>::max();
            };
            std::unordered_map<std::uint64_t, LineGroup> groups;
            groups.reserve(batch.size());
            std::vector<std::vector<BatchPending>> conflicts;
            for (std::size_t rank = 0; rank < batch.size(); ++rank) {
                const auto& pending = batch[rank];
                const auto line = current_chunks_[pending.core]
                                      ->memory[pending.index].line;
                const auto value = corrected(pending, rank);
                const auto inserted = groups.emplace(
                    line, LineGroup{value});
                if (inserted.second) continue;
                auto& group = inserted.first->second;
                if (group.conflict ==
                    std::numeric_limits<std::size_t>::max()) {
                    group.conflict = conflicts.size();
                    conflicts.push_back({group.first});
                }
                conflicts[group.conflict].push_back(value);
            }
            for (auto& events : conflicts) {
                std::sort(events.begin(), events.end(), corrected_later);
                std::vector<std::size_t> ranks;
                ranks.reserve(events.size());
                for (const auto& event : events) {
                    ranks.push_back(event.proposed_rank);
                }
                stats_.same_line_reordered_pairs +=
                    count_inversions(ranks);
            }
            return;
        }

        auto proposed = batch;
        for (std::size_t rank = 0; rank < proposed.size(); ++rank) {
            proposed[rank] = corrected(proposed[rank], rank);
        }
        std::sort(proposed.begin(), proposed.end(),
                  corrected_later);

        std::vector<std::size_t> corrected_ranks;
        corrected_ranks.reserve(proposed.size());
        std::unordered_map<std::uint64_t, std::vector<std::size_t>>
            same_line_ranks;
        for (const auto& event : proposed) {
            const auto rank = event.proposed_rank;
            corrected_ranks.push_back(rank);
            const auto line =
                current_chunks_[event.core]->memory[event.index].line;
            same_line_ranks[line].push_back(rank);
        }
        stats_.reordered_memory_event_pairs +=
            count_inversions(corrected_ranks);
        for (const auto& line : same_line_ranks) {
            stats_.same_line_reordered_pairs +=
                count_inversions(line.second);
        }
    }

    void audit_corrected_epoch_boundary(
        const std::vector<BatchPending>& batch,
        const std::vector<std::vector<std::uint64_t>>& issue_extra_q16,
        const std::vector<std::size_t>& accepted_begin,
        std::uint64_t horizon_q16, bool time_epoch) {
        // This is a diagnostic linear pass over every memory event. Keep it
        // behind the existing attribution switch so production throughput is
        // unchanged when no detailed CPI audit was requested.
        if (!config_.cpi_attribution || !time_epoch || batch.empty()) return;
        std::vector<std::uint32_t> last_crossing_uop(
            config_.cores, std::numeric_limits<std::uint32_t>::max());
        for (const auto& pending : batch) {
            const auto& event = current_chunks_[pending.core]
                                    ->memory[pending.index];
            const auto position = static_cast<std::size_t>(
                event.uop_index) - accepted_begin[pending.core];
            if (position >= issue_extra_q16[pending.core].size()) {
                throw std::logic_error(
                    "corrected epoch-boundary audit is outside feedback "
                    "range");
            }
            const auto corrected_issue_q16 = saturating_add(
                pending.issue_q16,
                issue_extra_q16[pending.core][position]);
            if (corrected_issue_q16 <= horizon_q16) continue;

            ++stats_.epoch_corrected_issue_horizon_events;
            if (last_crossing_uop[pending.core] != event.uop_index) {
                ++stats_.epoch_corrected_issue_horizon_uops;
                last_crossing_uop[pending.core] = event.uop_index;
            }
            const auto late_cycles = fixed_to_cycle_ceil(
                corrected_issue_q16 - horizon_q16);
            stats_.epoch_corrected_issue_horizon_cycles += late_cycles;
            stats_.epoch_corrected_issue_horizon_max_cycles = std::max(
                stats_.epoch_corrected_issue_horizon_max_cycles,
                late_cycles);
        }
    }

    bool corrected_suffix_cuts(
        const std::vector<BatchPending>& batch,
        const TimingFeedback& timing,
        const std::vector<std::size_t>& accepted_begin,
        const std::vector<std::size_t>& accepted_end,
        std::uint64_t horizon_q16,
        std::vector<std::size_t>& cuts) const {
        cuts = accepted_end;
        bool crossing = false;
        for (const auto& pending : batch) {
            const auto& event = current_chunks_[pending.core]
                                    ->memory[pending.index];
            const auto position = static_cast<std::size_t>(
                event.uop_index) - accepted_begin[pending.core];
            if (position >= timing.issue_extra_q16[pending.core].size()) {
                throw std::logic_error(
                    "corrected suffix candidate is outside timing "
                    "feedback range");
            }
            const auto corrected_issue = saturating_add(
                pending.issue_q16,
                timing.issue_extra_q16[pending.core][position]);
            if (corrected_issue <= horizon_q16) continue;
            crossing = true;
            cuts[pending.core] = std::min(
                cuts[pending.core],
                static_cast<std::size_t>(event.uop_index));
        }
        return crossing;
    }

    bool preflight_corrected_epoch_suffix(
        std::vector<BatchPending>& batch,
        const std::vector<std::vector<MemoryReplay>>& event_feedback,
        const std::vector<std::size_t>& accepted_begin,
        std::vector<std::size_t>& accepted_end,
        std::uint64_t horizon_q16) {
        if (!config_.interval_corrected_suffix_carry || batch.empty()) {
            return false;
        }
        // The optimistic pass can only trim work when target state carried
        // from the preceding epoch already keeps every IQ slot occupied past
        // this horizon.  Current-epoch responses are intentionally zero in
        // preflight and cannot certify a cut; skip the full per-UOP pass in
        // the overwhelmingly common case.
        const auto horizon_cycle = fixed_to_cycle_ceil(horizon_q16);
        bool carried_iq_pressure = false;
        for (std::uint32_t core = 0; core < config_.cores; ++core) {
            const auto& iq = response_iq_ready_cycles_[core];
            carried_iq_pressure = carried_iq_pressure ||
                (!iq.empty() && iq.front() > horizon_cycle);
        }
        if (!carried_iq_pressure) return false;
        const auto original_end = accepted_end;
        const auto original_batch_size = batch.size();
        constexpr std::uint32_t kMaximumPreflightPasses = 8;
        bool changed = false;
        for (std::uint32_t pass = 0;
             pass < kMaximumPreflightPasses && !batch.empty(); ++pass) {
            auto optimistic = compute_timing_feedback(
                event_feedback, accepted_begin, accepted_end);
            std::vector<std::size_t> cuts;
            if (!corrected_suffix_cuts(
                    batch, optimistic, accepted_begin, accepted_end,
                    horizon_q16, cuts)) {
                break;
            }
            if (!changed) ++stats_.corrected_suffix_preflight_epochs;
            changed = true;
            ++stats_.corrected_suffix_preflight_passes;
            bool reduced = false;
            for (std::uint32_t core = 0; core < config_.cores; ++core) {
                if (cuts[core] < accepted_end[core]) {
                    accepted_end[core] = cuts[core];
                    reduced = true;
                }
            }
            if (!reduced) {
                throw std::logic_error(
                    "corrected suffix preflight did not reduce its prefix");
            }
            std::vector<BatchPending> retained;
            retained.reserve(batch.size());
            for (const auto& pending : batch) {
                const auto uop = static_cast<std::size_t>(
                    current_chunks_[pending.core]
                        ->memory[pending.index].uop_index);
                if (uop >= accepted_end[pending.core]) continue;
                auto value = pending;
                value.proposed_rank = retained.size();
                retained.push_back(value);
            }
            batch = std::move(retained);
        }
        if (!changed) return false;

        std::uint64_t original_uops = 0;
        std::uint64_t final_uops = 0;
        std::uint64_t original_active = 0;
        std::uint64_t final_active = 0;
        for (std::uint32_t core = 0; core < config_.cores; ++core) {
            original_uops += original_end[core] - accepted_begin[core];
            final_uops += accepted_end[core] - accepted_begin[core];
            original_active += original_end[core] != accepted_begin[core];
            final_active += accepted_end[core] != accepted_begin[core];
        }
        const auto deferred_uops = original_uops - final_uops;
        const auto deferred_events = original_batch_size - batch.size();
        stats_.corrected_suffix_deferred_uops += deferred_uops;
        stats_.corrected_suffix_deferred_memory_events += deferred_events;
        stats_.interval_accepted_uops -= deferred_uops;
        stats_.interval_active_prefixes -= original_active - final_active;
        return true;
    }

    bool repair_corrected_epoch_suffix(
        std::vector<BatchPending>& batch,
        std::vector<std::vector<MemoryReplay>>& event_feedback,
        const std::vector<std::size_t>& accepted_begin,
        std::vector<std::size_t>& accepted_end,
        SharedSystem::Transaction& epoch_transaction,
        const std::vector<CoreCounters>& core_counters_before,
        std::uint64_t horizon_q16, TimingFeedback& timing) {
        if (!config_.interval_corrected_suffix_carry || batch.empty()) {
            return false;
        }

        std::vector<std::size_t> cuts;
        auto has_crossing = corrected_suffix_cuts(
            batch, timing, accepted_begin, accepted_end,
            horizon_q16, cuts);
        if (!has_crossing) return false;
        ++stats_.corrected_suffix_candidate_epochs;

        const auto original_end = accepted_end;
        const auto original_batch_size = batch.size();
        const auto original_active = static_cast<std::uint64_t>(
            std::count_if(
                accepted_begin.begin(), accepted_begin.end(),
                [&](const auto& begin) {
                    const auto core = static_cast<std::size_t>(
                        &begin - accepted_begin.data());
                    return begin != original_end[core];
                }));

        const auto restore_epoch_entry = [&] {
            shared_->restore(epoch_transaction);
            for (std::uint32_t core = 0; core < config_.cores; ++core) {
                cores_[core]->total = core_counters_before[core];
            }
        };
        const auto rebuild_batch = [&] {
            std::vector<BatchPending> retained;
            retained.reserve(batch.size());
            for (const auto& pending : batch) {
                const auto uop = static_cast<std::size_t>(
                    current_chunks_[pending.core]
                        ->memory[pending.index].uop_index);
                if (uop >= accepted_end[pending.core]) continue;
                auto value = pending;
                value.corrected_issue_q16 = value.issue_q16;
                value.proposed_rank = retained.size();
                retained.push_back(value);
            }
            batch = std::move(retained);
        };
        const auto replay_prefix = [&] {
            restore_epoch_entry();
            rebuild_batch();
            const auto timing_start = shared_->capture_timing_state();
            for (const auto& pending : batch) {
                const auto& event = current_chunks_[pending.core]
                                        ->memory[pending.index];
                event_feedback[pending.core][pending.index] =
                    replay_memory_event(
                        pending.core, event, pending.issue_q16, true,
                        &epoch_transaction);
            }
            timing = compute_timing_feedback(
                event_feedback, accepted_begin, accepted_end);
            if (config_.dram.scheduler == "frfcfs" && !batch.empty()) {
                apply_frfcfs_dram_repair(
                    batch, event_feedback, accepted_begin, accepted_end,
                    timing_start, horizon_q16, timing);
            }
            ++stats_.corrected_suffix_passes;
        };

        constexpr std::uint32_t kMaximumSuffixPasses = 8;
        std::uint32_t pass = 0;
        while (has_crossing && pass < kMaximumSuffixPasses) {
            bool reduced = false;
            for (std::uint32_t core = 0; core < config_.cores; ++core) {
                if (cuts[core] < accepted_end[core]) {
                    accepted_end[core] = cuts[core];
                    reduced = true;
                }
            }
            if (!reduced) {
                throw std::logic_error(
                    "corrected suffix repair did not reduce its prefix");
            }
            replay_prefix();
            has_crossing = corrected_suffix_cuts(
                batch, timing, accepted_begin, accepted_end,
                horizon_q16, cuts);
            ++pass;
        }

        if (has_crossing) {
            // A bounded host algorithm must never commit an uncertified
            // request.  Restore the entire epoch and let the next fixed-Q
            // horizon retry it; this path is conservative but target-safe.
            accepted_end = accepted_begin;
            replay_prefix();
            ++stats_.corrected_suffix_conservative_epochs;
            ++stats_.interval_zero_progress_steps;
        } else {
            ++stats_.corrected_suffix_stable_epochs;
        }

        std::uint64_t final_uops = 0;
        std::uint64_t original_uops = 0;
        std::uint64_t final_active = 0;
        for (std::uint32_t core = 0; core < config_.cores; ++core) {
            original_uops += original_end[core] - accepted_begin[core];
            final_uops += accepted_end[core] - accepted_begin[core];
            final_active += accepted_end[core] != accepted_begin[core];
        }
        const auto deferred_uops = original_uops - final_uops;
        const auto deferred_events = original_batch_size - batch.size();
        stats_.corrected_suffix_deferred_uops += deferred_uops;
        stats_.corrected_suffix_deferred_memory_events += deferred_events;
        stats_.interval_accepted_uops -= deferred_uops;
        stats_.interval_active_prefixes -= original_active - final_active;
        stats_.batch_memory_events -= deferred_events;
        return true;
    }

    PrivatePreviewPlan plan_private_preview(
        const std::vector<BatchPending>& batch) {
        PrivatePreviewPlan plan;
        plan.component_rank_limit.assign(
            static_cast<std::size_t>(config_.cores) *
                certificate_component_sets_,
            batch.size());
        std::vector<std::uint8_t> active_core(config_.cores, 0);
        for (const auto& pending : batch) {
            active_core[pending.core] = 1;
        }

        // Inclusive LLC victims can invalidate arbitrary private lines.  An
        // epoch-local certificate cannot predict those victims without
        // executing the shared hierarchy, so keep all active cores canonical.
        if (!config_.interval_private_preview || config_.inclusive_llc) {
            std::fill(plan.component_rank_limit.begin(),
                      plan.component_rank_limit.end(), 0);
        }

        bool has_atomic = false;
        for (const auto& pending : batch) {
            if (current_chunks_[pending.core]
                    ->memory[pending.index].atomic) {
                has_atomic = true;
                break;
            }
        }
        // Atomic ordering is a global boundary in the current model.  Do not
        // speculate any private hierarchy across it.
        if (has_atomic) {
            std::fill(plan.component_rank_limit.begin(),
                      plan.component_rank_limit.end(), 0);
        }

        if (config_.coherence && !config_.inclusive_llc && !has_atomic) {
            // Reuse a dense reverse-scan table over the common private-set
            // components. With the normal 64-set L1, C32 needs only 2048
            // ranks; this is cheaper than hashing every event and avoids
            // per-epoch allocation churn.
            std::fill(certificate_component_next_rank_.begin(),
                      certificate_component_next_rank_.end(), batch.size());
            prepare_epoch_accessors(batch.size());

            for (const auto& pending : batch) {
                const auto& event = current_chunks_[pending.core]
                                        ->memory[pending.index];
                auto& mask = add_epoch_accessor(event.line);
                mask[pending.core / 64] |=
                    1ull << (pending.core % 64);
            }

            for (std::size_t reverse = batch.size(); reverse != 0;
                 --reverse) {
                const auto rank = reverse - 1;
                const auto& pending = batch[rank];
                const auto& event = current_chunks_[pending.core]
                                        ->memory[pending.index];
                const auto component_set = static_cast<std::size_t>(
                    event.line & (certificate_component_sets_ - 1));
                if (event.write) {
                    auto possible_sharers =
                        shared_->sharers_of(event.line);
                    const auto* accessors =
                        find_epoch_accessors(event.line);
                    if (accessors != nullptr) {
                        for (std::size_t word = 0;
                             word < possible_sharers.size(); ++word) {
                            possible_sharers[word] |=
                                (*accessors)[word];
                        }
                    }
                    for (std::uint32_t group = 0;
                         group < possible_sharers.size(); ++group) {
                        auto word = possible_sharers[group];
                        while (word != 0) {
                            const auto bit = static_cast<std::uint32_t>(
                                __builtin_ctzll(word));
                            const auto core = group * 64 + bit;
                            word &= word - 1;
                            if (core >= config_.cores ||
                                core == pending.core) {
                                continue;
                            }
                            const auto component =
                                static_cast<std::size_t>(core) *
                                    certificate_component_sets_ +
                                component_set;
                            if (plan.component_rank_limit[component] <=
                                rank) {
                                continue;
                            }
                            // Moving this core's private accesses ahead is
                            // exact only until the first invalidation that can
                            // change a later access in the same private-state
                            // component.
                            if (certificate_component_next_rank_[component] !=
                                batch.size()) {
                                plan.component_rank_limit[component] = rank;
                            }
                        }
                    }
                }
                certificate_component_next_rank_[
                    static_cast<std::size_t>(pending.core) *
                        certificate_component_sets_ +
                    component_set] = rank;
            }
        }

        std::vector<std::uint8_t> has_safe(config_.cores, 0);
        std::vector<std::uint8_t> has_unsafe(config_.cores, 0);
        for (std::size_t rank = 0; rank < batch.size(); ++rank) {
            const auto& pending = batch[rank];
            const auto& event = current_chunks_[pending.core]
                                    ->memory[pending.index];
            const auto component =
                static_cast<std::size_t>(pending.core) *
                    certificate_component_sets_ +
                static_cast<std::size_t>(
                    event.line & (certificate_component_sets_ - 1));
            if (rank < plan.component_rank_limit[component]) {
                ++plan.safe_events;
                has_safe[pending.core] = 1;
            } else {
                ++plan.unsafe_events;
                has_unsafe[pending.core] = 1;
            }
        }
        for (std::uint32_t core = 0; core < config_.cores; ++core) {
            if (!active_core[core]) continue;
            plan.safe_cores += has_safe[core];
            plan.unsafe_cores += has_unsafe[core];
        }
        return plan;
    }

    bool private_preview_allowed(
        const PrivatePreviewPlan& plan,
        const BatchPending& pending) const {
        const auto& event = current_chunks_[pending.core]
                                ->memory[pending.index];
        const auto component =
            static_cast<std::size_t>(pending.core) *
                certificate_component_sets_ +
            static_cast<std::size_t>(
                event.line & (certificate_component_sets_ - 1));
        return pending.proposed_rank <
               plan.component_rank_limit[component];
    }

    static bool materializes_shared_event(
        const ChunkMemoryEvent& event, const MemoryReplay& preview) {
        return event.write || event.atomic || preview.l2_evicted ||
               preview.private_level == HitLevel::kLlc ||
               preview.private_level == HitLevel::kUnknown;
    }

    bool try_compute_response_inactive_segment(
        std::uint32_t core, std::size_t begin, std::size_t end,
        TimingFeedback& timing) const {
        if (!config_.response_activity_certificate ||
            !config_.response_sparse_scoreboard ||
            config_.interval_rob_head_suffix_replay ||
            config_.response_rename_feedback || begin == end) {
            return false;
        }
        // Copying the fixed ROB/IQ exit state is not worthwhile for tiny
        // gaps between memory UOPs.  This host-only crossover changes which
        // exact implementation runs, never the target transition.
        constexpr std::size_t kMinimumCertifiedSegmentUops = 64;
        if (end - begin < kMinimumCertifiedSegmentUops) return false;

        const auto& chunk = *current_chunks_[core];
        const auto memory_begin = static_cast<std::size_t>(
            chunk.uops[begin].first_memory);
        const auto memory_end =
            end == chunk.uops.size()
                ? chunk.memory.size()
                : static_cast<std::size_t>(
                      chunk.uops[end].first_memory);
        if (memory_begin != memory_end) return false;

        auto& audit = timing.sparse_counters[core];
        ++audit.activity_candidates;
        const auto reject = [&audit]() {
            ++audit.activity_fallback_segments;
            return false;
        };

        // The activity bit is deliberately conservative: a memory-free
        // range containing a serialize edge still uses the complete
        // response/retirement closure.  No tentative state has been changed
        // when this pre-certificate fails.
        for (auto uop = begin; uop < end; ++uop) {
            const auto& bound = chunk.uops[uop];
            if (bound.memory_count != 0 || bound.serialize_before ||
                bound.serialize_after) {
                return reject();
            }
        }

        // Work on private candidates so that any capacity/dependency edge
        // discovered below can fall back to the complete loop without an
        // undo log.  The copied state is O(ROB+IQ), independent of segment
        // length, and is committed only after the exit certificate passes.
        auto candidate_iq = timing.iq_ready_cycles[core];
        auto candidate_sparse = timing.sparse_rob_entries[core];
        if (candidate_iq.empty() || candidate_sparse.empty()) {
            return reject();
        }
        auto candidate_o3 = timing.o3_counters[core];
        auto candidate_counters = audit;
        auto dispatch_cycle = timing.dispatch_cycle[core];
        auto dispatch_used = timing.dispatch_used[core];
        auto dispatch_cause = timing.dispatch_cause[core];
        auto commit_cycle = timing.commit_cycle[core];
        auto commit_used = timing.commit_used[core];
        auto commit_cause = timing.commit_cause[core];
        auto rob_next_slot = timing.rob_next_slot[core];
        const auto interval_gap_cycles =
            fixed_to_cycle_ceil(interval_gap_q16_[core]);

        for (auto uop = begin; uop < end; ++uop) {
            const auto& bound = chunk.uops[uop];
            const auto proposed_dispatch = saturating_add(
                bound.dispatch_q16 / kCycleUnit,
                interval_gap_cycles);

            // The entry at rob_next_slot is exactly one ROB window older.
            // A live predecessor means this segment is response-active and
            // must take the full capacity path.
            if (bound.sequence >= candidate_sparse.size()) {
                const auto predecessor =
                    bound.sequence - candidate_sparse.size();
                const auto& entry = candidate_sparse[rob_next_slot];
                if (entry.sequence != predecessor) {
                    throw std::logic_error(
                        "sparse ROB predecessor is outside the committed "
                        "completion ring");
                }
                if (entry.retire_cycle > proposed_dispatch) {
                    return reject();
                }
            }

            if (proposed_dispatch > dispatch_cycle) {
                dispatch_cycle = proposed_dispatch;
                dispatch_used = 0;
                dispatch_cause = CriticalCause::kUnattributed;
            }
            if (dispatch_used == config_.dispatch_width) {
                ++dispatch_cycle;
                dispatch_used = 0;
            }
            const auto admitted_dispatch = std::max(
                dispatch_cycle, candidate_iq.front());
            if (admitted_dispatch > proposed_dispatch) {
                return reject();
            }
            ++dispatch_used;
            if (candidate_o3.iq_max_occupancy <
                candidate_iq.size()) {
                const auto active = static_cast<std::uint64_t>(
                    std::count_if(
                        candidate_iq.begin(), candidate_iq.end(),
                        [admitted_dispatch](std::uint64_t ready) {
                            return ready > admitted_dispatch;
                        }));
                candidate_o3.iq_max_occupancy = std::max(
                    candidate_o3.iq_max_occupancy, active + 1);
            }

            const auto consumer_base_issue = saturating_add(
                bound.issue_q16 / kCycleUnit,
                interval_gap_cycles);
            const auto admitted_issue_floor = saturating_add(
                proposed_dispatch, config_.dispatch_to_issue);
            if (admitted_issue_floor > consumer_base_issue) {
                return reject();
            }

            for (const auto distance : bound.producer_dists) {
                if (distance == 0 || distance > bound.sequence) {
                    continue;
                }
                if (distance >= candidate_sparse.size()) {
                    ++candidate_counters.absorbed_edges;
                    continue;
                }
                std::uint64_t producer_completion = 0;
                bool producer_available = false;
                if (distance <= uop - begin) {
                    const auto producer = uop - distance;
                    producer_completion = saturating_add(
                        chunk.uops[producer].completion_q16 /
                            kCycleUnit,
                        interval_gap_cycles);
                    producer_available = true;
                } else {
                    const auto distance_slots =
                        static_cast<std::size_t>(distance);
                    const auto producer_slot =
                        rob_next_slot >= distance_slots
                            ? rob_next_slot - distance_slots
                            : candidate_sparse.size() -
                                  (distance_slots - rob_next_slot);
                    const auto producer_sequence =
                        bound.sequence - distance;
                    const auto& entry =
                        candidate_sparse[producer_slot];
                    if (entry.sequence == producer_sequence) {
                        producer_completion = entry.completion_cycle;
                        producer_available = true;
                        ++candidate_counters.cross_epoch_edges;
                    }
                }
                if (!producer_available ||
                    producer_completion <= consumer_base_issue) {
                    ++candidate_counters.absorbed_edges;
                    continue;
                }
                return reject();
            }

            const auto actual_completion = saturating_add(
                bound.completion_q16 / kCycleUnit,
                interval_gap_cycles);
            replace_min(candidate_iq, consumer_base_issue);

            const auto base_retire = saturating_add(
                bound.retire_q16 / kCycleUnit,
                interval_gap_cycles);
            if (saturating_add(actual_completion,
                               config_.execute_to_commit) >
                base_retire) {
                return reject();
            }
            if (base_retire > commit_cycle) {
                commit_cycle = base_retire;
                commit_used = 0;
                commit_cause = CriticalCause::kUnattributed;
            }
            if (commit_used == config_.commit_width) {
                ++commit_cycle;
                commit_used = 0;
            }
            if (commit_cycle > base_retire) return reject();
            ++commit_used;

            auto& entry = candidate_sparse[rob_next_slot];
            entry.sequence = bound.sequence;
            entry.completion_cycle = actual_completion;
            entry.retire_cycle = base_retire;
            entry.completion_extended = false;
            ++rob_next_slot;
            if (rob_next_slot == candidate_sparse.size()) {
                rob_next_slot = 0;
            }
        }

        ++candidate_counters.activity_certified_segments;
        candidate_counters.activity_certified_uops += end - begin;
        timing.iq_ready_cycles[core] = std::move(candidate_iq);
        timing.o3_counters[core] = candidate_o3;
        timing.dispatch_cycle[core] = dispatch_cycle;
        timing.dispatch_used[core] = dispatch_used;
        timing.dispatch_cause[core] = dispatch_cause;
        timing.rob_next_slot[core] = rob_next_slot;
        timing.commit_cycle[core] = commit_cycle;
        timing.commit_used[core] = commit_used;
        timing.commit_cause[core] = commit_cause;
        timing.sparse_rob_entries[core] =
            std::move(candidate_sparse);
        timing.sparse_counters[core] = candidate_counters;
        timing.interval_extra_q16[core] = 0;
        timing.interval_critical_cause[core] =
            CriticalCause::kUnattributed;
        return true;
    }

    void compute_core_timing_feedback(
        std::uint32_t core,
        const std::vector<std::vector<MemoryReplay>>& event_feedback,
        const std::vector<std::size_t>& accepted_begin,
        const std::vector<std::size_t>& accepted_end,
        TimingFeedback& timing) const {
            const auto begin = accepted_begin[core];
            const auto end = accepted_end[core];
            if (begin == end) return;
            const auto& chunk = *current_chunks_[core];
            const bool sparse_scoreboard =
                config_.response_sparse_scoreboard;
            const bool attribute_cycles = config_.cpi_attribution;
            const auto memory_begin = static_cast<std::size_t>(
                chunk.uops[begin].first_memory);
            const auto memory_end =
                end == chunk.uops.size()
                    ? chunk.memory.size()
                    : static_cast<std::size_t>(
                          chunk.uops[end].first_memory);
            if (memory_end < memory_begin) {
                throw std::logic_error(
                    "interval memory range is not monotonic");
            }
            timing.issue_extra_q16[core].assign(end - begin, 0);
            timing.rob_head_suffix_uops[core].assign(end - begin, 0);
            if (try_compute_response_inactive_segment(
                    core, begin, end, timing)) {
                return;
            }
            std::vector<std::uint64_t> completion_extra_cycles(
                end - begin, 0);
            const auto memory_event_upper_bound =
                memory_end - memory_begin;
            // These calendars start empty at every checkpoint.  If even the
            // upper bound on requests does not exceed capacity, a zero slot
            // necessarily remains at the heap root and no request can stall.
            // Avoid initializing and touching two 256-entry heaps in that
            // common case; this is timing-equivalent, not a capacity bypass.
            std::vector<std::uint64_t> mshr_ready_cycles;
            if (memory_event_upper_bound > config_.l1d_mshrs) {
                mshr_ready_cycles.resize(config_.l1d_mshrs, 0);
            }
            std::vector<std::uint64_t> l2_mshr_ready_cycles;
            if (memory_event_upper_bound > config_.l2_mshrs) {
                l2_mshr_ready_cycles.resize(config_.l2_mshrs, 0);
            }
            auto& sequencer_ready =
                timing.sequencer_ready_cycles[core];
            auto sequencer_counters =
                timing.sequencer_counters[core];
            auto& iq_ready = timing.iq_ready_cycles[core];
            auto o3_counters = timing.o3_counters[core];
            auto rename_releases = timing.rename_releases[core];
            auto rename_live = timing.rename_live[core];
            auto rename_cycle = timing.rename_cycle[core];
            auto rename_used = timing.rename_used[core];
            auto rename_cause = timing.rename_cause[core];
            auto rename_counters = timing.rename_counters[core];
            auto dispatch_cycle = timing.dispatch_cycle[core];
            auto dispatch_used = timing.dispatch_used[core];
            auto dispatch_cause = timing.dispatch_cause[core];
            auto& rob_ready = timing.rob_ready_cycles[core];
            auto& lq_ready = timing.lq_ready_cycles[core];
            auto& sq_ready = timing.sq_ready_cycles[core];
            auto rob_next_slot = timing.rob_next_slot[core];
            auto lq_next_slot = timing.lq_next_slot[core];
            auto sq_next_slot = timing.sq_next_slot[core];
            auto commit_cycle = timing.commit_cycle[core];
            auto commit_used = timing.commit_used[core];
            auto commit_cause = timing.commit_cause[core];
            auto store_drain_ready =
                timing.store_drain_ready_cycle[core];
            auto& sparse_rob = timing.sparse_rob_entries[core];
            auto sparse_counters = timing.sparse_counters[core];
            const auto sparse_rob_entry_slot = rob_next_slot;
            std::vector<std::uint64_t> sparse_retire_cycles;
            if (sparse_scoreboard &&
                config_.response_block_summary) {
                sparse_retire_cycles.resize(sparse_rob.size());
                ++sparse_counters.block_summary_checkpoints;
                sparse_counters.block_summary_uops += end - begin;
            }
            auto response_residuals = timing.response_residuals[core];
            auto head_suffix_open =
                timing.rob_head_suffix_open[core];
            const auto interval_gap_cycles =
                fixed_to_cycle_ceil(interval_gap_q16_[core]);
            std::uint64_t adjusted_end_q16 =
                chunk.uops[end - 1].retire_q16;
            std::uint64_t sparse_last_retire_cycle = 0;
            auto sparse_last_retire_cause =
                CriticalCause::kUnattributed;
            // A response-delayed predecessor must be able to move a syscall's
            // serialize-before edge.  Ordered commit alone is insufficient:
            // it would let the syscall execute early and merely wait in the
            // ROB.  `serial_timing_extra` similarly carries a corrected
            // serialize-after edge to younger UOPs inside this checkpoint.
            auto previous_actual_retire = commit_cycle;
            std::uint64_t serial_timing_extra = 0;
            std::vector<std::uint8_t> resource_candidate(end - begin, 0);
            std::optional<SparseIssueResourceCalendar> resource_calendar;
            if (config_.response_sparse_resource_repair) {
                auto potential_rob = sparse_rob;
                auto potential_next_slot = rob_next_slot;
                bool has_resource_candidate = false;
                for (auto candidate_uop = begin;
                     candidate_uop < end; ++candidate_uop) {
                    const auto& candidate_bound =
                        chunk.uops[candidate_uop];
                    bool candidate = false;
                    for (std::uint32_t offset = 0;
                         offset < candidate_bound.memory_count; ++offset) {
                        const auto event_index =
                            static_cast<std::size_t>(
                                candidate_bound.first_memory) + offset;
                        const auto& event = chunk.memory[event_index];
                        candidate = candidate ||
                            (event.blocks_retirement &&
                             event_feedback[core][event_index]
                                     .exposed_cycles != 0);
                    }
                    for (const auto distance :
                         candidate_bound.producer_dists) {
                        if (distance == 0 ||
                            distance > candidate_bound.sequence ||
                            distance >= potential_rob.size()) {
                            continue;
                        }
                        const auto local = candidate_uop - begin;
                        if (distance <= local) {
                            candidate = candidate ||
                                resource_candidate[local - distance] != 0;
                            continue;
                        }
                        const auto distance_slots =
                            static_cast<std::size_t>(distance);
                        const auto producer_slot =
                            potential_next_slot >= distance_slots
                                ? potential_next_slot - distance_slots
                                : potential_rob.size() -
                                      (distance_slots -
                                       potential_next_slot);
                        const auto producer_sequence =
                            candidate_bound.sequence - distance;
                        const auto& producer =
                            potential_rob[producer_slot];
                        candidate = candidate ||
                            (producer.sequence == producer_sequence &&
                             producer.completion_extended);
                    }
                    const auto local = candidate_uop - begin;
                    resource_candidate[local] = candidate;
                    has_resource_candidate =
                        has_resource_candidate || candidate;
                    auto& entry = potential_rob[potential_next_slot];
                    entry.sequence = candidate_bound.sequence;
                    entry.completion_extended = candidate;
                    ++potential_next_slot;
                    if (potential_next_slot == potential_rob.size()) {
                        potential_next_slot = 0;
                    }
                    sparse_counters.resource_candidates += candidate;
                }
                if (has_resource_candidate) {
                    resource_calendar.emplace(config_);
                }
            }
            for (auto uop = begin; uop < end; ++uop) {
                const auto& bound = chunk.uops[uop];
                bool response_rob_head_crossing = false;
                std::uint64_t dispatch_extra = 0;
                auto uop_dispatch_cause =
                    CriticalCause::kUnattributed;
                auto actual_rename = saturating_add(
                    bound.rename_q16 / kCycleUnit,
                    interval_gap_cycles);
                auto actual_rename_cause =
                    CriticalCause::kUnattributed;
                const bool iq_slot_reserved = !iq_ready.empty();
                if (iq_slot_reserved) {
                    const auto base_dispatch =
                        bound.dispatch_q16 / kCycleUnit;
                    const auto proposed_dispatch = saturating_add(
                        base_dispatch, interval_gap_cycles);
                    auto capacity_dispatch = proposed_dispatch;
                    auto capacity_cause =
                        CriticalCause::kUnattributed;
                    const auto rename_dispatch = saturating_add(
                        actual_rename, config_.rename_to_dispatch);
                    if (rename_dispatch > capacity_dispatch) {
                        capacity_dispatch = rename_dispatch;
                        capacity_cause = actual_rename_cause;
                    }
                    bool dispatch_load = bound.dispatch_load;
                    bool dispatch_store = bound.dispatch_store;
                    if (!config_.response_memory_descriptor) {
                        dispatch_load = false;
                        dispatch_store = false;
                        for (std::uint32_t offset = 0;
                             offset < bound.memory_count; ++offset) {
                            const auto event_index =
                                static_cast<std::size_t>(
                                    bound.first_memory) + offset;
                            const auto& event =
                                chunk.memory[event_index];
                            dispatch_store =
                                dispatch_store || event.write;
                            dispatch_load =
                                dispatch_load || !event.write;
                        }
                    }
                    if (sparse_scoreboard) {
                        const auto gate_sparse_queue = [&] (
                            std::vector<std::uint64_t>& ready,
                            std::uint32_t next_slot,
                            bool fifo,
                            bool used_by_uop,
                            std::uint64_t& full_events,
                            std::uint64_t& stall_cycles,
                            std::uint64_t& max_occupancy,
                            std::uint64_t& crossings,
                            CriticalCause cause) {
                            if (!used_by_uop || ready.empty()) return;
                            const auto before = capacity_dispatch;
                            const auto release_cycle =
                                fifo ? ready[next_slot] : ready.front();
                            const auto earliest_release = saturating_add(
                                release_cycle,
                                static_cast<std::uint64_t>(
                                    config_.iew_to_rename) +
                                    config_.rename_to_dispatch);
                            if (earliest_release > proposed_dispatch) {
                                ++crossings;
                            }
                            if (earliest_release > capacity_dispatch) {
                                capacity_dispatch = earliest_release;
                                if (attribute_cycles) {
                                    capacity_cause = cause;
                                }
                            }
                            if (capacity_dispatch > before) {
                                ++full_events;
                                stall_cycles +=
                                    capacity_dispatch - before;
                                max_occupancy = std::max(
                                    max_occupancy,
                                    static_cast<std::uint64_t>(
                                        ready.size()));
                            }
                        };
                        if (bound.sequence >= sparse_rob.size()) {
                            const auto predecessor =
                                bound.sequence - sparse_rob.size();
                            // `rob_next_slot` always names both the slot to
                            // overwrite for this UOP and the UOP exactly one
                            // ROB window older.  Keeping this cursor avoids
                            // two runtime modulo operations per UOP and makes
                            // the wrap-around invariant explicit.
                            const auto local = uop - begin;
                            std::uint64_t predecessor_retire = 0;
                            bool predecessor_extended = false;
                            if (config_.response_block_summary &&
                                local >= sparse_rob.size()) {
                                const auto predecessor_local =
                                    local - sparse_rob.size();
                                const auto& predecessor_bound =
                                    chunk.uops[begin + predecessor_local];
                                if (predecessor_bound.sequence !=
                                    predecessor) {
                                    throw std::logic_error(
                                        "lazy sparse ROB predecessor "
                                        "sequence mismatch");
                                }
                                predecessor_retire =
                                    sparse_retire_cycles[rob_next_slot];
                                predecessor_extended =
                                    completion_extra_cycles[
                                        predecessor_local] != 0;
                            } else {
                                const auto& entry =
                                    sparse_rob[rob_next_slot];
                                if (entry.sequence != predecessor) {
                                    throw std::logic_error(
                                        "sparse ROB predecessor is outside "
                                        "the committed completion ring");
                                }
                                predecessor_retire =
                                    entry.retire_cycle;
                                predecessor_extended =
                                    entry.completion_extended;
                            }
                            const auto before = capacity_dispatch;
                            const auto rob_release = saturating_add(
                                predecessor_retire,
                                static_cast<std::uint64_t>(
                                    config_.commit_to_rename) +
                                    config_.rename_to_dispatch);
                            if (rob_release > capacity_dispatch) {
                                capacity_dispatch = rob_release;
                                response_rob_head_crossing =
                                    config_
                                        .interval_rob_head_suffix_replay &&
                                    predecessor_extended;
                                if (attribute_cycles) {
                                    capacity_cause =
                                        CriticalCause::kRobCapacity;
                                }
                            }
                            if (capacity_dispatch > before) {
                                ++o3_counters.rob_full_events;
                                o3_counters.rob_stall_cycles +=
                                    capacity_dispatch - before;
                                o3_counters.rob_max_occupancy = std::max(
                                    o3_counters.rob_max_occupancy,
                                    static_cast<std::uint64_t>(
                                        sparse_rob.size()));
                                ++sparse_counters.rob_crossings;
                            }
                        }
                        gate_sparse_queue(
                            lq_ready, lq_next_slot, true, dispatch_load,
                            o3_counters.lq_full_events,
                            o3_counters.lq_stall_cycles,
                            o3_counters.lq_max_occupancy,
                            sparse_counters.lq_crossings,
                            CriticalCause::kLqCapacity);
                        gate_sparse_queue(
                            sq_ready, sq_next_slot, config_.needs_tso,
                            dispatch_store,
                            o3_counters.sq_full_events,
                            o3_counters.sq_stall_cycles,
                            o3_counters.sq_max_occupancy,
                            sparse_counters.sq_crossings,
                            CriticalCause::kSqCapacity);
                    } else if (config_.response_rob_lsq_feedback) {
                        const auto gate = [&] (
                            std::vector<std::uint64_t>& ready,
                            std::uint32_t next_slot,
                            bool fifo,
                            bool used_by_uop,
                            std::uint64_t& full_events,
                            std::uint64_t& stall_cycles,
                            std::uint64_t& max_occupancy) {
                            if (!used_by_uop || ready.empty()) return;
                            const auto before = capacity_dispatch;
                            const auto release_cycle =
                                fifo ? ready[next_slot] : ready.front();
                            const auto backward_delay =
                                &ready == &rob_ready
                                    ? config_.commit_to_rename
                                    : config_.iew_to_rename;
                            const auto earliest_release = saturating_add(
                                release_cycle,
                                static_cast<std::uint64_t>(
                                    backward_delay) +
                                    config_.rename_to_dispatch);
                            capacity_dispatch = std::max(
                                capacity_dispatch, earliest_release);
                            if (capacity_dispatch > before) {
                                ++full_events;
                                stall_cycles +=
                                    capacity_dispatch - before;
                                max_occupancy = std::max(
                                    max_occupancy,
                                    static_cast<std::uint64_t>(
                                        ready.size()));
                            }
                            if (max_occupancy < ready.size()) {
                                const auto active =
                                    static_cast<std::uint64_t>(
                                        std::count_if(
                                            ready.begin(), ready.end(),
                                            [capacity_dispatch](
                                                std::uint64_t release) {
                                                return release >
                                                       capacity_dispatch;
                                            }));
                                max_occupancy = std::max(
                                    max_occupancy, active + 1);
                            }
                        };
                        gate(rob_ready, rob_next_slot, true, true,
                             o3_counters.rob_full_events,
                             o3_counters.rob_stall_cycles,
                             o3_counters.rob_max_occupancy);
                        gate(lq_ready, lq_next_slot, true, dispatch_load,
                             o3_counters.lq_full_events,
                             o3_counters.lq_stall_cycles,
                             o3_counters.lq_max_occupancy);
                        gate(sq_ready, sq_next_slot, config_.needs_tso,
                             dispatch_store,
                             o3_counters.sq_full_events,
                             o3_counters.sq_stall_cycles,
                             o3_counters.sq_max_occupancy);
                    }
                    bool iq_admitted_before_rename = false;
                    if (config_.response_rename_feedback &&
                        iq_ready.front() > capacity_dispatch) {
                        const auto before = capacity_dispatch;
                        capacity_dispatch = iq_ready.front();
                        ++o3_counters.iq_full_events;
                        o3_counters.iq_stall_cycles +=
                            capacity_dispatch - before;
                        o3_counters.iq_max_occupancy = std::max(
                            o3_counters.iq_max_occupancy,
                            static_cast<std::uint64_t>(iq_ready.size()));
                        capacity_cause = CriticalCause::kIqCapacity;
                        iq_admitted_before_rename = true;
                    }
                    if (config_.response_rename_feedback) {
                        const std::array<std::uint64_t,
                                         kTrackedRegisterClasses>
                            capacities{
                                config_.rename_int_free_entries,
                                config_.rename_float_free_entries,
                                config_.rename_vec_free_entries,
                                config_.rename_cc_free_entries};
                        const auto release_through = [&] (
                            std::uint64_t cycle) {
                            while (!rename_releases.empty() &&
                                   rename_releases.front().cycle <= cycle) {
                                const auto release =
                                    rename_releases.front();
                                rename_releases.pop_front();
                                for (std::size_t class_index = 0;
                                     class_index < rename_live.size();
                                     ++class_index) {
                                    const auto count =
                                        release.counts[class_index];
                                    if (count >
                                        rename_live[class_index]) {
                                        throw std::logic_error(
                                            "response rename free-list "
                                            "release underflow");
                                    }
                                    rename_live[class_index] -= count;
                                    rename_counters.released[class_index] +=
                                        count;
                                }
                            }
                        };
                        const auto can_allocate = [&] {
                            for (std::size_t class_index = 0;
                                 class_index < rename_live.size();
                                 ++class_index) {
                                if (rename_live[class_index] +
                                        bound.destination_class_counts[
                                            class_index] >
                                    capacities[class_index]) {
                                    return false;
                                }
                            }
                            return true;
                        };

                        const auto downstream_rename =
                            capacity_dispatch > config_.rename_to_dispatch
                                ? capacity_dispatch -
                                      config_.rename_to_dispatch
                                : 0;
                        const auto proposed_rename = std::max(
                            actual_rename, downstream_rename);
                        actual_rename = std::max(
                            proposed_rename, rename_cycle);
                        release_through(actual_rename);
                        bool capacity_stall = false;
                        while (!can_allocate()) {
                            capacity_stall = true;
                            if (rename_releases.empty()) {
                                throw std::logic_error(
                                    "response rename free list is full with "
                                    "no ordered-retirement release");
                            }
                            actual_rename = std::max(
                                actual_rename,
                                rename_releases.front().cycle);
                            release_through(actual_rename);
                        }
                        if (actual_rename > rename_cycle) {
                            rename_cycle = actual_rename;
                            rename_used = 0;
                            rename_cause = capacity_stall
                                ? CriticalCause::kRenameFreeList
                                : capacity_cause;
                        }
                        if (rename_used == config_.rename_width) {
                            ++rename_cycle;
                            rename_used = 0;
                            release_through(rename_cycle);
                        }
                        actual_rename = rename_cycle;
                        ++rename_used;
                        actual_rename_cause =
                            capacity_stall
                                ? CriticalCause::kRenameFreeList
                                : rename_cause;
                        if (capacity_stall) {
                            ++rename_counters.free_list_stall_uops;
                            rename_counters.free_list_stall_cycles +=
                                actual_rename - proposed_rename;
                        }
                        bool has_destination = false;
                        for (std::size_t class_index = 0;
                             class_index < rename_live.size();
                             ++class_index) {
                            const auto count =
                                bound.destination_class_counts[class_index];
                            has_destination =
                                has_destination || count != 0;
                            rename_live[class_index] += count;
                            rename_counters.allocated[class_index] +=
                                count;
                            rename_counters.max_live[class_index] =
                                std::max(
                                    rename_counters.max_live[class_index],
                                    rename_live[class_index]);
                        }
                        rename_counters.destination_uops +=
                            has_destination;
                        rename_counters.live = rename_live;
                        const auto admitted_rename_dispatch = saturating_add(
                            actual_rename, config_.rename_to_dispatch);
                        if (admitted_rename_dispatch > capacity_dispatch) {
                            capacity_dispatch = admitted_rename_dispatch;
                            capacity_cause = actual_rename_cause;
                        }
                    }
                    if (capacity_dispatch > dispatch_cycle) {
                        dispatch_cycle = capacity_dispatch;
                        dispatch_used = 0;
                        if (attribute_cycles) {
                            dispatch_cause = capacity_cause;
                        }
                    }
                    if (dispatch_used == config_.dispatch_width) {
                        ++dispatch_cycle;
                        dispatch_used = 0;
                    }
                    const auto pipeline_dispatch = dispatch_cycle;
                    auto admitted_dispatch_cause =
                        pipeline_dispatch > proposed_dispatch
                            ? dispatch_cause
                            : CriticalCause::kUnattributed;
                    const auto admitted_dispatch =
                        config_.response_rename_feedback &&
                                iq_admitted_before_rename
                            ? pipeline_dispatch
                            : std::max(
                                  pipeline_dispatch, iq_ready.front());
                    if (admitted_dispatch > pipeline_dispatch) {
                        ++o3_counters.iq_full_events;
                        o3_counters.iq_stall_cycles +=
                            admitted_dispatch - pipeline_dispatch;
                        dispatch_cycle = admitted_dispatch;
                        dispatch_used = 0;
                        if (attribute_cycles) {
                            dispatch_cause =
                                CriticalCause::kIqCapacity;
                            admitted_dispatch_cause =
                                CriticalCause::kIqCapacity;
                        }
                        o3_counters.iq_max_occupancy = std::max(
                            o3_counters.iq_max_occupancy,
                            static_cast<std::uint64_t>(iq_ready.size()));
                    }
                    ++dispatch_used;
                    dispatch_extra =
                        admitted_dispatch - proposed_dispatch;
                    if (attribute_cycles && dispatch_extra != 0) {
                        ++response_residuals.dispatch_moved_uops;
                        response_residuals.dispatch_moved_cycles +=
                            dispatch_extra;
                    }
                    if (attribute_cycles && dispatch_extra != 0) {
                        uop_dispatch_cause =
                            admitted_dispatch_cause;
                    }
                    if (o3_counters.iq_max_occupancy < iq_ready.size()) {
                        const auto active =
                            static_cast<std::uint64_t>(std::count_if(
                                iq_ready.begin(), iq_ready.end(),
                                [admitted_dispatch](std::uint64_t ready) {
                                    return ready > admitted_dispatch;
                                }));
                        o3_counters.iq_max_occupancy = std::max(
                            o3_counters.iq_max_occupancy, active + 1);
                    }
                }

                const auto consumer_base_issue = saturating_add(
                    bound.issue_q16 / kCycleUnit,
                    interval_gap_cycles);
                // A late ROB/IQ admission constrains issue at
                // `dispatch + dispatch_to_issue`; it is not itself an issue
                // delay.  The lower-bound schedule can already contain FU,
                // dependency, or translation slack between dispatch and
                // issue.  Charging the whole dispatch displacement again
                // turns every ROB-full UOP into a critical-path UOP (the B3
                // dense-calendar failure mode).  Carry only the residual
                // that actually crosses the lower-bound issue frontier.
                const auto admitted_issue_floor = saturating_add(
                    saturating_add(
                        bound.dispatch_q16 / kCycleUnit,
                        interval_gap_cycles),
                    dispatch_extra + config_.dispatch_to_issue);
                std::uint64_t dependency_extra =
                    admitted_issue_floor > consumer_base_issue
                        ? admitted_issue_floor - consumer_base_issue
                        : 0;
                auto dependency_cause =
                    dependency_extra != 0
                        ? uop_dispatch_cause
                        : CriticalCause::kUnattributed;
                if (serial_timing_extra > dependency_extra) {
                    dependency_extra = serial_timing_extra;
                    if (attribute_cycles) {
                        dependency_cause = CriticalCause::kDependency;
                    }
                }
                if (bound.serialize_before && bound.sequence != 0) {
                    const auto serialize_issue_floor = saturating_add(
                        saturating_add(previous_actual_retire, 1),
                        static_cast<std::uint64_t>(
                            config_.rename_to_dispatch) +
                            config_.dispatch_to_issue);
                    if (serialize_issue_floor > consumer_base_issue) {
                        const auto serialize_extra =
                            serialize_issue_floor - consumer_base_issue;
                        if (serialize_extra > dependency_extra) {
                            dependency_extra = serialize_extra;
                            if (attribute_cycles) {
                                dependency_cause =
                                    CriticalCause::kDependency;
                            }
                        }
                    }
                }
                for (const auto distance :
                     bound.producer_dists) {
                    if (distance == 0) continue;
                    if (sparse_scoreboard) {
                        if (distance > bound.sequence) continue;
                        // Once a producer is at least one ROB window older,
                        // successful admission of the consumer proves that
                        // producer retired.  Its completion cannot add a
                        // later issue constraint, so no older history is
                        // needed beyond the fixed ROB ring.
                        if (distance >= sparse_rob.size()) {
                            ++sparse_counters.absorbed_edges;
                            continue;
                        }
                        std::uint64_t producer_actual_completion = 0;
                        std::uint64_t producer_completion_extension = 0;
                        bool producer_available = false;
                        if (distance <= uop - begin) {
                            const auto producer = uop - distance;
                            const auto producer_base_completion =
                                saturating_add(
                                    chunk.uops[producer].completion_q16 /
                                        kCycleUnit,
                                    interval_gap_cycles);
                            producer_actual_completion = saturating_add(
                                producer_base_completion,
                                completion_extra_cycles[producer - begin]);
                            producer_completion_extension =
                                completion_extra_cycles[producer - begin];
                            producer_available = true;
                        } else {
                            const auto distance_slots =
                                static_cast<std::size_t>(distance);
                            const auto producer_slot =
                                rob_next_slot >= distance_slots
                                    ? rob_next_slot - distance_slots
                                    : sparse_rob.size() -
                                          (distance_slots - rob_next_slot);
                            const auto producer_sequence =
                                bound.sequence - distance;
                            const auto& entry = sparse_rob[producer_slot];
                            if (entry.sequence == producer_sequence) {
                                producer_actual_completion =
                                    entry.completion_cycle;
                                producer_completion_extension =
                                    entry.completion_extension_cycles;
                                producer_available = true;
                                ++sparse_counters.cross_epoch_edges;
                            }
                        }
                        if (attribute_cycles && producer_available &&
                            producer_completion_extension != 0) {
                            const auto residual =
                                producer_actual_completion >
                                        consumer_base_issue
                                    ? producer_actual_completion -
                                          consumer_base_issue
                                    : 0;
                            const auto propagated = std::min(
                                producer_completion_extension, residual);
                            ++response_residuals.dependency_edges;
                            response_residuals.dependency_input_cycles +=
                                producer_completion_extension;
                            response_residuals
                                .dependency_propagated_cycles +=
                                propagated;
                            response_residuals
                                .dependency_absorbed_cycles +=
                                producer_completion_extension - propagated;
                        }
                        if (!producer_available ||
                            producer_actual_completion <=
                                consumer_base_issue) {
                            ++sparse_counters.absorbed_edges;
                            continue;
                        }
                        const auto producer_extra =
                            producer_actual_completion -
                            consumer_base_issue;
                        if (producer_extra > dependency_extra) {
                            dependency_extra = producer_extra;
                            if (attribute_cycles) {
                                dependency_cause =
                                    CriticalCause::kDependency;
                            }
                        }
                        continue;
                    }
                    if (distance > uop) continue;
                    const auto producer = uop - distance;
                    if (producer >= begin) {
                        // A producer delay matters only after consuming the
                        // OoO slack already present in the lower-bound
                        // schedule. Propagating the producer's entire extra
                        // delay double-counts slack whenever unrelated FU or
                        // queue work already places the consumer later.
                        const auto producer_base_completion =
                            saturating_add(
                                chunk.uops[producer].completion_q16 /
                                    kCycleUnit,
                                interval_gap_cycles);
                        const auto producer_actual_completion =
                            saturating_add(
                                producer_base_completion,
                                completion_extra_cycles[producer - begin]);
                        if (producer_actual_completion >
                            consumer_base_issue) {
                            const auto producer_extra =
                                producer_actual_completion -
                                consumer_base_issue;
                            if (producer_extra > dependency_extra) {
                                dependency_extra = producer_extra;
                                if (attribute_cycles) {
                                    dependency_cause =
                                        CriticalCause::kDependency;
                                }
                            }
                        }
                    }
                }
                if (resource_calendar && bound.memory_count == 0) {
                    const auto repaired_issue =
                        resource_calendar->allocate_issue(
                            bound,
                            saturating_add(
                                consumer_base_issue,
                                dependency_extra));
                    const auto collision_cycles =
                        repaired_issue - saturating_add(
                            consumer_base_issue,
                            dependency_extra);
                    sparse_counters.resource_issue_collision_cycles +=
                        collision_cycles;
                    const auto repaired_extra =
                        repaired_issue - consumer_base_issue;
                    if (repaired_extra > dependency_extra) {
                        dependency_extra = repaired_extra;
                        if (attribute_cycles) {
                            dependency_cause =
                                CriticalCause::kDependency;
                        }
                    }
                    sparse_counters.resource_issue_moves +=
                        repaired_issue > consumer_base_issue;
                }
                std::uint64_t uop_issue_extra = dependency_extra;
                std::uint64_t completion_extra = dependency_extra;
                auto completion_cause = dependency_cause;
                std::uint64_t memory_response_cycle = 0;
                bool regular_store = false;
                bool has_load = false;
                bool has_store = false;
                bool response_seed = false;
                std::uint64_t store_latency_cycles = 0;
                std::uint64_t store_base_issue_cycle = 0;
                for (std::uint32_t offset = 0;
                     offset < bound.memory_count; ++offset) {
                    const auto event_index =
                        static_cast<std::size_t>(bound.first_memory) +
                        offset;
                    const auto& event = chunk.memory[event_index];
                    const auto& feedback =
                        event_feedback[core][event_index];
                    regular_store = regular_store ||
                        (event.write && !event.atomic);
                    has_store = has_store || event.write;
                    has_load = has_load || !event.write;
                    if (event.write) {
                        store_latency_cycles = std::max(
                            store_latency_cycles,
                            feedback.latency_cycles);
                        store_base_issue_cycle = std::max(
                            store_base_issue_cycle,
                            event.delta_q16 / kCycleUnit);
                    }
                    if (feedback.exposed_cycles > (1ull << 40)) {
                        throw std::overflow_error(
                            "unbounded interval memory feedback: core=" +
                            std::to_string(core) + " uop=" +
                            std::to_string(uop) + " event=" +
                            std::to_string(event_index) + " exposed=" +
                            std::to_string(feedback.exposed_cycles));
                    }
                    auto event_issue_extra = dependency_extra;
                    auto event_issue_cause = dependency_cause;
                    const auto base_issue =
                        event.delta_q16 / kCycleUnit;
                    if (!sequencer_ready.empty()) {
                        ++sequencer_counters.requests;
                        const auto proposed_issue = saturating_add(
                            saturating_add(base_issue,
                                           interval_gap_cycles),
                            event_issue_extra);
                        const auto admitted_issue = std::max(
                            proposed_issue, sequencer_ready.front());
                        if (admitted_issue > proposed_issue) {
                            ++sequencer_counters.buffer_full_stalls;
                            sequencer_counters.stall_cycles +=
                                admitted_issue - proposed_issue;
                            event_issue_extra = saturating_add(
                                event_issue_extra,
                                admitted_issue - proposed_issue);
                            if (attribute_cycles) {
                                event_issue_cause =
                                    CriticalCause::kSequencer;
                            }
                            sequencer_counters.max_outstanding = std::max(
                                sequencer_counters.max_outstanding,
                                static_cast<std::uint64_t>(
                                    sequencer_ready.size()));
                        }
                        if (sequencer_counters.max_outstanding <
                            sequencer_ready.size()) {
                            const auto active =
                                static_cast<std::uint64_t>(std::count_if(
                                    sequencer_ready.begin(),
                                    sequencer_ready.end(),
                                    [admitted_issue](std::uint64_t ready) {
                                        return ready > admitted_issue;
                                    }));
                            sequencer_counters.max_outstanding = std::max(
                                sequencer_counters.max_outstanding,
                                active + 1);
                        }
                    }
                    const bool uses_l1_mshr =
                        !mshr_ready_cycles.empty() &&
                        feedback.private_level != HitLevel::kL1;
                    const bool uses_l2_mshr =
                        !l2_mshr_ready_cycles.empty() &&
                        (feedback.private_level == HitLevel::kLlc ||
                         feedback.private_level == HitLevel::kUnknown);
                    if (uses_l1_mshr) {
                        const auto proposed_issue = saturating_add(
                            base_issue, event_issue_extra);
                        if (mshr_ready_cycles.front() > proposed_issue) {
                            event_issue_extra =
                                mshr_ready_cycles.front() - base_issue;
                            if (attribute_cycles) {
                                event_issue_cause =
                                    CriticalCause::kL1Mshr;
                            }
                        }
                    }
                    if (uses_l2_mshr) {
                        const auto proposed_issue = saturating_add(
                            base_issue, event_issue_extra);
                        if (l2_mshr_ready_cycles.front() > proposed_issue) {
                            event_issue_extra =
                                l2_mshr_ready_cycles.front() - base_issue;
                            if (attribute_cycles) {
                                event_issue_cause =
                                    CriticalCause::kL2Mshr;
                            }
                        }
                    }
                    if (attribute_cycles && event_issue_extra != 0) {
                        ++response_residuals.memory_issue_moved_events;
                        response_residuals.memory_issue_moved_cycles +=
                            event_issue_extra;
                        if (feedback.shared_escape) {
                            ++response_residuals
                                  .escape_issue_moved_events;
                            response_residuals
                                .escape_issue_moved_cycles +=
                                event_issue_extra;
                        }
                    }
                    const auto mshr_release = saturating_add(
                        saturating_add(base_issue, event_issue_extra),
                        feedback.latency_cycles);
                    if (uses_l1_mshr) {
                        replace_min(mshr_ready_cycles, mshr_release);
                    }
                    if (uses_l2_mshr) {
                        replace_min(l2_mshr_ready_cycles, mshr_release);
                    }
                    if (!sequencer_ready.empty()) {
                        const auto release = saturating_add(
                            saturating_add(
                                saturating_add(base_issue,
                                               interval_gap_cycles),
                                event_issue_extra),
                            feedback.latency_cycles);
                        replace_min(sequencer_ready, release);
                    }
                    memory_response_cycle = std::max(
                        memory_response_cycle,
                        saturating_add(
                            saturating_add(
                                saturating_add(base_issue,
                                               interval_gap_cycles),
                                event_issue_extra),
                            feedback.latency_cycles));
                    if (event_issue_extra > uop_issue_extra) {
                        uop_issue_extra = event_issue_extra;
                    }
                    if (event.blocks_retirement) {
                        response_seed = response_seed ||
                            feedback.exposed_cycles != 0;
                        if (attribute_cycles &&
                            feedback.exposed_cycles != 0) {
                            ++response_residuals.response_seed_events;
                            response_residuals.response_seed_cycles +=
                                feedback.exposed_cycles;
                        }
                        const auto event_completion_extra =
                            saturating_add(event_issue_extra,
                                           feedback.exposed_cycles);
                        if (event_completion_extra > completion_extra) {
                            completion_extra = event_completion_extra;
                            if (attribute_cycles) {
                                completion_cause =
                                    feedback.exposed_cycles != 0
                                        ? CriticalCause::kMemoryResponse
                                        : event_issue_cause;
                            }
                        }
                    } else {
                        if (event_issue_extra > completion_extra) {
                            completion_extra = event_issue_extra;
                            if (attribute_cycles) {
                                completion_cause = event_issue_cause;
                            }
                        }
                    }
                }
                if (resource_calendar && bound.memory_count != 0) {
                    const auto proposed_issue = saturating_add(
                        consumer_base_issue, uop_issue_extra);
                    const auto repaired_issue =
                        resource_calendar->allocate_issue(
                            bound, proposed_issue);
                    const auto collision_cycles =
                        repaired_issue - proposed_issue;
                    sparse_counters.resource_issue_collision_cycles +=
                        collision_cycles;
                    sparse_counters.resource_issue_moves +=
                        repaired_issue > consumer_base_issue;
                    if (collision_cycles != 0) {
                        uop_issue_extra = saturating_add(
                            uop_issue_extra, collision_cycles);
                        completion_extra = saturating_add(
                            completion_extra, collision_cycles);
                        memory_response_cycle = saturating_add(
                            memory_response_cycle, collision_cycles);
                        if (attribute_cycles) {
                            completion_cause =
                                CriticalCause::kDependency;
                        }
                    }
                }
                if (resource_calendar) {
                    const auto base_completion = saturating_add(
                        bound.completion_q16 / kCycleUnit,
                        interval_gap_cycles);
                    const auto proposed_completion = saturating_add(
                        base_completion, completion_extra);
                    const auto repaired_completion =
                        resource_calendar->allocate_writeback(
                            proposed_completion);
                    const auto collision_cycles =
                        repaired_completion - proposed_completion;
                    sparse_counters.resource_writeback_collision_cycles +=
                        collision_cycles;
                    sparse_counters.resource_writeback_moves +=
                        repaired_completion > base_completion;
                    if (collision_cycles != 0) {
                        completion_extra = saturating_add(
                            completion_extra, collision_cycles);
                        if (attribute_cycles) {
                            completion_cause =
                                CriticalCause::kDependency;
                        }
                    }
                }
                const auto actual_issue = saturating_add(
                    saturating_add(
                        bound.issue_q16 / kCycleUnit,
                        interval_gap_cycles),
                    uop_issue_extra);
                const auto actual_completion = saturating_add(
                    saturating_add(
                        bound.completion_q16 / kCycleUnit,
                        interval_gap_cycles),
                    completion_extra);
                if (attribute_cycles && response_seed) {
                    ++response_residuals.response_seed_uops;
                }
                if (attribute_cycles && completion_extra != 0) {
                    ++response_residuals.completion_extended_uops;
                    response_residuals.completion_extension_cycles +=
                        completion_extra;
                }
                if (iq_slot_reserved) {
                    auto iq_release = actual_issue;
                    if (bound.memory_count != 0) {
                        iq_release = regular_store
                                         ? actual_completion
                                         : std::max(actual_completion,
                                                    memory_response_cycle);
                    }
                    replace_min(iq_ready, iq_release);
                }
                std::uint64_t actual_retire = saturating_add(
                    bound.retire_q16 / kCycleUnit,
                    interval_gap_cycles);
                auto actual_retire_cause =
                    CriticalCause::kUnattributed;
                bool head_suffix_crossing = false;
                if (sparse_scoreboard) {
                    const auto base_retire = actual_retire;
                    const auto base_completion = saturating_add(
                        bound.completion_q16 / kCycleUnit,
                        interval_gap_cycles);
                    const auto completion_extension =
                        actual_completion > base_completion
                            ? actual_completion - base_completion
                            : 0;
                    auto exposed_extension = completion_extension;
                    // The structural path uses exposure=1. Avoid a floating
                    // conversion and libm ceil call for every UOP in that
                    // overwhelmingly common case; fractional exposure is an
                    // explicit experiment and keeps the exact old rounding.
                    if (config_.response_retire_exposure != 1.0) {
                        exposed_extension =
                            static_cast<std::uint64_t>(std::ceil(
                                static_cast<double>(completion_extension) *
                                config_.response_retire_exposure));
                    }
                    const auto exposed_completion = saturating_add(
                        base_completion, exposed_extension);
                    const auto completion_retire = saturating_add(
                        exposed_completion, config_.execute_to_commit);
                    head_suffix_crossing =
                        response_rob_head_crossing;
                    if (head_suffix_crossing && !head_suffix_open) {
                        head_suffix_open = true;
                        ++sparse_counters.head_suffix_anchors;
                    }
                    if (attribute_cycles && exposed_extension != 0) {
                        const auto retire_residual =
                            completion_retire > base_retire
                                ? completion_retire - base_retire
                                : 0;
                        const auto retire_propagated = std::min(
                            exposed_extension, retire_residual);
                        ++response_residuals.retire_seed_uops;
                        response_residuals.retire_input_cycles +=
                            exposed_extension;
                        response_residuals.retire_propagated_cycles +=
                            retire_propagated;
                        response_residuals.retire_absorbed_cycles +=
                            exposed_extension - retire_propagated;
                    }
                    if (completion_retire > base_retire) {
                        actual_retire = completion_retire;
                        if (attribute_cycles) {
                            actual_retire_cause = completion_cause;
                        }
                    } else {
                        actual_retire = base_retire;
                    }
                    if (actual_retire > commit_cycle) {
                        commit_cycle = actual_retire;
                        commit_used = 0;
                        if (attribute_cycles) {
                            commit_cause = actual_retire_cause;
                        }
                    }
                    if (commit_used == config_.commit_width) {
                        ++commit_cycle;
                        commit_used = 0;
                    }
                    actual_retire = commit_cycle;
                    if (attribute_cycles) {
                        actual_retire_cause = commit_cause;
                    }
                    ++commit_used;
                    if (attribute_cycles &&
                        actual_retire > base_retire) {
                        ++response_residuals
                              .ordered_retire_moved_uops;
                        response_residuals
                            .ordered_retire_moved_cycles +=
                            actual_retire - base_retire;
                    }
                    if (has_load) {
                        lq_ready[lq_next_slot] = actual_retire;
                        lq_next_slot = static_cast<std::uint32_t>(
                            (lq_next_slot + 1) % lq_ready.size());
                    }
                    if (has_store) {
                        auto sq_release = std::max(
                            actual_retire, memory_response_cycle);
                        if (regular_store) {
                            auto store_send = actual_retire;
                            if (config_.needs_tso) {
                                store_send = std::max(
                                    store_send, store_drain_ready);
                                o3_counters.tso_store_stall_cycles +=
                                    store_send - actual_retire;
                            }
                            sq_release = saturating_add(
                                store_send, store_latency_cycles);
                            if (config_.needs_tso) {
                                store_drain_ready = sq_release;
                            }
                            const auto proposed_store_issue =
                                saturating_add(
                                    store_base_issue_cycle,
                                    interval_gap_cycles);
                            if (store_send > proposed_store_issue) {
                                uop_issue_extra = std::max(
                                    uop_issue_extra,
                                    store_send - proposed_store_issue);
                            }
                        }
                        if (config_.needs_tso) {
                            sq_ready[sq_next_slot] = sq_release;
                            sq_next_slot = static_cast<std::uint32_t>(
                                (sq_next_slot + 1) % sq_ready.size());
                        } else {
                            replace_min(sq_ready, sq_release);
                        }
                    }
                    if (config_.response_block_summary) {
                        sparse_retire_cycles[rob_next_slot] =
                            actual_retire;
                    } else {
                        auto& entry = sparse_rob[rob_next_slot];
                        entry.sequence = bound.sequence;
                        entry.completion_cycle = actual_completion;
                        entry.completion_extension_cycles =
                            completion_extension;
                        entry.retire_cycle = actual_retire;
                        entry.completion_extended =
                            completion_extension != 0;
                    }
                    ++rob_next_slot;
                    if (rob_next_slot == sparse_rob.size()) {
                        rob_next_slot = 0;
                    }
                    sparse_last_retire_cycle = actual_retire;
                    if (attribute_cycles) {
                        sparse_last_retire_cause =
                            actual_retire_cause;
                    }
                    sparse_counters.seeds += response_seed;
                    if (config_.response_rename_feedback &&
                        std::any_of(
                            bound.destination_class_counts.begin(),
                            bound.destination_class_counts.end(),
                            [](std::uint8_t count) {
                                return count != 0;
                            })) {
                        if (!rename_releases.empty() &&
                            rename_releases.back().cycle >
                                actual_retire) {
                            throw std::logic_error(
                                "response rename releases are not "
                                "ordered by retirement");
                        }
                        rename_releases.push_back(
                            ResponseRenameRelease{
                                actual_retire,
                                bound.destination_class_counts});
                    }
                    if (completion_extension != 0 ||
                        actual_retire > base_retire ||
                        dispatch_extra != 0) {
                        ++sparse_counters.materialized_uops;
                    }
                } else if (config_.response_rob_lsq_feedback) {
                    const auto base_retire = actual_retire;
                    const auto full_retire = std::max(
                        base_retire,
                        saturating_add(actual_completion,
                                       config_.execute_to_commit));
                    const auto response_extension =
                        full_retire - base_retire;
                    auto exposed_extension = response_extension;
                    if (config_.response_retire_exposure != 1.0) {
                        exposed_extension =
                            static_cast<std::uint64_t>(std::ceil(
                                static_cast<double>(response_extension) *
                                config_.response_retire_exposure));
                    }
                    actual_retire = saturating_add(
                        base_retire, exposed_extension);
                    if (actual_retire > commit_cycle) {
                        commit_cycle = actual_retire;
                        commit_used = 0;
                    }
                    if (commit_used == config_.commit_width) {
                        ++commit_cycle;
                        commit_used = 0;
                    }
                    actual_retire = commit_cycle;
                    ++commit_used;
                    rob_ready[rob_next_slot] = actual_retire;
                    rob_next_slot = static_cast<std::uint32_t>(
                        (rob_next_slot + 1) % rob_ready.size());
                    if (has_load) {
                        lq_ready[lq_next_slot] = actual_retire;
                        lq_next_slot = static_cast<std::uint32_t>(
                            (lq_next_slot + 1) % lq_ready.size());
                    }
                    if (has_store) {
                        auto sq_release = std::max(
                            actual_retire, memory_response_cycle);
                        if (regular_store) {
                            auto store_send = actual_retire;
                            if (config_.needs_tso) {
                                store_send = std::max(
                                    store_send, store_drain_ready);
                                o3_counters.tso_store_stall_cycles +=
                                    store_send - actual_retire;
                            }
                            sq_release = saturating_add(
                                store_send, store_latency_cycles);
                            if (config_.needs_tso) {
                                store_drain_ready = sq_release;
                            }
                            const auto proposed_store_issue =
                                saturating_add(
                                    store_base_issue_cycle,
                                    interval_gap_cycles);
                            if (store_send > proposed_store_issue) {
                                uop_issue_extra = std::max(
                                    uop_issue_extra,
                                    store_send - proposed_store_issue);
                            }
                        }
                        if (config_.needs_tso) {
                            sq_ready[sq_next_slot] = sq_release;
                            sq_next_slot = static_cast<std::uint32_t>(
                                (sq_next_slot + 1) % sq_ready.size());
                        } else {
                            replace_min(sq_ready, sq_release);
                        }
                    }
                }
                if (config_.interval_rob_head_suffix_replay &&
                    sparse_scoreboard && head_suffix_open) {
                    timing.rob_head_suffix_uops[core][uop - begin] = 1;
                    ++sparse_counters.head_suffix_uops;
                    // Recovery is the first younger head that can retire at
                    // its lower-bound time without inheriting the blocked
                    // head's ordered-commit debt. Include that head in the
                    // local replay and close the episode immediately after.
                    const auto base_retire = saturating_add(
                        bound.retire_q16 / kCycleUnit,
                        interval_gap_cycles);
                    if (!head_suffix_crossing &&
                        actual_retire <= base_retire) {
                        head_suffix_open = false;
                        ++sparse_counters.head_suffix_recoveries;
                    }
                }
                previous_actual_retire = actual_retire;
                if (bound.serialize_after) {
                    const auto base_retire = saturating_add(
                        bound.retire_q16 / kCycleUnit,
                        interval_gap_cycles);
                    serial_timing_extra =
                        actual_retire > base_retire
                            ? actual_retire - base_retire
                            : 0;
                }
                timing.issue_extra_q16[core][uop - begin] =
                    cycles_to_fixed(uop_issue_extra,
                                    "interval issue feedback");
                completion_extra_cycles[uop - begin] = completion_extra;
                if (sparse_scoreboard) continue;
                const auto retirement_tail =
                    static_cast<std::uint64_t>(end - 1 - uop) /
                    config_.commit_width;
                auto candidate_end = saturating_add(
                    chunk.uops[uop].completion_q16,
                    cycles_to_fixed(
                        completion_extra,
                        "interval completion feedback"));
                candidate_end = saturating_add(
                    candidate_end,
                    cycles_to_fixed(retirement_tail));
                if (config_.response_rob_lsq_feedback) {
                    const auto local_retire =
                        actual_retire > interval_gap_cycles
                            ? actual_retire - interval_gap_cycles
                            : 0;
                    candidate_end = std::max(
                        candidate_end,
                        cycles_to_fixed(
                            local_retire,
                            "response ordered retirement"));
                }
                adjusted_end_q16 = std::max(
                    adjusted_end_q16, candidate_end);
            }
            if (sparse_scoreboard &&
                config_.response_block_summary) {
                const auto accepted = end - begin;
                const auto retained_begin =
                    accepted > sparse_rob.size()
                        ? accepted - sparse_rob.size()
                        : 0;
                auto slot = sparse_rob_entry_slot;
                const auto skipped =
                    retained_begin % sparse_rob.size();
                slot = static_cast<std::uint32_t>(
                    (slot + skipped) % sparse_rob.size());
                for (auto local = retained_begin;
                     local < accepted; ++local) {
                    const auto& retained = chunk.uops[begin + local];
                    auto& entry = sparse_rob[slot];
                    entry.sequence = retained.sequence;
                    entry.completion_cycle = saturating_add(
                        retained.completion_q16 / kCycleUnit,
                        saturating_add(
                            interval_gap_cycles,
                            completion_extra_cycles[local]));
                    entry.completion_extension_cycles =
                        completion_extra_cycles[local];
                    entry.retire_cycle = sparse_retire_cycles[slot];
                    entry.completion_extended =
                        completion_extra_cycles[local] != 0;
                    ++slot;
                    if (slot == sparse_rob.size()) slot = 0;
                }
                if (slot != rob_next_slot) {
                    throw std::logic_error(
                        "lazy sparse ROB exit cursor mismatch");
                }
                const auto writes = accepted - retained_begin;
                sparse_counters.block_summary_rob_writes += writes;
                sparse_counters.block_summary_rob_writes_avoided +=
                    accepted - writes;
            }
            const auto base_end_q16 = chunk.uops[end - 1].retire_q16;
            if (sparse_scoreboard) {
                const auto base_end_cycle = saturating_add(
                    base_end_q16 / kCycleUnit,
                    interval_gap_cycles);
                const auto extra_cycles =
                    sparse_last_retire_cycle > base_end_cycle
                        ? sparse_last_retire_cycle - base_end_cycle
                        : 0;
                timing.interval_extra_q16[core] = cycles_to_fixed(
                    extra_cycles, "sparse scoreboard interval feedback");
                if (attribute_cycles && extra_cycles != 0) {
                    timing.interval_critical_cause[core] =
                        sparse_last_retire_cause;
                }
            } else {
                timing.interval_extra_q16[core] =
                    adjusted_end_q16 - base_end_q16;
            }
            // Keep hot scalar state private to the worker for the whole
            // interval.  Writing adjacent per-core slots on every UOP caused
            // cache-line ping-pong between domain workers at C16/C32 even
            // though the core computations are logically independent.
            timing.sequencer_counters[core] = sequencer_counters;
            timing.o3_counters[core] = o3_counters;
            timing.rename_releases[core] =
                std::move(rename_releases);
            timing.rename_live[core] = rename_live;
            timing.rename_cycle[core] = rename_cycle;
            timing.rename_used[core] = rename_used;
            timing.rename_cause[core] = rename_cause;
            timing.rename_counters[core] = rename_counters;
            timing.dispatch_cycle[core] = dispatch_cycle;
            timing.dispatch_used[core] = dispatch_used;
            timing.dispatch_cause[core] = dispatch_cause;
            timing.rob_next_slot[core] = rob_next_slot;
            timing.lq_next_slot[core] = lq_next_slot;
            timing.sq_next_slot[core] = sq_next_slot;
            timing.commit_cycle[core] = commit_cycle;
            timing.commit_used[core] = commit_used;
            timing.commit_cause[core] = commit_cause;
            timing.store_drain_ready_cycle[core] = store_drain_ready;
            timing.sparse_counters[core] = sparse_counters;
            timing.rob_head_suffix_open[core] = head_suffix_open;
            if (config_.interval_rob_head_suffix_replay &&
                head_suffix_open) {
                ++timing.sparse_counters[core]
                      .head_suffix_open_checkpoints;
            }
            if (!response_residuals.dependency_conserved() ||
                !response_residuals.retire_conserved()) {
                throw std::logic_error(
                    "response residual stage accounting is not conserved");
            }
            timing.response_residuals[core] = response_residuals;
    }

    TimingFeedback compute_timing_feedback(
        const std::vector<std::vector<MemoryReplay>>& event_feedback,
        const std::vector<std::size_t>& accepted_begin,
        const std::vector<std::size_t>& accepted_end) {
        TimingFeedback timing;
        timing.issue_extra_q16.resize(config_.cores);
        timing.interval_extra_q16.resize(config_.cores, 0);
        timing.interval_critical_cause.resize(
            config_.cores, CriticalCause::kUnattributed);
        timing.sequencer_counters.resize(config_.cores);
        timing.o3_counters.resize(config_.cores);
        timing.rename_live = response_rename_live_;
        timing.rename_cycle = response_rename_cycle_;
        timing.rename_used = response_rename_used_;
        timing.rename_cause = response_rename_cause_;
        timing.rename_counters = response_rename_counters_;
        timing.dispatch_cycle = response_dispatch_cycle_;
        timing.dispatch_used = response_dispatch_used_;
        timing.dispatch_cause = response_dispatch_cause_;
        timing.rob_next_slot = response_rob_next_slot_;
        timing.lq_next_slot = response_lq_next_slot_;
        timing.sq_next_slot = response_sq_next_slot_;
        timing.commit_cycle = response_commit_cycle_;
        timing.commit_used = response_commit_used_;
        timing.commit_cause = response_commit_cause_;
        timing.store_drain_ready_cycle =
            response_store_drain_ready_cycle_;
        timing.sparse_counters.resize(config_.cores);
        timing.response_residuals.resize(config_.cores);
        timing.rob_head_suffix_uops.resize(config_.cores);
        timing.rob_head_suffix_open = response_rob_head_suffix_open_;

        timing.sequencer_ready_cycles = sequencer_ready_cycles_;
        timing.iq_ready_cycles = response_iq_ready_cycles_;
        timing.rename_releases = response_rename_releases_;
        timing.rob_ready_cycles = response_rob_ready_cycles_;
        timing.lq_ready_cycles = response_lq_ready_cycles_;
        timing.sq_ready_cycles = response_sq_ready_cycles_;
        timing.sparse_rob_entries = response_sparse_rob_entries_;
        std::uint64_t active_tasks = 0;
        for (std::uint32_t core = 0; core < config_.cores; ++core) {
            active_tasks += accepted_begin[core] != accepted_end[core];
        }
        const auto started = std::chrono::steady_clock::now();
        // Waking the complete domain pool for zero or one non-empty core is
        // pure host overhead.  This is common near an imbalanced FS
        // all-core ROI tail (C4 lbm averages fewer than one active feedback
        // task per epoch).  Execute that exact same per-core transition on
        // the coordinator and reserve the worker barrier for actual
        // parallel work.
        if (config_.interval_parallel_feedback && active_tasks > 1) {
            run_parallel_timing_feedback(
                event_feedback, accepted_begin, accepted_end, timing);
            ++stats_.timing_feedback_parallel_calls;
        } else {
            for (std::uint32_t core = 0; core < config_.cores; ++core) {
                compute_core_timing_feedback(
                    core, event_feedback, accepted_begin, accepted_end,
                    timing);
            }
        }
        const auto ended = std::chrono::steady_clock::now();
        ++stats_.timing_feedback_calls;
        stats_.timing_feedback_core_tasks += active_tasks;
        stats_.timing_feedback_wall_ns += static_cast<std::uint64_t>(
            std::chrono::duration_cast<std::chrono::nanoseconds>(
                ended - started).count());
        return timing;
    }

    void commit_timing_feedback(
        const TimingFeedback& timing,
        const std::vector<std::size_t>& accepted_begin,
        const std::vector<std::size_t>& accepted_end,
        const std::vector<std::uint64_t>& old_gap_q16,
        std::uint64_t horizon_q16, bool time_epoch) {
        for (std::uint32_t core = 0; core < config_.cores; ++core) {
            const auto begin = accepted_begin[core];
            const auto end = accepted_end[core];
            if (begin == end) continue;
            const auto& chunk = *current_chunks_[core];
            const auto extra_q16 = timing.interval_extra_q16[core];
            const auto adjusted_end_q16 = saturating_add(
                chunk.uops[end - 1].retire_q16, extra_q16);
            if (time_epoch &&
                saturating_add(adjusted_end_q16,
                               old_gap_q16[core]) > horizon_q16) {
                ++stats_.epoch_corrected_horizon_violations;
            }
            interval_gap_q16_[core] = saturating_add(
                interval_gap_q16_[core], extra_q16);
            const auto exposed_extra_cycles =
                fixed_to_cycle_ceil(extra_q16);
            cores_[core]->total.memory_penalty_cycles +=
                exposed_extra_cycles;
            if (config_.cpi_attribution) {
                record_response_critical_cycles(
                    stats_.response_critical_cycles[core],
                    timing.interval_critical_cause[core],
                    exposed_extra_cycles);
            }
            if (config_.response_queue_feedback) {
                response_iq_ready_cycles_[core] =
                    timing.iq_ready_cycles[core];
                response_dispatch_cycle_[core] =
                    timing.dispatch_cycle[core];
                response_dispatch_used_[core] =
                    timing.dispatch_used[core];
                response_dispatch_cause_[core] =
                    timing.dispatch_cause[core];
                stats_.o3[core] += timing.o3_counters[core];
            }
            if (config_.response_rename_feedback) {
                response_rename_releases_[core] =
                    timing.rename_releases[core];
                response_rename_live_[core] =
                    timing.rename_live[core];
                response_rename_cycle_[core] =
                    timing.rename_cycle[core];
                response_rename_used_[core] =
                    timing.rename_used[core];
                response_rename_cause_[core] =
                    timing.rename_cause[core];
                response_rename_counters_[core] =
                    timing.rename_counters[core];
                if (!response_rename_counters_[core].conserved()) {
                    throw std::logic_error(
                        "response rename free-list accounting is not "
                        "conserved");
                }
                stats_.response_rename[core] =
                    response_rename_counters_[core];
            }
            if (config_.response_rob_lsq_feedback) {
                response_rob_ready_cycles_[core] =
                    timing.rob_ready_cycles[core];
                response_lq_ready_cycles_[core] =
                    timing.lq_ready_cycles[core];
                response_sq_ready_cycles_[core] =
                    timing.sq_ready_cycles[core];
                response_rob_next_slot_[core] =
                    timing.rob_next_slot[core];
                response_lq_next_slot_[core] =
                    timing.lq_next_slot[core];
                response_sq_next_slot_[core] =
                    timing.sq_next_slot[core];
                response_commit_cycle_[core] =
                    timing.commit_cycle[core];
                response_commit_used_[core] =
                    timing.commit_used[core];
                response_commit_cause_[core] =
                    timing.commit_cause[core];
                response_store_drain_ready_cycle_[core] =
                    timing.store_drain_ready_cycle[core];
            }
            if (config_.response_sparse_scoreboard) {
                response_rob_ready_cycles_[core] =
                    timing.rob_ready_cycles[core];
                response_lq_ready_cycles_[core] =
                    timing.lq_ready_cycles[core];
                response_sq_ready_cycles_[core] =
                    timing.sq_ready_cycles[core];
                response_rob_next_slot_[core] =
                    timing.rob_next_slot[core];
                response_lq_next_slot_[core] =
                    timing.lq_next_slot[core];
                response_sq_next_slot_[core] =
                    timing.sq_next_slot[core];
                response_commit_cycle_[core] =
                    timing.commit_cycle[core];
                response_commit_used_[core] =
                    timing.commit_used[core];
                response_commit_cause_[core] =
                    timing.commit_cause[core];
                response_store_drain_ready_cycle_[core] =
                    timing.store_drain_ready_cycle[core];
                response_sparse_rob_entries_[core] =
                    timing.sparse_rob_entries[core];
                const auto& sparse = timing.sparse_counters[core];
                stats_.sparse_scoreboard_seeds += sparse.seeds;
                stats_.sparse_scoreboard_materialized_uops +=
                    sparse.materialized_uops;
                stats_.sparse_scoreboard_absorbed_edges +=
                    sparse.absorbed_edges;
                stats_.sparse_scoreboard_cross_epoch_edges +=
                    sparse.cross_epoch_edges;
                stats_.sparse_scoreboard_rob_crossings +=
                    sparse.rob_crossings;
                stats_.sparse_scoreboard_lq_crossings +=
                    sparse.lq_crossings;
                stats_.sparse_scoreboard_sq_crossings +=
                    sparse.sq_crossings;
                stats_.response_block_summary_checkpoints +=
                    sparse.block_summary_checkpoints;
                stats_.response_block_summary_uops +=
                    sparse.block_summary_uops;
                stats_.response_block_summary_rob_writes +=
                    sparse.block_summary_rob_writes;
                stats_.response_block_summary_rob_writes_avoided +=
                    sparse.block_summary_rob_writes_avoided;
                stats_.sparse_resource_candidates +=
                    sparse.resource_candidates;
                stats_.sparse_resource_issue_moves +=
                    sparse.resource_issue_moves;
                stats_.sparse_resource_issue_collision_cycles +=
                    sparse.resource_issue_collision_cycles;
                stats_.sparse_resource_writeback_moves +=
                    sparse.resource_writeback_moves;
                stats_.sparse_resource_writeback_collision_cycles +=
                    sparse.resource_writeback_collision_cycles;
                stats_.rob_head_suffix_anchors +=
                    sparse.head_suffix_anchors;
                stats_.rob_head_suffix_recoveries +=
                    sparse.head_suffix_recoveries;
                stats_.rob_head_suffix_uops +=
                    sparse.head_suffix_uops;
                stats_.rob_head_suffix_open_checkpoints +=
                    sparse.head_suffix_open_checkpoints;
                stats_.response_activity_candidates +=
                    sparse.activity_candidates;
                stats_.response_activity_certified_segments +=
                    sparse.activity_certified_segments;
                stats_.response_activity_certified_uops +=
                    sparse.activity_certified_uops;
                stats_.response_activity_fallback_segments +=
                    sparse.activity_fallback_segments;
                stats_.response_residuals[core] +=
                    timing.response_residuals[core];
                if (config_.interval_rob_head_suffix_replay) {
                    response_rob_head_suffix_open_[core] =
                        timing.rob_head_suffix_open[core];
                }
            }
            if (config_.ruby_sequencer_max_outstanding != 0) {
                sequencer_ready_cycles_[core] =
                    timing.sequencer_ready_cycles[core];
                stats_.sequencer[core] +=
                    timing.sequencer_counters[core];
            }
        }
    }

    std::vector<BatchPending> corrected_shared_order(
        const std::vector<BatchPending>& materialized,
        const TimingFeedback& timing,
        const std::vector<std::size_t>& accepted_begin,
        std::uint64_t local_horizon_q16 =
            std::numeric_limits<std::uint64_t>::max()) const {
        auto corrected = materialized;
        std::vector<std::uint64_t> last_core_issue(config_.cores, 0);
        for (auto& pending : corrected) {
            const auto& event = current_chunks_[pending.core]
                                    ->memory[pending.index];
            const auto position = static_cast<std::size_t>(
                event.uop_index) - accepted_begin[pending.core];
            const bool in_local_suffix =
                !config_.interval_rob_head_suffix_replay ||
                timing.rob_head_suffix_uops[pending.core][position] != 0;
            pending.corrected_issue_q16 = saturating_add(
                pending.issue_q16,
                in_local_suffix
                    ? timing.issue_extra_q16[pending.core][position]
                    : 0);
            if (config_.interval_rob_head_suffix_replay &&
                pending.corrected_issue_q16 > local_horizon_q16) {
                // Do not commit future shared-ready state across fixed Q.
                // The open ROB-head episode is checkpointed, while this
                // boundary event retains canonical timing for this pass.
                pending.corrected_issue_q16 = pending.issue_q16;
            }
            pending.corrected_issue_q16 = std::max(
                pending.corrected_issue_q16,
                last_core_issue[pending.core]);
            last_core_issue[pending.core] = pending.corrected_issue_q16;
        }
        std::sort(
            corrected.begin(), corrected.end(),
            [](const BatchPending& left, const BatchPending& right) {
                if (left.corrected_issue_q16 !=
                    right.corrected_issue_q16) {
                    return left.corrected_issue_q16 <
                           right.corrected_issue_q16;
                }
                if (left.core != right.core) return left.core < right.core;
                return left.index < right.index;
            });
        return corrected;
    }

    std::uint64_t count_rob_head_suffix_boundary_clips(
        const std::vector<BatchPending>& materialized,
        const TimingFeedback& timing,
        const std::vector<std::size_t>& accepted_begin,
        std::uint64_t horizon_q16) const {
        if (!config_.interval_rob_head_suffix_replay) return 0;
        std::uint64_t clipped = 0;
        for (const auto& pending : materialized) {
            const auto& event = current_chunks_[pending.core]
                                    ->memory[pending.index];
            const auto position = static_cast<std::size_t>(
                event.uop_index) - accepted_begin[pending.core];
            if (timing.rob_head_suffix_uops[pending.core][position] == 0) {
                continue;
            }
            clipped += saturating_add(
                pending.issue_q16,
                timing.issue_extra_q16[pending.core][position]) >
                horizon_q16;
        }
        return clipped;
    }

    static std::uint64_t batch_event_key(
        const BatchPending& pending) {
        return (static_cast<std::uint64_t>(pending.core) << 32) |
               pending.index;
    }

    PathConflictPlan plan_cross_core_line_conflicts(
        const std::vector<BatchPending>& events) const {
        struct LineSummary {
            std::uint32_t first_core = 0;
            std::uint64_t events = 0;
            bool cross_core = false;
        };
        std::unordered_map<std::uint64_t, LineSummary> summaries;
        summaries.reserve(events.size());
        for (const auto& pending : events) {
            const auto line = current_chunks_[pending.core]
                                  ->memory[pending.index].line;
            const auto inserted = summaries.emplace(
                line, LineSummary{pending.core, 1, false});
            if (inserted.second) continue;
            auto& summary = inserted.first->second;
            ++summary.events;
            summary.cross_core =
                summary.cross_core || summary.first_core != pending.core;
        }

        PathConflictPlan plan;
        for (const auto& entry : summaries) {
            if (!entry.second.cross_core) continue;
            plan.cross_core_lines.insert(entry.first);
            plan.component_events += entry.second.events;
            plan.max_component_events = std::max(
                plan.max_component_events, entry.second.events);
        }
        return plan;
    }

    bool same_conflict_component_order(
        const std::vector<BatchPending>& left,
        const std::vector<BatchPending>& right,
        const PathConflictPlan& plan) const {
        if (left.size() != right.size()) return false;
        std::unordered_map<std::uint64_t, std::size_t> right_rank;
        right_rank.reserve(right.size());
        for (std::size_t rank = 0; rank < right.size(); ++rank) {
            right_rank.emplace(batch_event_key(right[rank]), rank);
        }
        std::unordered_map<std::uint64_t, std::size_t> last_rank;
        last_rank.reserve(plan.cross_core_lines.size());
        for (const auto& pending : left) {
            const auto line = current_chunks_[pending.core]
                                  ->memory[pending.index].line;
            if (plan.cross_core_lines.find(line) ==
                plan.cross_core_lines.end()) {
                continue;
            }
            const auto rank = right_rank.at(batch_event_key(pending));
            const auto previous = last_rank.find(line);
            if (previous != last_rank.end() && rank < previous->second) {
                return false;
            }
            last_rank[line] = rank;
        }
        return true;
    }

    bool same_conflict_component_arrivals(
        const std::vector<BatchPending>& left,
        const std::vector<BatchPending>& right,
        const PathConflictPlan& plan) const {
        if (left.size() != right.size()) return false;
        std::unordered_map<std::uint64_t, std::uint64_t> right_arrival;
        right_arrival.reserve(right.size());
        for (const auto& pending : right) {
            right_arrival.emplace(
                batch_event_key(pending), pending.corrected_issue_q16);
        }
        for (const auto& pending : left) {
            const auto line = current_chunks_[pending.core]
                                  ->memory[pending.index].line;
            if (plan.cross_core_lines.find(line) ==
                plan.cross_core_lines.end()) {
                continue;
            }
            if (right_arrival.at(batch_event_key(pending)) !=
                pending.corrected_issue_q16) {
                return false;
            }
        }
        return true;
    }

    std::uint32_t llc_set_of(std::uint64_t line) const {
        const auto sets = config_.llc.size_bytes /
            (static_cast<std::uint64_t>(config_.llc.associativity) *
             config_.llc.line_size);
        return static_cast<std::uint32_t>(line & (sets - 1));
    }

    std::array<std::uint32_t, 2> shared_path_sets(
        const BatchPending& pending,
        const std::vector<std::vector<MemoryReplay>>& feedback,
        std::size_t& count) const {
        std::array<std::uint32_t, 2> sets{};
        count = 0;
        const auto& replay = feedback[pending.core][pending.index];
        const auto& event = current_chunks_[pending.core]
                                ->memory[pending.index];
        if (replay.shared_timing.uncore_request) {
            sets[count++] = llc_set_of(event.line);
        }
        if (replay.l2_evicted) {
            const auto victim_set = llc_set_of(replay.l2_evicted_line);
            if (count == 0 || sets[0] != victim_set) {
                sets[count++] = victim_set;
            }
        }
        return sets;
    }

    CausalClosurePlan plan_causal_timing_order(
        const std::vector<BatchPending>& canonical,
        const std::vector<BatchPending>& corrected,
        const std::vector<std::vector<MemoryReplay>>& feedback) const {
        CausalClosurePlan plan;
        if (canonical.empty()) return plan;

        std::unordered_map<std::uint64_t, BatchPending> corrected_event;
        std::unordered_map<std::uint64_t, std::size_t> corrected_rank;
        corrected_event.reserve(corrected.size());
        corrected_rank.reserve(corrected.size());
        for (std::size_t rank = 0; rank < corrected.size(); ++rank) {
            const auto key = batch_event_key(corrected[rank]);
            corrected_event.emplace(key, corrected[rank]);
            corrected_rank.emplace(key, rank);
        }

        auto nodes = canonical;
        std::unordered_map<std::uint32_t, std::vector<std::size_t>>
            set_nodes;
        set_nodes.reserve(canonical.size());
        std::vector<std::vector<std::size_t>> core_nodes(config_.cores);
        for (std::size_t rank = 0; rank < canonical.size(); ++rank) {
            const auto key = batch_event_key(canonical[rank]);
            nodes[rank].corrected_issue_q16 =
                corrected_event.at(key).corrected_issue_q16;
            const auto& event = current_chunks_[canonical[rank].core]
                                    ->memory[canonical[rank].index];
            if (event.atomic) plan.replayable = false;
            const auto& descriptor =
                feedback[canonical[rank].core][canonical[rank].index]
                    .shared_timing;
            plan.replayable = plan.replayable && descriptor.replayable;
            core_nodes[canonical[rank].core].push_back(rank);
            std::size_t set_count = 0;
            const auto sets = shared_path_sets(
                canonical[rank], feedback, set_count);
            for (std::size_t index = 0; index < set_count; ++index) {
                set_nodes[sets[index]].push_back(rank);
            }
        }

        std::unordered_set<std::uint32_t> closure_sets;
        closure_sets.reserve(set_nodes.size());
        std::vector<std::uint8_t> closure_node(canonical.size(), 0);
        for (const auto& entry : set_nodes) {
            std::size_t last = 0;
            bool first = true;
            bool inverted = false;
            for (const auto node : entry.second) {
                const auto rank = corrected_rank.at(
                    batch_event_key(canonical[node]));
                if (!first && rank < last) inverted = true;
                first = false;
                last = rank;
            }
            if (!inverted) continue;
            closure_sets.insert(entry.first);
            for (const auto node : entry.second) closure_node[node] = 1;
        }
        plan.closure_components = closure_sets.size();
        plan.closure_events = static_cast<std::uint64_t>(std::count(
            closure_node.begin(), closure_node.end(),
            static_cast<std::uint8_t>(1)));
        // A timestamp or queue-predecessor shift without a path-component
        // inversion is already represented by response feedback. Feeding it
        // back into the same epoch double-counts stalls. B1b is entered only
        // at a genuine cache/directory causal boundary.
        plan.changed = plan.closure_events != 0;

        // Canonical chains inside every shared-state set are the path
        // certificate. Corrected arrival remains the priority among ready
        // nodes, so only queue timing—not cache/directory state—is re-woven.
        std::vector<std::vector<std::size_t>> successors(canonical.size());
        std::vector<std::uint32_t> indegree(canonical.size(), 0);
        const auto add_chains = [&](const auto& chains) {
            for (const auto& entry : chains) {
                const auto& chain = entry.second;
                for (std::size_t index = 1; index < chain.size(); ++index) {
                    successors[chain[index - 1]].push_back(chain[index]);
                    ++indegree[chain[index]];
                }
            }
        };
        add_chains(set_nodes);
        for (const auto& chain : core_nodes) {
            for (std::size_t index = 1; index < chain.size(); ++index) {
                successors[chain[index - 1]].push_back(chain[index]);
                ++indegree[chain[index]];
            }
        }

        const auto later = [&](std::size_t left, std::size_t right) {
            if (nodes[left].corrected_issue_q16 !=
                nodes[right].corrected_issue_q16) {
                return nodes[left].corrected_issue_q16 >
                       nodes[right].corrected_issue_q16;
            }
            if (nodes[left].core != nodes[right].core) {
                return nodes[left].core > nodes[right].core;
            }
            if (nodes[left].index != nodes[right].index) {
                return nodes[left].index > nodes[right].index;
            }
            return left > right;
        };
        std::priority_queue<std::size_t, std::vector<std::size_t>,
                            decltype(later)> ready(later);
        for (std::size_t node = 0; node < nodes.size(); ++node) {
            if (indegree[node] == 0) ready.push(node);
        }
        plan.timing_order.reserve(nodes.size());
        while (!ready.empty()) {
            const auto node = ready.top();
            ready.pop();
            plan.timing_order.push_back(nodes[node]);
            for (const auto successor : successors[node]) {
                if (--indegree[successor] == 0) ready.push(successor);
            }
        }
        if (plan.timing_order.size() != canonical.size()) {
            throw std::logic_error("causal timing graph contains a cycle");
        }
        return plan;
    }

    bool apply_frfcfs_dram_repair(
        const std::vector<BatchPending>& materialized,
        std::vector<std::vector<MemoryReplay>>& event_feedback,
        const std::vector<std::size_t>& accepted_begin,
        const std::vector<std::size_t>& accepted_end,
        const SharedSystem::TimingState& timing_start,
        std::uint64_t horizon_q16,
        TimingFeedback& timing,
        bool response_retime_pass = false) {
        struct RepairEvent {
            BatchPending pending;
            SharedTimingDescriptor descriptor;
            std::uint64_t issue_cycle = 0;
            std::uint64_t fair_ordinal = 0;
        };

        struct SharedArrival {
            BatchPending pending;
            SharedTimingDescriptor descriptor;
            std::uint64_t issue_cycle = 0;
            std::uint64_t fair_ordinal = 0;
        };

        std::vector<std::uint64_t> response_retime_entry_queue;
        if (response_timing_retime_enabled() &&
            !response_retime_pass) {
            response_retime_entry_queue.reserve(config_.cha_count);
            for (const auto& cha : stats_.cha) {
                response_retime_entry_queue.push_back(cha.queue_cycles);
            }
        }

        std::uint64_t memory_requests = 0;
        std::uint64_t canonical_queue_cycles = 0;
        for (const auto& pending : materialized) {
            const auto& descriptor =
                event_feedback[pending.core][pending.index]
                    .shared_timing;
            if (descriptor.path != SharedTimingPath::kMemory) continue;
            ++memory_requests;
            canonical_queue_cycles = saturating_add(
                canonical_queue_cycles,
                descriptor.canonical_memory_queue_cycles);
        }
        if (memory_requests == 0) return false;

        const auto record_pressure_bypass = [&] {
            ++stats_.dram_frfcfs_bypass_epochs;
            stats_.dram_frfcfs_bypass_requests += memory_requests;
            stats_.dram_frfcfs_bypass_queue_cycles +=
                canonical_queue_cycles;
        };
        const auto configured_window =
            config_.dram.frfcfs_selection_window == 0
                ? config_.dram.read_buffer_size
                : config_.dram.frfcfs_selection_window;
        auto repair_selection_window = configured_window;
        if (config_.dram.frfcfs_topology_scaled_window) {
            const auto producer_lanes =
                (static_cast<std::uint64_t>(config_.cores) *
                 config_.dram.ranks_per_channel) /
                config_.dram.channels;
            const auto inferred_window =
                producer_lanes > 1 ? producer_lanes - 1 : 1;
            repair_selection_window = static_cast<std::uint32_t>(
                std::min<std::uint64_t>(
                    configured_window, inferred_window));
            // A one-entry window cannot reorder controller work. Preserve
            // canonical FCFS timing and avoid paying for a no-op repair.
            if (repair_selection_window <= 1) {
                stats_.dram_frfcfs_effective_selection_window = 1;
                record_pressure_bypass();
                return false;
            }
        }
        stats_.dram_frfcfs_effective_selection_window =
            repair_selection_window;
        stats_.dram_frfcfs_candidate_queue_cycles +=
            canonical_queue_cycles;
        stats_.dram_frfcfs_selection_window_sum +=
            repair_selection_window;
        stats_.dram_frfcfs_selection_window_max = std::max(
            stats_.dram_frfcfs_selection_window_max,
            static_cast<std::uint64_t>(repair_selection_window));

        std::vector<SharedArrival> shared_arrivals;
        shared_arrivals.reserve(materialized.size());
        std::unordered_map<std::uint64_t, std::uint64_t>
            bucket_core_rank;
        bucket_core_rank.reserve(materialized.size());
        std::unordered_map<std::uint64_t, std::uint64_t>
            fair_ordinal_by_event;
        fair_ordinal_by_event.reserve(materialized.size());
        for (const auto& pending : materialized) {
            const auto& replay =
                event_feedback[pending.core][pending.index];
            if (!replay.shared_timing.uncore_request) continue;
            const auto issue_cycle =
                fixed_to_cycle_ceil(pending.corrected_issue_q16);
            const auto bucket_width =
                config_.dram.frfcfs_arrival_bucket_cycles;
            const auto arrival_class = bucket_width == 0
                ? issue_cycle
                : issue_cycle / bucket_width;
            const auto domain_key = arrival_class * config_.cores +
                pending.core;
            const auto local_rank = bucket_core_rank[domain_key]++;
            const auto fair_ordinal = local_rank * config_.cores +
                pending.core;
            fair_ordinal_by_event.emplace(
                batch_event_key(pending), fair_ordinal);
            shared_arrivals.push_back(SharedArrival{
                pending, replay.shared_timing,
                issue_cycle, fair_ordinal});
        }
        std::stable_sort(
            shared_arrivals.begin(), shared_arrivals.end(),
            [this](const SharedArrival& left,
                   const SharedArrival& right) {
                const auto bucket =
                    config_.dram.frfcfs_arrival_bucket_cycles;
                const auto left_class = bucket == 0
                    ? left.issue_cycle
                    : left.issue_cycle / bucket;
                const auto right_class = bucket == 0
                    ? right.issue_cycle
                    : right.issue_cycle / bucket;
                if (left_class != right_class) {
                    return left_class < right_class;
                }
                return left.fair_ordinal < right.fair_ordinal;
            });
        auto fair_cha_ready = timing_start.cha_ready;
        std::unordered_map<std::uint64_t, std::uint64_t> fair_tag_ready;
        std::unordered_map<std::uint64_t, std::uint64_t> fair_cha_queue;
        fair_tag_ready.reserve(shared_arrivals.size());
        fair_cha_queue.reserve(shared_arrivals.size());
        for (const auto& shared : shared_arrivals) {
            const auto cha = shared.descriptor.cha;
            const auto arrival = shared.issue_cycle +
                config_.noc_one_way_latency;
            const auto cha_start = std::max(
                arrival, fair_cha_ready[cha]);
            fair_cha_ready[cha] = cha_start +
                config_.llc_service_cycles;
            fair_cha_queue.emplace(
                batch_event_key(shared.pending), cha_start - arrival);
            fair_tag_ready.emplace(
                batch_event_key(shared.pending),
                cha_start + config_.llc.hit_latency);
        }

        std::vector<RepairEvent> events;
        events.reserve(materialized.size());
        bool replayable = true;
        for (const auto& pending : materialized) {
            const auto& replay =
                event_feedback[pending.core][pending.index];
            replayable = replayable && replay.shared_timing.replayable;
            if (replay.shared_timing.path != SharedTimingPath::kMemory) {
                continue;
            }
            const auto& event = current_chunks_[pending.core]
                                    ->memory[pending.index];
            replayable = replayable && !event.atomic;
            auto descriptor = replay.shared_timing;
            descriptor.canonical_tag_ready =
                fair_tag_ready.at(batch_event_key(pending)) +
                config_.directory_memory_latency;
            events.push_back(RepairEvent{
                pending, descriptor,
                fixed_to_cycle_ceil(pending.corrected_issue_q16),
                fair_ordinal_by_event.at(batch_event_key(pending))});
        }
        if (events.empty()) return false;
        // Equal lower-bound arrival times are a target concurrency class, not
        // permission for host core-major iteration to inject thousands of
        // same-core requests first. Round-robin ordinals preserve each core's
        // order while treating cores symmetrically inside that class.
        std::stable_sort(
            events.begin(), events.end(),
            [](const RepairEvent& left, const RepairEvent& right) {
                if (left.descriptor.canonical_tag_ready !=
                    right.descriptor.canonical_tag_ready) {
                    return left.descriptor.canonical_tag_ready <
                           right.descriptor.canonical_tag_ready;
                }
                return left.fair_ordinal < right.fair_ordinal;
            });

        const auto started = std::chrono::steady_clock::now();
        const auto record_wall_time = [&] {
            const auto ended = std::chrono::steady_clock::now();
            stats_.dram_frfcfs_wall_ns +=
                static_cast<std::uint64_t>(
                    std::chrono::duration_cast<std::chrono::nanoseconds>(
                        ended - started).count());
        };
        ++stats_.dram_frfcfs_candidate_epochs;
        stats_.dram_frfcfs_requests += events.size();
        if (!replayable) {
            ++stats_.dram_frfcfs_fallback_epochs;
            record_wall_time();
            return false;
        }

        std::vector<std::uint64_t> previous_arrivals;
        std::vector<std::uint64_t> previous_completions;
        previous_arrivals.reserve(events.size());
        previous_completions.reserve(events.size());
        for (const auto& event : events) {
            previous_arrivals.push_back(
                event.descriptor.canonical_dram_arrival);
            previous_completions.push_back(
                event.descriptor.canonical_fill_completion);
        }
        std::vector<std::size_t> previous_service_order;

        DramModel stable_dram = timing_start.dram;
        std::vector<std::uint64_t> stable_mshrs;
        DramModel::BatchResult stable_batch;
        std::vector<DramModel::Request> stable_requests;
        bool stable = false;
        std::function<void(
            std::uint32_t,
            const std::function<void(std::uint32_t)>&)>
            parallel_for_channels;
        // The runner is a host-only scheduling choice.  A modest amount of
        // work per worker amortizes the two pool barriers; target DRAM state
        // and channel-major service order are identical on either path.
        if (domain_worker_count_ > 1 &&
            events.size() >=
                static_cast<std::size_t>(domain_worker_count_) * 8) {
            parallel_for_channels =
                [this](
                    std::uint32_t count,
                    const std::function<void(std::uint32_t)>& task) {
                    run_parallel_channel_tasks(count, task);
                };
        }

        for (std::uint32_t pass = 0;
             pass < config_.dram.frfcfs_passes; ++pass) {
            auto mshr_ready = timing_start.llc_mshr_ready;
            std::vector<DramModel::Request> requests;
            requests.reserve(events.size());

            const auto replace_slice_min = [&] (
                std::size_t begin, std::size_t count,
                std::uint64_t value) {
                if (count == 0) {
                    throw std::logic_error(
                        "FR-FCFS repair has no LLC MSHR entries");
                }
                mshr_ready[begin] = value;
                std::size_t parent = 0;
                while (true) {
                    const auto left = parent * 2 + 1;
                    if (left >= count) break;
                    const auto right = left + 1;
                    const auto child =
                        right < count &&
                                mshr_ready[begin + right] <
                                    mshr_ready[begin + left]
                            ? right
                            : left;
                    if (mshr_ready[begin + parent] <=
                        mshr_ready[begin + child]) {
                        break;
                    }
                    std::swap(mshr_ready[begin + parent],
                              mshr_ready[begin + child]);
                    parent = child;
                }
            };

            std::vector<std::uint64_t> arrivals(events.size(), 0);
            for (std::size_t index = 0; index < events.size(); ++index) {
                const auto& event = events[index];
                const auto cha = event.descriptor.cha;
                const auto offset = static_cast<std::size_t>(cha) *
                    config_.llc_mshrs;
                const auto mshr_available = mshr_ready[offset];
                const auto arrival = std::max(
                    event.descriptor.canonical_tag_ready,
                    mshr_available);
                arrivals[index] = arrival;
                requests.push_back(DramModel::Request{
                    index, arrival, event.descriptor.line,
                    event.fair_ordinal});
                replace_slice_min(
                    offset, config_.llc_mshrs,
                    std::max(previous_completions[index], arrival));
            }

            auto repaired_dram = timing_start.dram;
            auto batch = repaired_dram.schedule_frfcfs(
                requests, repair_selection_window,
                parallel_for_channels);
            std::vector<std::uint64_t> completions(events.size(), 0);
            for (std::size_t index = 0; index < events.size(); ++index) {
                completions[index] =
                    batch.results[index].completion +
                    config_.llc_fill_response_latency;
            }
            ++stats_.dram_frfcfs_passes;

            const bool fixed_point = pass != 0 &&
                arrivals == previous_arrivals &&
                completions == previous_completions &&
                batch.service_order == previous_service_order;
            if (fixed_point) {
                stable = true;
                stable_dram = std::move(repaired_dram);
                stable_mshrs = std::move(mshr_ready);
                stable_batch = std::move(batch);
                stable_requests = std::move(requests);
                break;
            }
            previous_arrivals = std::move(arrivals);
            previous_completions = std::move(completions);
            previous_service_order = std::move(batch.service_order);
        }

        if (!stable) {
            ++stats_.dram_frfcfs_fallback_epochs;
            record_wall_time();
            return false;
        }

        std::vector<std::uint64_t> base_latency(events.size(), 0);
        std::vector<std::uint64_t> base_exposed(events.size(), 0);
        std::vector<std::uint64_t> absolute_response(events.size(), 0);
        std::vector<std::uint64_t> canonical_cha_start(events.size(), 0);
        const auto update_feedback = [&] (
            std::size_t index, std::uint64_t issue_delay) {
            const auto& repair = events[index];
            auto& replay = event_feedback[repair.pending.core]
                                         [repair.pending.index];
            const auto corrected_issue = saturating_add(
                repair.issue_cycle, issue_delay);
            replay.latency_cycles =
                absolute_response[index] > corrected_issue
                    ? absolute_response[index] - corrected_issue
                    : 0;
            const auto& memory_event =
                current_chunks_[repair.pending.core]
                    ->memory[repair.pending.index];
            const auto exposed_latency =
                replay.latency_cycles > memory_event.lower_bound_latency
                    ? replay.latency_cycles -
                          memory_event.lower_bound_latency
                    : 0;
            replay.exposed_cycles = static_cast<std::uint64_t>(
                std::ceil(static_cast<double>(exposed_latency) *
                          config_.memory_exposure));
        };
        for (std::size_t index = 0; index < events.size(); ++index) {
            const auto& repair = events[index];
            absolute_response[index] =
                stable_batch.results[index].completion +
                config_.llc_fill_response_latency +
                config_.noc_one_way_latency;
            const auto fixed_path_latency =
                static_cast<std::uint64_t>(config_.llc.hit_latency) +
                config_.directory_memory_latency;
            if (repair.descriptor.canonical_tag_ready <
                fixed_path_latency) {
                throw std::logic_error(
                    "FR-FCFS canonical CHA boundary underflow");
            }
            canonical_cha_start[index] =
                repair.descriptor.canonical_tag_ready -
                fixed_path_latency;
            update_feedback(index, 0);
            const auto& replay =
                event_feedback[repair.pending.core][repair.pending.index];
            base_latency[index] = replay.latency_cycles;
            base_exposed[index] = replay.exposed_cycles;
        }

        // Secondary demand misses are not DRAM requests of their own.  Rebase
        // them onto the repaired completion of the unique fill they merged
        // behind, identified by the canonical (line, completion) pair.  This
        // keeps the functional cache path fixed while making every waiter
        // observe the same repaired response.
        std::unordered_map<
            std::uint64_t,
            std::unordered_map<std::uint64_t, std::uint64_t>>
            repaired_fill_completion;
        repaired_fill_completion.reserve(events.size());
        std::vector<std::array<std::uint64_t, 3>> fill_remaps;
        fill_remaps.reserve(events.size());
        for (std::size_t index = 0; index < events.size(); ++index) {
            const auto& descriptor = events[index].descriptor;
            const auto repaired =
                stable_batch.results[index].completion +
                config_.llc_fill_response_latency;
            repaired_fill_completion[descriptor.line][
                descriptor.canonical_fill_completion] = repaired;
            fill_remaps.push_back({
                descriptor.line,
                descriptor.canonical_fill_completion,
                repaired});
        }
        for (const auto& pending : materialized) {
            auto& replay = event_feedback[pending.core][pending.index];
            const auto& descriptor = replay.shared_timing;
            if (descriptor.path != SharedTimingPath::kMergedMemory) {
                continue;
            }
            const auto line = repaired_fill_completion.find(
                descriptor.line);
            if (line == repaired_fill_completion.end()) continue;
            const auto parent = line->second.find(
                descriptor.canonical_fill_completion);
            if (parent == line->second.end()) continue;
            const auto issue_cycle = fixed_to_cycle_ceil(
                pending.corrected_issue_q16);
            const auto response = parent->second +
                config_.noc_one_way_latency;
            replay.latency_cycles = response > issue_cycle
                ? response - issue_cycle
                : 0;
            const auto& memory_event =
                current_chunks_[pending.core]->memory[pending.index];
            const auto exposed_latency =
                replay.latency_cycles > memory_event.lower_bound_latency
                    ? replay.latency_cycles -
                          memory_event.lower_bound_latency
                    : 0;
            replay.exposed_cycles = static_cast<std::uint64_t>(
                std::ceil(static_cast<double>(exposed_latency) *
                          config_.memory_exposure));
        }
        timing = compute_timing_feedback(
            event_feedback, accepted_begin, accepted_end);

        // Compose response feedback with the FR-FCFS timing certificate
        // without replaying cache/directory state. If a response-delayed
        // issue still reaches its already-selected CHA before the canonical
        // service start, the request remains resident in exactly the same
        // queue position and its downstream absolute response is unchanged.
        // Rebase latency on the corrected issue time in that certified slack
        // region; requests crossing the CHA boundary keep the conservative
        // relative-latency result for a later sparse component replay.
        if (config_.interval_causal_timing) {
            const auto causal_started =
                std::chrono::steady_clock::now();
            const auto record_causal_wall_time = [&] {
                const auto causal_ended =
                    std::chrono::steady_clock::now();
                stats_.causal_timing_wall_ns +=
                    static_cast<std::uint64_t>(
                        std::chrono::duration_cast<
                            std::chrono::nanoseconds>(
                                causal_ended - causal_started).count());
            };
            const auto base_timing = timing;
            bool candidate_counted = false;
            bool closure_deferred = false;
            bool closure_stable = false;
            for (std::uint32_t pass = 0;
                 pass < config_.interval_causal_passes; ++pass) {
                std::vector<std::uint64_t> issue_delay(
                    events.size(), 0);
                std::unordered_set<std::uint32_t> affected_chas;
                std::uint64_t anchored_events = 0;
                for (std::size_t index = 0;
                     index < events.size(); ++index) {
                    const auto& repair = events[index];
                    const auto position = static_cast<std::size_t>(
                        current_chunks_[repair.pending.core]
                            ->memory[repair.pending.index].uop_index) -
                        accepted_begin[repair.pending.core];
                    const auto delay = fixed_to_cycle_ceil(
                        timing.issue_extra_q16[repair.pending.core]
                                              [position]);
                    const auto corrected_cha_arrival = saturating_add(
                        saturating_add(repair.issue_cycle, delay),
                        config_.noc_one_way_latency);
                    if (delay != 0 && corrected_cha_arrival <=
                                          canonical_cha_start[index]) {
                        issue_delay[index] = delay;
                        ++anchored_events;
                        affected_chas.insert(repair.descriptor.cha);
                    }
                }
                if (anchored_events == 0) {
                    if (!candidate_counted) {
                        ++stats_.causal_timing_noop_epochs;
                        closure_stable = true;
                    }
                    break;
                }
                if (!candidate_counted) {
                    candidate_counted = true;
                    ++stats_.causal_timing_candidate_epochs;
                    stats_.causal_closure_components +=
                        affected_chas.size();
                    stats_.causal_closure_events += anchored_events;
                    stats_.causal_max_closure_events = std::max(
                        stats_.causal_max_closure_events,
                        anchored_events);
                    if (anchored_events >
                        config_.interval_causal_max_closure_events) {
                        ++stats_.causal_timing_deferred_epochs;
                        closure_deferred = true;
                        break;
                    }
                }

                for (std::size_t index = 0;
                     index < events.size(); ++index) {
                    if (issue_delay[index] != 0) {
                        update_feedback(index, issue_delay[index]);
                    } else {
                        auto& replay =
                            event_feedback[events[index].pending.core]
                                          [events[index].pending.index];
                        replay.latency_cycles = base_latency[index];
                        replay.exposed_cycles = base_exposed[index];
                    }
                }
                ++stats_.causal_timing_passes;
                stats_.causal_timing_replayed_events += anchored_events;
                auto next_timing = compute_timing_feedback(
                    event_feedback, accepted_begin, accepted_end);
                bool fixed_point = true;
                for (std::size_t index = 0;
                     index < events.size(); ++index) {
                    const auto& repair = events[index];
                    const auto position = static_cast<std::size_t>(
                        current_chunks_[repair.pending.core]
                            ->memory[repair.pending.index].uop_index) -
                        accepted_begin[repair.pending.core];
                    if (next_timing.issue_extra_q16[repair.pending.core]
                                                   [position] !=
                        timing.issue_extra_q16[repair.pending.core]
                                              [position]) {
                        fixed_point = false;
                        break;
                    }
                }
                timing = std::move(next_timing);
                if (fixed_point) {
                    ++stats_.causal_timing_stable_epochs;
                    closure_stable = true;
                    break;
                }
            }
            if (!closure_stable || closure_deferred) {
                for (std::size_t index = 0;
                     index < events.size(); ++index) {
                    auto& replay =
                        event_feedback[events[index].pending.core]
                                      [events[index].pending.index];
                    replay.latency_cycles = base_latency[index];
                    replay.exposed_cycles = base_exposed[index];
                }
                timing = base_timing;
                if (!closure_deferred) {
                    ++stats_.causal_timing_fallback_epochs;
                }
            }
            record_causal_wall_time();
        }

        for (std::size_t index = 0; index < events.size(); ++index) {
            const auto& repair = events[index];
            auto& cha = stats_.cha[repair.descriptor.cha];
            if (cha.queue_cycles <
                repair.descriptor.canonical_memory_queue_cycles) {
                throw std::logic_error(
                    "canonical DRAM queue accounting underflow");
            }
            cha.queue_cycles -=
                repair.descriptor.canonical_memory_queue_cycles;
            cha.queue_cycles +=
                stable_requests[index].arrival -
                    repair.descriptor.canonical_tag_ready +
                stable_batch.results[index].queue_cycles;
        }
        if (response_retime_pass) {
            for (const auto& shared : shared_arrivals) {
                const auto& descriptor = shared.descriptor;
                if (descriptor.canonical_queue_cycles <
                    descriptor.canonical_memory_queue_cycles) {
                    throw std::logic_error(
                        "canonical CHA-only queue accounting underflow");
                }
                const auto canonical_cha_queue =
                    descriptor.canonical_queue_cycles -
                    descriptor.canonical_memory_queue_cycles;
                auto& cha = stats_.cha[descriptor.cha];
                if (cha.queue_cycles < canonical_cha_queue) {
                    throw std::logic_error(
                        "canonical CHA queue accounting underflow");
                }
                cha.queue_cycles -= canonical_cha_queue;
                cha.queue_cycles += fair_cha_queue.at(
                    batch_event_key(shared.pending));
            }
        }
        shared_->install_memory_timing(
            std::move(stable_dram), std::move(stable_mshrs),
            fill_remaps);
        ++stats_.dram_frfcfs_stable_epochs;
        stats_.dram_frfcfs_reordered_requests +=
            stable_batch.reordered_requests;
        stats_.dram_frfcfs_row_hits += stable_batch.row_hits;
        stats_.dram_frfcfs_row_misses += stable_batch.row_misses;
        stats_.dram_frfcfs_max_pending = std::max(
            stats_.dram_frfcfs_max_pending,
            stable_batch.max_pending);
        stats_.dram_frfcfs_max_admitted_pending = std::max(
            stats_.dram_frfcfs_max_admitted_pending,
            stable_batch.max_admitted_pending);
        stats_.dram_frfcfs_saturated_selections +=
            stable_batch.saturated_selections;
        stats_.dram_frfcfs_page_policy_scanned_requests +=
            stable_batch.page_policy_scanned_requests;
        stats_.dram_frfcfs_outside_window_row_hits +=
            stable_batch.outside_window_row_hits;
        stats_.dram_frfcfs_outside_window_bank_conflicts +=
            stable_batch.outside_window_bank_conflicts;
        stats_.dram_frfcfs_row_cap_precharges +=
            stable_batch.row_cap_precharges;
        stats_.dram_frfcfs_adaptive_precharges +=
            stable_batch.adaptive_precharges;
        record_wall_time();
        if (response_timing_retime_enabled() &&
            !response_retime_pass) {
            const auto retime_started =
                std::chrono::steady_clock::now();
            const auto record_retime_wall_time = [&] {
                const auto ended = std::chrono::steady_clock::now();
                record_response_retime_wall_ns(
                    static_cast<std::uint64_t>(
                        std::chrono::duration_cast<
                            std::chrono::nanoseconds>(
                                ended - retime_started).count()));
            };
            auto corrected = corrected_shared_order(
                materialized, timing, accepted_begin, horizon_q16);
            stats_.rob_head_suffix_boundary_clipped_events +=
                count_rob_head_suffix_boundary_clips(
                    materialized, timing, accepted_begin, horizon_q16);
            std::uint64_t moved_shared = 0;
            for (const auto& pending : corrected) {
                const auto& descriptor =
                    event_feedback[pending.core][pending.index]
                        .shared_timing;
                moved_shared += descriptor.uncore_request &&
                    pending.corrected_issue_q16 != pending.issue_q16;
            }
            if (moved_shared == 0) {
                record_response_retime_noop();
                record_retime_wall_time();
                return true;
            }

            record_response_retime_candidate(moved_shared);
            const bool crosses_epoch = std::any_of(
                corrected.begin(), corrected.end(),
                [&](const BatchPending& pending) {
                    const auto& descriptor =
                        event_feedback[pending.core][pending.index]
                            .shared_timing;
                    return descriptor.uncore_request &&
                        pending.corrected_issue_q16 > horizon_q16;
                });
            if (crosses_epoch) {
                record_response_retime_fallback();
                record_retime_wall_time();
                return true;
            }
            record_response_retime_replayed(
                static_cast<std::uint64_t>(std::count_if(
                    corrected.begin(), corrected.end(),
                    [&](const BatchPending& pending) {
                        return event_feedback[pending.core][pending.index]
                            .shared_timing.uncore_request;
                    })));

            const auto first_timing_state =
                shared_->capture_timing_state();
            const auto first_timing = timing;
            std::vector<MemoryReplay> first_feedback;
            first_feedback.reserve(materialized.size());
            for (const auto& pending : materialized) {
                first_feedback.push_back(
                    event_feedback[pending.core][pending.index]);
            }
            std::vector<std::uint64_t> first_queue;
            first_queue.reserve(config_.cha_count);
            for (const auto& cha : stats_.cha) {
                first_queue.push_back(cha.queue_cycles);
            }
            for (std::uint32_t cha = 0;
                 cha < config_.cha_count; ++cha) {
                stats_.cha[cha].queue_cycles =
                    response_retime_entry_queue[cha];
            }

            const bool replayed = apply_frfcfs_dram_repair(
                corrected, event_feedback, accepted_begin,
                accepted_end, timing_start, horizon_q16, timing, true);
            bool retime_stable = false;
            if (replayed) {
                const auto next_corrected = corrected_shared_order(
                    materialized, timing, accepted_begin, horizon_q16);
                retime_stable = same_causal_timing_schedule(
                    corrected, next_corrected) &&
                    (!config_.interval_rob_head_suffix_replay ||
                     same_causal_timing_arrivals(
                         corrected, next_corrected));
            }
            if (retime_stable) {
                // The response pass may move queue/DRAM timing, but the
                // canonical functional path remains authoritative. Keep the
                // already-certified transient-fill membership from the
                // first FR-FCFS pass so a timing-only repair cannot change a
                // later epoch's LLC miss/merge classification.
                auto retimed_state = shared_->capture_timing_state();
                retimed_state.llc_transient_ready =
                    first_timing_state.llc_transient_ready;
                shared_->install_timing_state(std::move(retimed_state));
                record_response_retime_stable();
                record_retime_wall_time();
                return true;
            }

            shared_->install_timing_state(first_timing_state);
            timing = first_timing;
            for (std::size_t index = 0;
                 index < materialized.size(); ++index) {
                const auto& pending = materialized[index];
                event_feedback[pending.core][pending.index] =
                    first_feedback[index];
            }
            for (std::uint32_t cha = 0;
                 cha < config_.cha_count; ++cha) {
                stats_.cha[cha].queue_cycles = first_queue[cha];
            }
            record_response_retime_fallback();
            record_retime_wall_time();
        }
        return true;
    }

    static bool same_causal_timing_schedule(
        const std::vector<BatchPending>& left,
        const std::vector<BatchPending>& right) {
        if (left.size() != right.size()) return false;
        for (std::size_t index = 0; index < left.size(); ++index) {
            if (batch_event_key(left[index]) !=
                batch_event_key(right[index])) {
                return false;
            }
        }
        return true;
    }

    static bool same_causal_timing_arrivals(
        const std::vector<BatchPending>& left,
        const std::vector<BatchPending>& right) {
        if (left.size() != right.size()) return false;
        for (std::size_t index = 0; index < left.size(); ++index) {
            if (batch_event_key(left[index]) !=
                    batch_event_key(right[index]) ||
                left[index].corrected_issue_q16 !=
                    right[index].corrected_issue_q16) {
                return false;
            }
        }
        return true;
    }

    bool apply_response_timing_retime(
        const std::vector<BatchPending>& materialized,
        std::vector<std::vector<MemoryReplay>>& event_feedback,
        const std::vector<std::size_t>& accepted_begin,
        const std::vector<std::size_t>& accepted_end,
        const SharedSystem::TimingState& timing_start,
        std::uint64_t horizon_q16,
        TimingFeedback& timing) {
        if (materialized.empty()) return false;
        const auto started = std::chrono::steady_clock::now();
        const auto record_wall_time = [&] {
            const auto ended = std::chrono::steady_clock::now();
            record_response_retime_wall_ns(
                static_cast<std::uint64_t>(
                    std::chrono::duration_cast<std::chrono::nanoseconds>(
                        ended - started).count()));
        };

        auto corrected = corrected_shared_order(
            materialized, timing, accepted_begin, horizon_q16);
        stats_.rob_head_suffix_boundary_clipped_events +=
            count_rob_head_suffix_boundary_clips(
                materialized, timing, accepted_begin, horizon_q16);
        std::uint64_t moved_shared = 0;
        for (const auto& pending : corrected) {
            const auto& descriptor =
                event_feedback[pending.core][pending.index].shared_timing;
            moved_shared += descriptor.uncore_request &&
                pending.corrected_issue_q16 != pending.issue_q16;
        }
        if (moved_shared == 0) {
            record_response_retime_noop();
            record_wall_time();
            return false;
        }
        record_response_retime_candidate(moved_shared);
        const bool crosses_epoch = std::any_of(
            corrected.begin(), corrected.end(),
            [&](const BatchPending& pending) {
                const auto& descriptor =
                    event_feedback[pending.core][pending.index]
                        .shared_timing;
                return descriptor.uncore_request &&
                    pending.corrected_issue_q16 > horizon_q16;
            });
        if (crosses_epoch) {
            record_response_retime_fallback();
            record_wall_time();
            return false;
        }

        auto plan = plan_causal_timing_order(
            materialized, corrected, event_feedback);
        if (!plan.replayable || config_.inclusive_llc) {
            record_response_retime_fallback();
            record_wall_time();
            return false;
        }

        std::vector<MemoryReplay> canonical_feedback;
        canonical_feedback.reserve(materialized.size());
        const auto canonical_transient_ready =
            shared_->capture_timing_state().llc_transient_ready;
        std::vector<std::uint64_t> canonical_queue(
            config_.cha_count, 0);
        for (const auto& pending : materialized) {
            const auto& replay =
                event_feedback[pending.core][pending.index];
            canonical_feedback.push_back(replay);
            if (replay.shared_timing.uncore_request) {
                canonical_queue[replay.shared_timing.cha] +=
                    replay.shared_timing.canonical_queue_cycles;
            }
        }

        auto replay_state = timing_start;
        std::vector<std::uint64_t> corrected_queue(
            config_.cha_count, 0);
        std::uint64_t replayed_events = 0;
        for (const auto& pending : plan.timing_order) {
            auto& replay = event_feedback[pending.core][pending.index];
            if (!replay.shared_timing.uncore_request) continue;
            const auto& event = current_chunks_[pending.core]
                                    ->memory[pending.index];
            const auto issue_cycle =
                fixed_to_cycle_ceil(pending.corrected_issue_q16);
            const auto result = shared_->replay_timing(
                replay.shared_timing, issue_cycle, replay_state);
            replay.latency_cycles = result.completion > issue_cycle
                ? result.completion - issue_cycle
                : 0;
            const auto exposed_latency =
                replay.latency_cycles > event.lower_bound_latency
                    ? replay.latency_cycles - event.lower_bound_latency
                    : 0;
            replay.exposed_cycles = static_cast<std::uint64_t>(
                std::ceil(static_cast<double>(exposed_latency) *
                          config_.memory_exposure));
            corrected_queue[result.cha] += result.queue_cycles;
            ++replayed_events;
        }
        record_response_retime_replayed(replayed_events);

        auto pass_timing = compute_timing_feedback(
            event_feedback, accepted_begin, accepted_end);
        const auto next_corrected = corrected_shared_order(
            materialized, pass_timing, accepted_begin, horizon_q16);
        if (same_causal_timing_schedule(corrected, next_corrected) &&
            (!config_.interval_rob_head_suffix_replay ||
             same_causal_timing_arrivals(corrected, next_corrected))) {
            for (std::uint32_t cha = 0;
                 cha < config_.cha_count; ++cha) {
                if (stats_.cha[cha].queue_cycles <
                    canonical_queue[cha]) {
                    throw std::logic_error(
                        "canonical CHA queue accounting underflow");
                }
                stats_.cha[cha].queue_cycles -= canonical_queue[cha];
                stats_.cha[cha].queue_cycles += corrected_queue[cha];
            }
            replay_state.llc_transient_ready = canonical_transient_ready;
            shared_->install_timing_state(std::move(replay_state));
            timing = std::move(pass_timing);
            record_response_retime_stable();
            record_wall_time();
            return true;
        }

        for (std::size_t index = 0;
             index < materialized.size(); ++index) {
            const auto& pending = materialized[index];
            event_feedback[pending.core][pending.index] =
                canonical_feedback[index];
        }
        record_response_retime_fallback();
        record_wall_time();
        return false;
    }

    bool apply_causal_timing_repair(
        const std::vector<BatchPending>& materialized,
        std::vector<std::vector<MemoryReplay>>& event_feedback,
        const std::vector<std::size_t>& accepted_begin,
        const std::vector<std::size_t>& accepted_end,
        const SharedSystem::TimingState& timing_start,
        TimingFeedback& timing) {
        if (materialized.empty()) return false;
        const auto started = std::chrono::steady_clock::now();
        const auto record_wall_time = [&] {
            const auto ended = std::chrono::steady_clock::now();
            stats_.causal_timing_wall_ns +=
                static_cast<std::uint64_t>(
                    std::chrono::duration_cast<std::chrono::nanoseconds>(
                        ended - started).count());
        };

        auto raw_corrected = corrected_shared_order(
            materialized, timing, accepted_begin);
        auto plan = plan_causal_timing_order(
            materialized, raw_corrected, event_feedback);
        if (!plan.changed) {
            ++stats_.causal_timing_noop_epochs;
            record_wall_time();
            return false;
        }

        ++stats_.causal_timing_candidate_epochs;
        stats_.causal_closure_components += plan.closure_components;
        stats_.causal_closure_events += plan.closure_events;
        stats_.causal_max_closure_events = std::max(
            stats_.causal_max_closure_events, plan.closure_events);
        if (!plan.replayable || config_.inclusive_llc ||
            plan.closure_events >
                config_.interval_causal_max_closure_events) {
            ++stats_.causal_timing_deferred_epochs;
            record_wall_time();
            return false;
        }

        std::vector<MemoryReplay> canonical_feedback;
        canonical_feedback.reserve(materialized.size());
        std::vector<std::uint64_t> canonical_queue(config_.cha_count, 0);
        for (const auto& pending : materialized) {
            const auto& replay =
                event_feedback[pending.core][pending.index];
            canonical_feedback.push_back(replay);
            if (replay.shared_timing.uncore_request) {
                canonical_queue[replay.shared_timing.cha] +=
                    replay.shared_timing.canonical_queue_cycles;
            }
        }

        for (std::uint32_t pass = 0;
             pass < config_.interval_causal_passes; ++pass) {
            auto replay_state = timing_start;
            std::vector<std::uint64_t> corrected_queue(
                config_.cha_count, 0);
            for (const auto& pending : plan.timing_order) {
                auto& replay =
                    event_feedback[pending.core][pending.index];
                const auto& event = current_chunks_[pending.core]
                                        ->memory[pending.index];
                const auto result = shared_->replay_timing(
                    replay.shared_timing,
                    fixed_to_cycle_ceil(pending.corrected_issue_q16),
                    replay_state);
                const auto issue_cycle =
                    fixed_to_cycle_ceil(pending.corrected_issue_q16);
                replay.latency_cycles =
                    result.completion > issue_cycle
                        ? result.completion - issue_cycle
                        : 0;
                const auto exposed_latency =
                    replay.latency_cycles > event.lower_bound_latency
                        ? replay.latency_cycles -
                              event.lower_bound_latency
                        : 0;
                replay.exposed_cycles = static_cast<std::uint64_t>(
                    std::ceil(static_cast<double>(exposed_latency) *
                              config_.memory_exposure));
                if (replay.shared_timing.uncore_request) {
                    corrected_queue[result.cha] += result.queue_cycles;
                }
            }
            ++stats_.causal_timing_passes;
            stats_.causal_timing_replayed_events += materialized.size();

            auto pass_timing = compute_timing_feedback(
                event_feedback, accepted_begin, accepted_end);
            raw_corrected = corrected_shared_order(
                materialized, pass_timing, accepted_begin);
            auto next_plan = plan_causal_timing_order(
                materialized, raw_corrected, event_feedback);
            stats_.causal_max_closure_events = std::max(
                stats_.causal_max_closure_events,
                next_plan.closure_events);
            const bool stable = next_plan.replayable &&
                next_plan.closure_events <=
                    config_.interval_causal_max_closure_events &&
                same_causal_timing_schedule(
                    plan.timing_order, next_plan.timing_order);
            if (stable) {
                for (std::uint32_t cha = 0;
                     cha < config_.cha_count; ++cha) {
                    if (stats_.cha[cha].queue_cycles <
                        canonical_queue[cha]) {
                        throw std::logic_error(
                            "canonical CHA queue accounting underflow");
                    }
                    stats_.cha[cha].queue_cycles -= canonical_queue[cha];
                    stats_.cha[cha].queue_cycles += corrected_queue[cha];
                }
                shared_->install_timing_state(std::move(replay_state));
                timing = std::move(pass_timing);
                ++stats_.causal_timing_stable_epochs;
                record_wall_time();
                return true;
            }
            plan = std::move(next_plan);
        }

        for (std::size_t index = 0;
             index < materialized.size(); ++index) {
            const auto& pending = materialized[index];
            event_feedback[pending.core][pending.index] =
                canonical_feedback[index];
        }
        ++stats_.causal_timing_fallback_epochs;
        record_wall_time();
        return false;
    }

    bool has_shared_resource_conflict(
        const std::vector<BatchPending>& events) const {
        std::unordered_map<std::uint32_t, std::uint8_t> set_counts;
        set_counts.reserve(events.size());
        for (const auto& pending : events) {
            const auto line = current_chunks_[pending.core]
                                  ->memory[pending.index].line;
            auto& count = set_counts[llc_set_of(line)];
            if (count != 0) return true;
            count = 1;
        }
        return false;
    }

    bool same_shared_resource_order(
        const std::vector<BatchPending>& left,
        const std::vector<BatchPending>& right) const {
        if (left.size() != right.size()) return false;
        std::unordered_map<std::uint64_t, std::size_t> right_rank;
        right_rank.reserve(right.size());
        for (std::size_t rank = 0; rank < right.size(); ++rank) {
            const auto key =
                (static_cast<std::uint64_t>(right[rank].core) << 32) |
                right[rank].index;
            right_rank.emplace(key, rank);
        }
        std::unordered_map<std::uint32_t, std::size_t> last_rank;
        last_rank.reserve(left.size());
        for (std::size_t index = 0; index < left.size(); ++index) {
            const auto& pending = left[index];
            const auto key =
                (static_cast<std::uint64_t>(pending.core) << 32) |
                pending.index;
            const auto rank = right_rank.at(key);
            const auto line = current_chunks_[pending.core]
                                  ->memory[pending.index].line;
            const auto set = llc_set_of(line);
            const auto previous = last_rank.find(set);
            if (previous != last_rank.end() && rank < previous->second) {
                return false;
            }
            last_rank[set] = rank;
        }
        return true;
    }

    void run_interval_weave() {
        const bool time_epoch =
            config_.interval_scheduler == "time_epoch";
        if (!time_epoch) {
            for (std::uint32_t core = 0; core < config_.cores; ++core) {
                load_interval_chunk(core);
            }
        }

        auto& global_time_q16 = phase_global_time_q16_;
        const auto max_step_q16 =
            cycles_to_fixed(config_.interval_max_cycles);
        std::vector<std::size_t> accepted_begin(config_.cores);
        std::vector<std::size_t> accepted_end(config_.cores);
        std::vector<std::size_t> accepted_memory_begin(config_.cores);
        std::vector<std::size_t> accepted_memory_end(config_.cores);
        std::vector<std::uint32_t> last_inflight_memory_uop(
            config_.cores, std::numeric_limits<std::uint32_t>::max());
        // Reuse the largest observed epoch storage.  Only feedback slots in
        // the accepted memory range are reset below; every consumer is
        // constrained by the same accepted UOP bounds.  This removes a full
        // resident-buffer initialization and allocation cycle per epoch.
        std::vector<BatchPending> batch;
        std::vector<std::vector<MemoryReplay>> event_feedback(
            config_.cores);

        while (!all_finished()) {
            const auto epoch_started = std::chrono::steady_clock::now();
            std::uint64_t horizon_q16 = 0;
            if (time_epoch) {
                const auto proposed_horizon = saturating_add(
                    global_time_q16, max_step_q16);
                ensure_epoch_lookahead(proposed_horizon);
                if (all_finished()) break;
                horizon_q16 = proposed_horizon;
            } else {
                auto minimum_candidate =
                    std::numeric_limits<std::uint64_t>::max();
                bool found_active = false;
                for (std::uint32_t core = 0; core < config_.cores; ++core) {
                    if (finished_[core]) continue;
                    const auto& chunk = *current_chunks_[core];
                    const auto cursor = current_uop_indices_[core];
                    if (cursor >= chunk.uops.size()) {
                        throw std::logic_error(
                            "interval UOP cursor is outside resident chunk");
                    }
                    const auto remaining = chunk.uops.size() - cursor;
                    const auto stride = std::min<std::size_t>(
                        config_.interval_target_uops, remaining);
                    const auto candidate = saturating_add(
                        chunk.uops[cursor + stride - 1].retire_q16,
                        interval_gap_q16_[core]);
                    minimum_candidate = std::min(
                        minimum_candidate, candidate);
                    found_active = true;
                }
                if (!found_active) break;
                const auto capped_horizon = saturating_add(
                    global_time_q16, max_step_q16);
                horizon_q16 = std::min(
                    minimum_candidate, capped_horizon);
                horizon_q16 = std::max(
                    horizon_q16, global_time_q16);
            }

            batch.clear();
            std::fill(last_inflight_memory_uop.begin(),
                      last_inflight_memory_uop.end(),
                      std::numeric_limits<std::uint32_t>::max());
            const auto old_gap_q16 = interval_gap_q16_;
            std::uint64_t accepted_uops = 0;
            std::uint64_t active_prefixes = 0;

            for (std::uint32_t core = 0; core < config_.cores; ++core) {
                accepted_begin[core] = current_uop_indices_[core];
                accepted_end[core] = current_uop_indices_[core];
                accepted_memory_begin[core] = 0;
                accepted_memory_end[core] = 0;
                if (finished_[core]) continue;
                const auto& chunk = *current_chunks_[core];
                auto end = current_uop_indices_[core];
                while (end < chunk.uops.size() &&
                       saturating_add(chunk.uops[end].retire_q16,
                                      old_gap_q16[core]) <= horizon_q16) {
                    ++end;
                }
                if (time_epoch) {
                    const auto first_memory = static_cast<std::size_t>(
                        chunk.uops[current_uop_indices_[core]].first_memory);
                    if (first_memory > chunk.memory.size()) {
                        throw std::logic_error(
                            "time-epoch memory cursor exceeds resident "
                            "buffer");
                    }
                    // Per-core memory issue time is monotonic: the bound
                    // producer preserves memory program order.  Locate the
                    // first event beyond the horizon directly instead of
                    // scanning every remaining UOP in the resident buffer.
                    const auto issued_end = std::upper_bound(
                        chunk.memory.begin() +
                            static_cast<std::ptrdiff_t>(first_memory),
                        chunk.memory.end(), horizon_q16,
                        [&](std::uint64_t horizon,
                            const ChunkMemoryEvent& event) {
                            return horizon < saturating_add(
                                event.delta_q16, old_gap_q16[core]);
                        });
                    if (issued_end !=
                        chunk.memory.begin() +
                            static_cast<std::ptrdiff_t>(first_memory)) {
                        const auto last_uop =
                            static_cast<std::size_t>(
                                std::prev(issued_end)->uop_index);
                        if (last_uop >= chunk.uops.size()) {
                            throw std::logic_error(
                                "time-epoch memory event references an "
                                "invalid UOP");
                        }
                        end = std::max(end, last_uop + 1);
                    }
                }
                accepted_end[core] = end;
                accepted_uops += end - accepted_begin[core];
                const auto count = end - accepted_begin[core];
                if (count != 0) ++active_prefixes;
                if (count == 0) continue;
                const auto memory_begin = static_cast<std::size_t>(
                    chunk.uops[accepted_begin[core]].first_memory);
                const auto memory_end =
                    end == chunk.uops.size()
                        ? chunk.memory.size()
                        : static_cast<std::size_t>(
                              chunk.uops[end].first_memory);
                if (memory_end < memory_begin ||
                    memory_end > chunk.memory.size()) {
                    throw std::logic_error(
                        "interval memory range is not monotonic");
                }
                accepted_memory_begin[core] = memory_begin;
                accepted_memory_end[core] = memory_end;
                if (event_feedback[core].size() < memory_end) {
                    event_feedback[core].resize(memory_end);
                }
            }

            stats_.interval_accepted_uops += accepted_uops;
            stats_.interval_active_prefixes += active_prefixes;
            stats_.max_interval_accepted_uops =
                std::max(stats_.max_interval_accepted_uops,
                         accepted_uops);

            if (accepted_uops == 0) {
                ++stats_.interval_zero_progress_steps;
                if (horizon_q16 == global_time_q16) {
                    throw std::logic_error(
                        "interval global-time scheduler made no progress");
                }
            }

            // Each core contributes a memory stream already sorted by
            // (issue, per-core ordinal). Merge the C resident streams rather
            // than sorting all E events, preserving the exact canonical key
            // (issue, core, ordinal) in O(E log C).
            PendingQueue merge_queue;
            for (std::uint32_t core = 0; core < config_.cores; ++core) {
                const auto begin = accepted_memory_begin[core];
                if (begin == accepted_memory_end[core]) continue;
                const auto& event =
                    current_chunks_[core]->memory[begin];
                const auto issue = saturating_add(
                    event.delta_q16, old_gap_q16[core]);
                merge_queue.push(Pending{
                    issue, core, static_cast<std::uint32_t>(begin)});
            }
            while (!merge_queue.empty()) {
                const auto pending = merge_queue.top();
                merge_queue.pop();
                const auto event_index =
                    static_cast<std::size_t>(pending.index);
                const auto& chunk = *current_chunks_[pending.core];
                const auto& event = chunk.memory[event_index];
                const auto uop = static_cast<std::size_t>(event.uop_index);
                if (uop < accepted_begin[pending.core] ||
                    uop >= accepted_end[pending.core]) {
                    throw std::logic_error(
                        "interval memory event is outside its accepted UOP "
                        "range");
                }
                const auto& bound = chunk.uops[uop];
                const auto first =
                    static_cast<std::size_t>(bound.first_memory);
                if (event_index < first ||
                    event_index >= first + bound.memory_count) {
                    throw std::logic_error(
                        "interval memory/UOP mapping is corrupt");
                }
                if (pending.issue_q16 > horizon_q16) {
                    throw std::logic_error(
                        "accepted UOP has a memory issue beyond the global "
                        "horizon");
                }
                event_feedback[pending.core][event_index] = MemoryReplay{};
                const auto rank = batch.size();
                batch.push_back(BatchPending{
                    pending.issue_q16, pending.issue_q16, rank,
                    pending.core, pending.index});

                if (last_inflight_memory_uop[pending.core] !=
                        event.uop_index &&
                    saturating_add(bound.retire_q16,
                                   old_gap_q16[pending.core]) >
                        horizon_q16) {
                    ++stats_.epoch_inflight_memory_uops;
                    last_inflight_memory_uop[pending.core] =
                        event.uop_index;
                }

                const auto next = event_index + 1;
                if (next < accepted_memory_end[pending.core]) {
                    const auto& next_event = chunk.memory[next];
                    const auto next_issue = saturating_add(
                        next_event.delta_q16,
                        old_gap_q16[pending.core]);
                    if (next_issue < pending.issue_q16) {
                        throw std::logic_error(
                            "per-core memory issue order is not monotonic");
                    }
                    merge_queue.push(Pending{
                        next_issue, pending.core,
                        static_cast<std::uint32_t>(next)});
                }
            }
            preflight_corrected_epoch_suffix(
                batch, event_feedback, accepted_begin, accepted_end,
                horizon_q16);
            stats_.batch_memory_events += batch.size();
            stats_.max_batch_memory_events = std::max<std::uint64_t>(
                stats_.max_batch_memory_events, batch.size());
            const auto batch_ready = std::chrono::steady_clock::now();

            std::optional<SharedSystem::Transaction> epoch_transaction;
            std::vector<CoreCounters> epoch_core_counters;
            if (config_.interval_corrected_suffix_carry &&
                !batch.empty()) {
                epoch_transaction.emplace(
                    shared_->begin_transaction(batch.size()));
                epoch_core_counters.reserve(config_.cores);
                for (const auto& core_state : cores_) {
                    epoch_core_counters.push_back(core_state->total);
                }
            }
            auto* suffix_transaction = epoch_transaction.has_value()
                ? &*epoch_transaction
                : nullptr;

            const bool preview_enabled =
                time_epoch && config_.interval_private_preview;
            const bool page_fault_state_epoch = std::any_of(
                batch.begin(), batch.end(), [&](const auto& pending) {
                    return current_chunks_[pending.core]
                        ->memory[pending.index].page_fault_state_fill;
                });
            const bool preview_requested =
                preview_enabled && !page_fault_state_epoch &&
                batch.size() >= config_.domain_min_events;
            if (preview_enabled && !batch.empty() &&
                !preview_requested) {
                ++stats_.private_preview_bypass_epochs;
                stats_.private_preview_bypass_events += batch.size();
            }
            PrivatePreviewPlan preview_plan;
            bool preview_low_yield = false;
            if (preview_requested && !batch.empty()) {
                const auto certificate_started =
                    std::chrono::steady_clock::now();
                preview_plan = plan_private_preview(batch);
                const auto certificate_ended =
                    std::chrono::steady_clock::now();
                stats_.state_certificate_wall_ns +=
                    static_cast<std::uint64_t>(
                        std::chrono::duration_cast<
                            std::chrono::nanoseconds>(
                            certificate_ended - certificate_started)
                            .count());
                if (!preview_plan.all_safe()) {
                    ++stats_.state_certificate_failures;
                }
                preview_low_yield = preview_plan.any_safe() &&
                    preview_plan.safe_events <
                        config_.domain_min_events;
                if (preview_low_yield) {
                    ++stats_.private_preview_bypass_epochs;
                    stats_.private_preview_bypass_events += batch.size();
                } else {
                    stats_.private_preview_events +=
                        preview_plan.safe_events;
                    stats_.private_preview_unsafe_events +=
                        preview_plan.unsafe_events;
                    stats_.private_preview_safe_cores +=
                        preview_plan.safe_cores;
                    stats_.private_preview_unsafe_cores +=
                        preview_plan.unsafe_cores;
                }
            }
            const bool preview_epoch =
                preview_plan.any_safe() && !preview_low_yield;
            if (preview_epoch) {
                for (const auto& pending : batch) {
                    if (private_preview_allowed(
                            preview_plan, pending)) {
                        event_feedback[pending.core][pending.index]
                            .private_previewed = true;
                    }
                }
                run_private_preview(
                    accepted_begin, accepted_end, event_feedback,
                    suffix_transaction);
                ++stats_.private_preview_epochs;
                if (!preview_plan.all_safe()) {
                    ++stats_.private_preview_partial_epochs;
                }
            } else if (preview_requested && !batch.empty() &&
                       !preview_low_yield) {
                ++stats_.canonical_fallback_epochs;
            }

            TimingFeedback timing;
            std::vector<BatchPending> causal_events;
            std::optional<SharedSystem::TimingState> shared_timing_start;
            if ((config_.interval_causal_timing ||
                 response_timing_retime_enabled() ||
                 config_.dram.scheduler == "frfcfs") &&
                !batch.empty()) {
                shared_timing_start = shared_->capture_timing_state();
            }
            // FR-FCFS repairs only queue timing after the canonical path has
            // committed cache/directory state.  With a single state-weave
            // pass, the canonical core feedback is otherwise immediately
            // overwritten on every certified repair.  Defer that work until
            // the repair either succeeds (and computes the repaired
            // feedback) or falls back (and needs the canonical feedback).
            const bool defer_initial_timing_feedback =
                config_.dram.scheduler == "frfcfs" &&
                shared_timing_start.has_value() &&
                config_.interval_reweave_passes == 1;
            if (preview_epoch) {
                std::vector<BatchPending> materialized;
                materialized.reserve(batch.size());
                for (const auto& pending : batch) {
                    const auto& event = current_chunks_[pending.core]
                                            ->memory[pending.index];
                    const auto& preview =
                        event_feedback[pending.core][pending.index];
                    if (!preview.private_previewed ||
                        materializes_shared_event(event, preview)) {
                        materialized.push_back(pending);
                    }
                }
                stats_.materialized_escape_events += materialized.size();
                causal_events = materialized;

                const auto replay_shared = [&] (
                    const std::vector<BatchPending>& order,
                    SharedSystem::Transaction* transaction) {
                    for (const auto& pending : order) {
                        const auto& event = current_chunks_[pending.core]
                                                ->memory[pending.index];
                        if (event_feedback[pending.core][pending.index]
                                .private_previewed) {
                            const auto preview =
                                event_feedback[pending.core][pending.index];
                            event_feedback[pending.core][pending.index] =
                                replay_previewed_memory_event(
                                    pending.core, event,
                                    pending.corrected_issue_q16, true,
                                    preview, transaction);
                        } else {
                            event_feedback[pending.core][pending.index] =
                                replay_memory_event(
                                    pending.core, event,
                                    pending.corrected_issue_q16, true,
                                    transaction);
                        }
                    }
                };

                bool committed = false;
                if (config_.interval_reweave_passes > 1 &&
                    has_shared_resource_conflict(materialized)) {
                    std::vector<CoreCounters> counters_before;
                    counters_before.reserve(config_.cores);
                    for (const auto& core_state : cores_) {
                        counters_before.push_back(core_state->total);
                    }
                    const auto restore_counters = [&] {
                        for (std::uint32_t core = 0;
                             core < config_.cores; ++core) {
                            cores_[core]->total = counters_before[core];
                        }
                    };
                    auto candidate = materialized;
                    for (std::uint32_t pass = 0;
                         pass < config_.interval_reweave_passes; ++pass) {
                        auto transaction = shared_->begin_transaction();
                        replay_shared(candidate, &transaction);
                        auto pass_timing = compute_timing_feedback(
                            event_feedback, accepted_begin, accepted_end);
                        auto corrected = corrected_shared_order(
                            materialized, pass_timing, accepted_begin);
                        // Pass zero establishes feedback from the lower-bound
                        // schedule.  A later pass uses corrected arrival time;
                        // stable non-commuting resource order is the path
                        // certificate for cache/directory state.
                        if (pass != 0 && same_shared_resource_order(
                                candidate, corrected)) {
                            timing = std::move(pass_timing);
                            committed = true;
                            break;
                        }
                        shared_->restore(transaction);
                        restore_counters();
                        ++stats_.timing_reweave_passes;
                        stats_.replayed_shared_events += candidate.size();
                        candidate = std::move(corrected);
                    }
                    if (!committed) {
                        ++stats_.timing_certificate_failures;
                        ++stats_.canonical_fallback_epochs;
                    }
                }
                if (!committed) {
                    replay_shared(materialized, suffix_transaction);
                    if (!defer_initial_timing_feedback) {
                        timing = compute_timing_feedback(
                            event_feedback, accepted_begin,
                            accepted_end);
                    }
                }
            } else {
                causal_events = batch;
                const auto conflict_plan =
                    config_.interval_reweave_passes > 1
                        ? plan_cross_core_line_conflicts(batch)
                        : PathConflictPlan{};
                bool committed = false;
                bool replay_epoch_counted = false;
                if (!conflict_plan.empty()) {
                    ++stats_.corrected_arrival_candidate_epochs;
                    stats_.corrected_arrival_conflict_components +=
                        conflict_plan.cross_core_lines.size();
                    stats_.corrected_arrival_component_events +=
                        conflict_plan.component_events;
                    stats_.corrected_arrival_max_component_events =
                        std::max(
                            stats_.corrected_arrival_max_component_events,
                            conflict_plan.max_component_events);

                    std::vector<CoreCounters> counters_before;
                    counters_before.reserve(config_.cores);
                    for (const auto& core_state : cores_) {
                        counters_before.push_back(core_state->total);
                    }
                    const auto restore_counters = [&] {
                        for (std::uint32_t core = 0;
                             core < config_.cores; ++core) {
                            cores_[core]->total = counters_before[core];
                        }
                    };

                    auto candidate = batch;
                    for (std::uint32_t pass = 0;
                         pass < config_.interval_reweave_passes; ++pass) {
                        if (pass != 0) {
                            ++stats_.timing_reweave_passes;
                            stats_.corrected_arrival_replayed_events +=
                                candidate.size();
                            if (!replay_epoch_counted) {
                                ++stats_.corrected_arrival_replay_epochs;
                                replay_epoch_counted = true;
                            }
                        }
                        auto transaction = shared_->begin_transaction();
                        for (const auto& pending : candidate) {
                            const auto& event =
                                current_chunks_[pending.core]
                                    ->memory[pending.index];
                            event_feedback[pending.core][pending.index] =
                                replay_memory_event(
                                    pending.core, event,
                                    pending.corrected_issue_q16, true,
                                    &transaction);
                        }
                        auto pass_timing = compute_timing_feedback(
                            event_feedback, accepted_begin, accepted_end);
                        auto corrected = corrected_shared_order(
                            batch, pass_timing, accepted_begin);
                        const auto order_stable =
                            same_conflict_component_order(
                                candidate, corrected, conflict_plan);
                        const auto arrivals_unchanged =
                            same_conflict_component_arrivals(
                                candidate, corrected, conflict_plan);

                        // The first pass uses the lower-bound schedule.  It
                        // can commit only if feedback changes neither the
                        // risky arrivals nor their relative order.  After a
                        // corrected-arrival pass, stable relative order is a
                        // path certificate; timing comes from that pass.
                        if (order_stable &&
                            (pass != 0 || arrivals_unchanged)) {
                            timing = std::move(pass_timing);
                            committed = true;
                            if (pass != 0) {
                                ++stats_.corrected_arrival_stable_epochs;
                            }
                            break;
                        }

                        shared_->restore(transaction);
                        restore_counters();
                        if (pass + 1 <
                            config_.interval_reweave_passes) {
                            candidate = std::move(corrected);
                            continue;
                        }

                        ++stats_.timing_certificate_failures;
                        ++stats_.canonical_fallback_epochs;
                        ++stats_.corrected_arrival_fallback_epochs;
                    }
                }

                if (!committed) {
                    if (!conflict_plan.empty() &&
                        replay_epoch_counted) {
                        stats_.corrected_arrival_replayed_events +=
                            batch.size();
                    }
                    for (const auto& pending : batch) {
                        const auto& event = current_chunks_[pending.core]
                                                ->memory[pending.index];
                            event_feedback[pending.core][pending.index] =
                                replay_memory_event(
                                    pending.core, event,
                                    pending.issue_q16, true,
                                    suffix_transaction);
                    }
                    if (!defer_initial_timing_feedback) {
                        timing = compute_timing_feedback(
                            event_feedback, accepted_begin,
                            accepted_end);
                    }
                }
            }
            bool frfcfs_repaired = false;
            if (config_.dram.scheduler == "frfcfs" &&
                shared_timing_start.has_value()) {
                frfcfs_repaired = apply_frfcfs_dram_repair(
                    causal_events, event_feedback, accepted_begin,
                    accepted_end, *shared_timing_start, horizon_q16,
                    timing);
            }
            if (defer_initial_timing_feedback && !frfcfs_repaired) {
                timing = compute_timing_feedback(
                    event_feedback, accepted_begin, accepted_end);
            }
            if (response_timing_retime_enabled() &&
                !frfcfs_repaired &&
                shared_timing_start.has_value()) {
                apply_response_timing_retime(
                    causal_events, event_feedback, accepted_begin,
                    accepted_end, *shared_timing_start, horizon_q16,
                    timing);
            }
            if (config_.interval_causal_timing &&
                !frfcfs_repaired &&
                shared_timing_start.has_value()) {
                apply_causal_timing_repair(
                    causal_events, event_feedback, accepted_begin,
                    accepted_end, *shared_timing_start, timing);
            }
            if (epoch_transaction.has_value()) {
                repair_corrected_epoch_suffix(
                    batch, event_feedback, accepted_begin, accepted_end,
                    *epoch_transaction, epoch_core_counters,
                    horizon_q16, timing);
            }
            const auto weave_ready = std::chrono::steady_clock::now();

            for (const auto& pending : batch) {
                const auto& replay =
                    event_feedback[pending.core][pending.index];
                if (config_.cpi_attribution) {
                    ++stats_.response_latency_samples;
                    stats_.response_latency_cycles +=
                        replay.latency_cycles;
                    if (replay.private_level == HitLevel::kL1) {
                        ++stats_.response_l1_samples;
                        stats_.response_l1_latency_cycles +=
                            replay.latency_cycles;
                    } else if (replay.private_level == HitLevel::kL2) {
                        ++stats_.response_l2_samples;
                        stats_.response_l2_latency_cycles +=
                            replay.latency_cycles;
                    } else {
                        ++stats_.response_escape_samples;
                        stats_.response_escape_latency_cycles +=
                            replay.latency_cycles;
                    }
                }
                if (replay.shared_escape) {
                    ++stats_.interval_escape_memory_events;
                } else {
                    ++stats_.interval_private_memory_events;
                }
            }
            commit_timing_feedback(
                timing, accepted_begin, accepted_end, old_gap_q16,
                horizon_q16, time_epoch);
            audit_batch_order(
                batch, timing.issue_extra_q16, accepted_begin);
            audit_corrected_epoch_boundary(
                batch, timing.issue_extra_q16, accepted_begin,
                horizon_q16, time_epoch);

            for (std::uint32_t core = 0; core < config_.cores; ++core) {
                if (finished_[core] ||
                    accepted_begin[core] == accepted_end[core]) {
                    continue;
                }
                current_uop_indices_[core] = accepted_end[core];
                if (time_epoch) {
                    compact_epoch_buffer(core);
                    continue;
                }
                auto& chunk = *current_chunks_[core];
                if (current_uop_indices_[core] == chunk.uops.size()) {
                    const bool reached_end = chunk.reached_end;
                    current_chunks_[core].reset();
                    if (reached_end) {
                        finished_[core] = true;
                    } else {
                        load_interval_chunk(core);
                    }
                }
            }
            if (time_epoch) {
                stats_.epoch_advanced_cycles += fixed_to_cycle_ceil(
                    horizon_q16 - global_time_q16);
            }
            global_time_q16 = horizon_q16;
            ++stats_.interval_steps;
            const auto epoch_ended = std::chrono::steady_clock::now();
            stats_.interval_schedule_batch_wall_ns +=
                static_cast<std::uint64_t>(
                    std::chrono::duration_cast<std::chrono::nanoseconds>(
                        batch_ready - epoch_started).count());
            stats_.interval_weave_wall_ns += static_cast<std::uint64_t>(
                std::chrono::duration_cast<std::chrono::nanoseconds>(
                    weave_ready - batch_ready).count());
            stats_.interval_commit_wall_ns += static_cast<std::uint64_t>(
                std::chrono::duration_cast<std::chrono::nanoseconds>(
                    epoch_ended - weave_ready).count());
        }
    }

    void prime_core(std::uint32_t core, PendingQueue& queue) {
        while (!finished_[core]) {
            auto chunk = acquire_chunk(core);
            cores_[core]->total += chunk->counters;
            current_indices_[core] = 0;
            if (chunk->memory.empty()) {
                if (!chunk->interval_bound) {
                    ready_q16_[core] += chunk->tail_q16;
                }
                const bool reached_end = chunk->reached_end;
                if (reached_end) {
                    finished_[core] = true;
                    return;
                }
                continue;
            }
            const auto first_issue = chunk->interval_bound
                ? chunk->memory.front().delta_q16 +
                      interval_gap_q16_[core]
                : ready_q16_[core] +
                      chunk->memory.front().delta_q16;
            current_chunks_[core] = std::move(chunk);
            queue.push(Pending{first_issue, core, 0});
            return;
        }
    }

    MemoryReplay replay_memory_event(
        std::uint32_t core, const ChunkMemoryEvent& event,
        std::uint64_t issue_q16, bool subtract_lower_bound,
        SharedSystem::Transaction* transaction = nullptr) {
        auto* private_transaction =
            transaction == nullptr
                ? nullptr
                : &transaction->private_caches[core];
        if (event.page_fault_state_fill) {
            shared_->seed_page_fault_page(
                core, event.page_first_line,
                4096u / config_.l1d.line_size, transaction);
        }
        const auto private_result = private_caches_[core]->access(
            event.line, event.write, cores_[core]->total,
            private_transaction);
        MemoryReplay preview;
        preview.private_level = private_result.level;
        preview.l2_evicted = private_result.l2_evicted;
        preview.l2_evicted_dirty = private_result.l2_evicted_dirty;
        preview.l2_evicted_line = private_result.l2_evicted_line;
        return replay_previewed_memory_event(
            core, event, issue_q16, subtract_lower_bound,
            preview, transaction);
    }

    MemoryReplay replay_previewed_memory_event(
        std::uint32_t core, const ChunkMemoryEvent& event,
        std::uint64_t issue_q16, bool subtract_lower_bound,
        const MemoryReplay& preview,
        SharedSystem::Transaction* transaction = nullptr) {
        const auto issue_cycle =
            fixed_to_cycle_ceil(issue_q16);
        bool timing_replayable = true;
        if (preview.l2_evicted) {
            timing_replayable = !shared_->private_evict(
                core, preview.l2_evicted_line,
                preview.l2_evicted_dirty, issue_cycle, transaction);
        }
        const auto shared_result = shared_->access(
            core, event.line, event.write,
            preview.private_level, issue_cycle, transaction);
        const auto latency =
            shared_result.completion > issue_cycle
                ? shared_result.completion - issue_cycle
                : 0;
        if (latency > (1ull << 40)) {
            throw std::overflow_error(
                "unbounded shared latency: core=" +
                std::to_string(core) + " line=" +
                std::to_string(event.line) + " issue=" +
                std::to_string(issue_cycle) + " completion=" +
                std::to_string(shared_result.completion));
        }
        auto exposed_latency = latency;
        if (subtract_lower_bound) {
            exposed_latency = latency > event.lower_bound_latency
                                   ? latency - event.lower_bound_latency
                                   : 0;
        }
        auto replay = preview;
        replay.shared_timing = shared_result.timing;
        replay.shared_timing.replayable =
            replay.shared_timing.replayable && timing_replayable;
        replay.latency_cycles = latency;
        replay.exposed_cycles = static_cast<std::uint64_t>(
            std::ceil(static_cast<double>(exposed_latency) *
                      config_.memory_exposure));
        replay.shared_escape =
            preview.l2_evicted || shared_result.uncore_request;
        return replay;
    }

    void process_memory_event(const Pending& pending) {
        auto& chunk = *current_chunks_[pending.core];
        if (pending.index != current_indices_[pending.core] ||
            pending.index >= chunk.memory.size()) {
            throw std::logic_error("causal frontier event index mismatch");
        }
        const auto& event = chunk.memory[pending.index];
        const auto replay = replay_memory_event(
            pending.core, event, pending.issue_q16,
            chunk.interval_bound);
        const auto exposed = replay.exposed_cycles;
        cores_[pending.core]->total.memory_penalty_cycles += exposed;
        if (chunk.interval_bound) {
            interval_gap_q16_[pending.core] += cycles_to_fixed(exposed);
        } else {
            ready_q16_[pending.core] =
                pending.issue_q16 + cycles_to_fixed(exposed);
        }
    }

    void advance_core(std::uint32_t core, PendingQueue& queue) {
        auto& chunk = *current_chunks_[core];
        const auto next = ++current_indices_[core];
        if (next < chunk.memory.size()) {
            const auto issue = chunk.interval_bound
                ? chunk.memory[next].delta_q16 + interval_gap_q16_[core]
                : ready_q16_[core] + chunk.memory[next].delta_q16;
            queue.push(Pending{issue, core,
                               static_cast<std::uint32_t>(next)});
            return;
        }
        if (!chunk.interval_bound) ready_q16_[core] += chunk.tail_q16;
        const bool reached_end = chunk.reached_end;
        current_chunks_[core].reset();
        if (reached_end) {
            finished_[core] = true;
        } else {
            prime_core(core, queue);
        }
    }

    bool all_finished() const {
        return std::all_of(
            finished_.begin(), finished_.end(),
            [](bool finished) { return finished; });
    }

    SimulatorConfig config_;
    SimulationStats stats_;
    bool measurement_warmup_enabled_ = false;
    std::uint64_t phase_global_time_q16_ = 0;
    std::vector<std::uint64_t> measurement_origin_q16_;
    std::vector<std::unique_ptr<HardwareCoreState>> cores_;
    std::vector<std::unique_ptr<ThreadState>> threads_;
    ProcessMemoryState process_memory_;
    std::vector<std::unique_ptr<PrivateHierarchy>> private_caches_;
    std::unique_ptr<SharedSystem> shared_;

    std::vector<std::deque<std::unique_ptr<CoreChunk>>> chunk_queues_;
    std::vector<std::unique_ptr<CoreChunk>> current_chunks_;
    std::vector<std::size_t> current_indices_;
    std::vector<std::size_t> current_uop_indices_;
    std::vector<std::uint64_t> ready_q16_;
    std::vector<std::uint64_t> interval_gap_q16_;
    std::vector<std::vector<std::uint64_t>> response_iq_ready_cycles_;
    std::vector<std::deque<ResponseRenameRelease>>
        response_rename_releases_;
    std::vector<std::array<std::uint64_t, kTrackedRegisterClasses>>
        response_rename_live_;
    std::vector<std::uint64_t> response_rename_cycle_;
    std::vector<std::uint32_t> response_rename_used_;
    std::vector<CriticalCause> response_rename_cause_;
    std::vector<ResponseRenameCounters> response_rename_counters_;
    std::vector<std::uint64_t> response_dispatch_cycle_;
    std::vector<std::uint32_t> response_dispatch_used_;
    std::vector<CriticalCause> response_dispatch_cause_;
    std::vector<std::vector<std::uint64_t>> response_rob_ready_cycles_;
    std::vector<std::vector<std::uint64_t>> response_lq_ready_cycles_;
    std::vector<std::vector<std::uint64_t>> response_sq_ready_cycles_;
    std::vector<std::uint32_t> response_rob_next_slot_;
    std::vector<std::uint32_t> response_lq_next_slot_;
    std::vector<std::uint32_t> response_sq_next_slot_;
    std::vector<std::uint64_t> response_commit_cycle_;
    std::vector<std::uint32_t> response_commit_used_;
    std::vector<CriticalCause> response_commit_cause_;
    std::vector<std::uint64_t> response_store_drain_ready_cycle_;
    std::vector<std::vector<SparseRobEntry>>
        response_sparse_rob_entries_;
    std::vector<std::uint8_t> response_rob_head_suffix_open_;
    // Absolute response calendars committed at interval checkpoints. Timing
    // and reweave candidates operate on copies, so retries cannot consume a
    // Sequencer slot twice.
    std::vector<std::vector<std::uint64_t>> sequencer_ready_cycles_;
    std::vector<bool> finished_;
    std::vector<bool> producer_finished_;

    std::size_t certificate_component_sets_ = 0;
    std::vector<std::size_t> certificate_component_next_rank_;
    std::vector<EpochAccessorSlot> epoch_accessor_slots_;
    std::uint32_t epoch_accessor_generation_ = 0;

    std::vector<std::thread> workers_;
    std::mutex queue_mutex_;
    std::condition_variable producer_cv_;
    std::condition_variable consumer_cv_;
    std::size_t resident_chunks_ = 0;
    std::uint64_t max_resident_chunks_ = 0;
    bool stopping_ = false;
    std::exception_ptr worker_error_;

    std::uint32_t domain_worker_count_ = 0;
    std::vector<std::thread> domain_workers_;
    std::mutex domain_mutex_;
    std::condition_variable domain_cv_;
    std::condition_variable domain_done_cv_;
    std::atomic<std::uint32_t> domain_next_core_{0};
    DomainPhaseTask domain_task_ = DomainPhaseTask::kNone;
    std::uint32_t domain_task_count_ = 0;
    const std::function<void(std::uint32_t)>* domain_index_task_ = nullptr;
    std::uint64_t domain_generation_ = 0;
    std::size_t domain_workers_pending_ = 0;
    bool domain_stopping_ = false;
    std::exception_ptr domain_error_;

    const std::vector<std::size_t>* preview_begin_ = nullptr;
    const std::vector<std::size_t>* preview_end_ = nullptr;
    std::vector<std::vector<MemoryReplay>>* preview_feedback_ = nullptr;
    SharedSystem::Transaction* preview_transaction_ = nullptr;

    const std::vector<std::vector<MemoryReplay>>*
        timing_event_feedback_ = nullptr;
    const std::vector<std::size_t>* timing_begin_ = nullptr;
    const std::vector<std::size_t>* timing_end_ = nullptr;
    TimingFeedback* timing_output_ = nullptr;
};

Simulator::Simulator(SimulatorConfig config,
                     std::vector<std::unique_ptr<TraceSource>> traces)
    : Simulator(std::move(config), bind_dense_threads(std::move(traces))) {}

Simulator::Simulator(SimulatorConfig config,
                     std::vector<ThreadTraceBinding> threads)
    : impl_(std::make_unique<Impl>(std::move(config),
                                   std::move(threads))) {}

Simulator::~Simulator() = default;

SimulationStats Simulator::run() { return impl_->run(); }

std::vector<std::unique_ptr<TraceSource>> make_synthetic_traces(
    std::uint32_t cores, std::uint64_t instructions_per_core,
    std::uint32_t memory_percent, std::uint32_t shared_percent,
    std::uint64_t working_set_lines, std::uint64_t seed) {
    std::vector<std::unique_ptr<TraceSource>> traces;
    traces.reserve(cores);
    for (std::uint32_t core = 0; core < cores; ++core) {
        SyntheticTraceConfig config;
        config.core_id = core;
        config.instructions = instructions_per_core;
        config.memory_percent = memory_percent;
        config.shared_percent = shared_percent;
        config.working_set_lines = working_set_lines;
        config.seed = seed;
        traces.push_back(std::make_unique<SyntheticTraceSource>(config));
    }
    return traces;
}

}  // namespace fastsim
