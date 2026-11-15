#pragma once
#include <algorithm>
#include <cstdint>
#include <limits>
#include <optional>

namespace fastsim {
// An I-fetch-only lower-bound certificate for the current IntervalCore model.
// It does not certify data/pagewalk/writeback events, core scheduling, cache
// state or a global prefetch-publication watermark. No target/native timing is
// an input. All times are local model cycles; conversion to a shared clock
// must round conservatively and include every other cache-state producer.
class SprRetiredIfetchFrontier {
 public:
    enum class Status {Unknown,Bounded,Finished};
    struct Tail {
        std::uint64_t unseen_floor=0;
        bool producer_finished=false;
    };
    struct Result {Status status=Status::Unknown;std::uint64_t cycle=0;};

    // Capture only AFTER a complete producer chunk has emitted all its
    // request descriptors. The source-aligned fetch-response ledger must
    // govern every future lower-hierarchy instruction fetch; retired-only
    // excludes physical speculative paths that bypass that ledger. A tail
    // belongs to one uninterrupted IntervalCore lifetime and is immutable.
    // The caller must invalidate it on source/model reset. It may be copied
    // with a chunk and safely retained across handoff and rollback.
    static std::optional<Tail> tail(bool source_aligned_fetch_ledger,
                                    bool retired_requests_only,
                                    bool has_last_response,
                                    std::uint64_t last_response,
                                    bool producer_finished) {
        if(!source_aligned_fetch_ledger || !retired_requests_only)return std::nullopt;
        if(producer_finished)return Tail{0,true};
        if(!has_last_response)return Tail{0,false};
        if(last_response==std::numeric_limits<std::uint64_t>::max())return std::nullopt;
        return Tail{last_response+1,false};
    }

    // `resident_min` is the caller-certified minimum ORIGINAL request cycle
    // of every generated but unconsumed I-fetch through this tail, including
    // queued producer chunks. resident_complete distinguishes a proven empty
    // suffix from missing evidence. The caller must bind the tail and suffix
    // to the same producer boundary and use the current nondecreasing gap.
    static Result combine(const Tail& tail,bool resident_complete,
                          std::optional<std::uint64_t> resident_min,
                          std::uint64_t nondecreasing_gap) {
        if(!resident_complete)return {};
        if(tail.producer_finished && !resident_min)return {Status::Finished,0};
        const auto local=tail.producer_finished ? *resident_min :
            resident_min ? std::min(*resident_min,tail.unseen_floor) : tail.unseen_floor;
        if(nondecreasing_gap>std::numeric_limits<std::uint64_t>::max()-local)return {};
        return {Status::Bounded,local+nondecreasing_gap};
    }
};
}
