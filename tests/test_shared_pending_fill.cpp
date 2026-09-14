#include "fastsim/shared_fill.hpp"
#include "fastsim/simulator.hpp"

#include <optional>
#include <stdexcept>

namespace {
void require(bool condition, const char* message) {
    if (!condition) throw std::runtime_error(message);
}
}

void test_shared_pending_fill() {
    using namespace fastsim;
    // A hit/merge cannot turn an unselected parent into numerical readiness.
    // Mutation caught: testing `tag_ready < ready.value_or(0)` accepts the hit.
    SharedFill fill{std::nullopt, 41};
    require(!fill.ready.has_value(), "unselected fill must stay explicitly pending");
    require(validate_fill_service(FillServiceKind::kHit, 0, 99, &fill) ==
                FillServiceValidity::kVisibilityChanged,
            "pending fill was treated as visible data");
    require(validate_fill_service(FillServiceKind::kMerge, 41, 99, &fill) ==
                FillServiceValidity::kPendingParent,
            "numeric timing replay must not accept an unselected parent");
    require(validate_fill_service(FillServiceKind::kMerge, 42, 99, &fill) ==
                FillServiceValidity::kReplacedParent,
            "pending parent identity must be checked before readiness");
    require(validate_fill_service(FillServiceKind::kMiss, 42, 99, &fill) ==
                FillServiceValidity::kVisibilityChanged,
            "new miss cannot replace the pending generation");
    require(validate_fill_service(FillServiceKind::kMiss, 41, 99, &fill) ==
                FillServiceValidity::kPendingParent,
            "numeric replay cannot reschedule its already submitted pending parent");
    auto snapshot = fill;
    fill.ready = 130;
    require(!snapshot.ready.has_value(), "pending fill snapshots must be independent");
    require(validate_fill_service(FillServiceKind::kMerge, 41, 129, &fill) ==
                FillServiceValidity::kValid,
            "selected response after frontier must remain usable");
    require(validate_fill_service(FillServiceKind::kMerge, 41, 130, &fill) ==
                FillServiceValidity::kVisibilityChanged,
            "equal-time completion must precede the next lookup");
    require(validate_fill_service(FillServiceKind::kHit, 0, 130, &fill) ==
                FillServiceValidity::kValid,
            "resolved fill must be visible at completion equality");
#ifdef FASTSIM_ENABLE_TEST_HOOKS
    // Uses the actual shared hierarchy, transaction and timing-replay path.
    // This is a hierarchy-boundary test, not an end-to-end core coverage claim.
    const auto state = testing::run_shared_pending_fill_probe();
    require(state.generation != 0 && !state.pending_ready && !state.restored_ready,
            "shared transaction lost an owned unselected fill");
    require(state.numeric_access_rejected && state.numeric_replay_pending,
            "legacy shared consumers fabricated pending response time");
    require(state.requests_after_rejected_access == 0 && state.requests_after_restore == 0,
            "pending access or rolled-back response leaked shared PMU");
    require(state.selected_ready == 200 && state.selected_merge_response == 205,
            "selected parent did not publish its exact fill/NoC response");
    require(!state.stale_publication && !state.duplicate_publication &&
                state.conflicting_publication_rejected,
            "stale/duplicate callback changed the active shared fill");
#endif
}

#ifdef FASTSIM_SHARED_PENDING_FILL_STANDALONE
#include <iostream>
int main() {
    try {
        test_shared_pending_fill();
        std::cout << "shared pending fill tests passed\n";
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return 1;
    }
}
#endif
