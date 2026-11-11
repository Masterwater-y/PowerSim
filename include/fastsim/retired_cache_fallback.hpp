#pragma once
namespace fastsim {
enum RetiredCacheFallback : unsigned {
    kRetiredFallbackMissingSource = 1u << 0,
    kRetiredFallbackUnknownPrivate = 1u << 1,
    kRetiredFallbackUnknownShared = 1u << 2,
    kRetiredFallbackMissingSharedClock = 1u << 3,
    kRetiredFallbackFutureParent = 1u << 4,
    kRetiredFallbackInvalidInterval = 1u << 5,
    kRetiredFallbackCollision = 1u << 6,
    kRetiredFallbackUnclassified = 1u << 7
};
}
