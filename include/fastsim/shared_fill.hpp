#pragma once

#include <cstdint>
#include <optional>

namespace fastsim {

// A fill's identity survives queue retiming. Its completion time is not an
// identity: two generations of the same line may have the same completion.
// Absence of ready means the service owns the fill but has not been selected.
// It is not visible data and must never enter a numeric expiry calendar.
struct SharedFill {
    std::optional<std::uint64_t> ready;
    std::uint64_t generation = 0;
    std::uint64_t lower_bound = 0;
};

enum class FillServiceKind { kOther, kHit, kMerge, kMiss };
enum class FillServiceValidity {
    kValid,
    kMissingParent,
    kReplacedParent,
    kVisibilityChanged,
    kPendingParent,
};

// Canonical cache/replacement effects may be retained only while the replay
// stays on the same side of the fill boundary. Equal-time completion makes
// the data visible before lookup. This certifies the fill relation only;
// callers must separately preserve cache/set and coherence order.
inline FillServiceValidity validate_fill_service(
    FillServiceKind kind, std::uint64_t generation,
    std::uint64_t tag_ready, const SharedFill* fill) {
    if (kind == FillServiceKind::kMerge) {
        if (fill == nullptr) return FillServiceValidity::kMissingParent;
        if (generation == 0 || fill->generation != generation) {
            return FillServiceValidity::kReplacedParent;
        }
        if (!fill->ready) return FillServiceValidity::kPendingParent;
        return tag_ready < *fill->ready
            ? FillServiceValidity::kValid
            : FillServiceValidity::kVisibilityChanged;
    }
    // A carried unique request can already own the functional lookahead
    // entry; replay schedules that same generation, not a second demand.
    if (kind == FillServiceKind::kMiss && fill != nullptr &&
        generation != 0 && generation == fill->generation) {
        return fill->ready ? FillServiceValidity::kValid
                           : FillServiceValidity::kPendingParent;
    }
    if ((kind == FillServiceKind::kHit || kind == FillServiceKind::kMiss) &&
        fill != nullptr && (!fill->ready || tag_ready < *fill->ready)) {
        return FillServiceValidity::kVisibilityChanged;
    }
    return FillServiceValidity::kValid;
}

}  // namespace fastsim
