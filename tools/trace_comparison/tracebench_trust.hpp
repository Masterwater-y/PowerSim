#ifndef TRACEBENCH_TRUST_HPP
#define TRACEBENCH_TRUST_HPP
#include <cstdlib>
#include <cstring>
#include <stdexcept>
namespace tracebench {
// Only the isolated benchmark builds use this switch. Production is untouched.
inline bool validation_enabled() {
    static const bool enabled = [] {
        const char *mode = std::getenv("TRACEBENCH_VALIDATED_INPUT");
        if (!mode || std::strcmp(mode, "1")) return true;
        const char *proof = std::getenv("TRACEBENCH_RECEIPT_SHA256");
        if (!proof || std::strlen(proof) != 64)
            throw std::runtime_error("validated benchmark input requires external receipt");
        for (const char *p = proof; *p; ++p)
            if (!((*p >= '0' && *p <= '9') || (*p >= 'a' && *p <= 'f')))
                throw std::runtime_error("invalid benchmark receipt identity");
        return false;
    }();
    return enabled;
}
}
#endif
