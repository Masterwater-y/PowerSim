#pragma once
#include "fastsim/pending_resources.hpp"
#include <cstddef>
#include <cstdint>
#include <memory>
#include <optional>
#include <vector>

namespace fastsim::spr {
struct RubyPrefetchProfile {
    std::uint32_t block_size=64, page_shift=12;
    std::uint32_t num_streams=4, train_misses=4, num_startup_pfs=1;
    std::uint32_t unit_filter=8, nonunit_filter=8;
    bool ip_stride=false, cross_page=false, queue_dedup=true;
};
// Functional request facts only. Outcomes passed to observe_* must come from
// the model's controller transition; this interface never consumes native timing.
struct PrefetchAccess {
    bool complete=false;
    std::optional<std::uint64_t> physical, virtual_address, pc;
    std::int32_t context=-1;
    bool uncacheable=false, strictly_ordered=false, instruction=false;
    bool prefetch=false, page_walk=false, secure=false;
    PendingOwner trigger{};
};
enum class PrefetchOrigin { Stream, IpStride, Adjacent };
struct PrefetchCandidate {
    std::uint64_t address=0;
    PrefetchOrigin origin=PrefetchOrigin::Stream;
    PendingOwner trigger{};
};
struct RubyPrefetchStats {
    std::uint64_t misses_observed=0, streams_allocated=0, requested=0;
    std::uint64_t duplicate_candidates=0, stream_candidates=0, ip_candidates=0;
    std::uint64_t ip_page_stops=0, adjacent_candidates=0;
    std::uint64_t hits=0, partial_hits=0, pages_crossed=0;
};
// Candidate generation and live queue dedup only. Cache/fill/TBE admission,
// prefetch bits and pollution belong to the controller adapter, not this class.
class RubyPrefetcher {
public:
    explicit RubyPrefetcher(RubyPrefetchProfile);
    ~RubyPrefetcher();
    RubyPrefetcher(RubyPrefetcher&&) noexcept;
    RubyPrefetcher& operator=(RubyPrefetcher&&) noexcept;
    RubyPrefetcher(const RubyPrefetcher&)=delete;
    RubyPrefetcher& operator=(const RubyPrefetcher&)=delete;
    std::vector<PrefetchCandidate> observe_data_access(const PrefetchAccess&,std::uint64_t cycle);
    std::vector<PrefetchCandidate> observe_data_miss(const PrefetchAccess&,std::uint64_t cycle);
    std::vector<PrefetchCandidate> observe_miss(std::uint64_t physical,std::uint64_t cycle,PendingOwner trigger={});
    std::vector<PrefetchCandidate> observe_adjacent(std::uint64_t physical,std::uint64_t cycle,PendingOwner trigger={});
    std::vector<PrefetchCandidate> observe_prefetch_hit(std::uint64_t physical,std::uint64_t cycle,PendingOwner trigger={});
    std::vector<PrefetchCandidate> observe_prefetch_miss(std::uint64_t physical,std::uint64_t cycle,PendingOwner trigger={});
    void dequeued(std::uint64_t physical);
    const RubyPrefetchStats& stats() const;
    std::size_t queued_lines() const;
    std::size_t live_streams() const;
private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};
} // namespace fastsim::spr
