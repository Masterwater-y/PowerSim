#pragma once
#include <array>
#include <cstdint>
#include <limits>
#include <stdexcept>

namespace fastsim {
inline constexpr std::uint64_t kServiceQ=65536;
struct ServiceExpectationCounts {
    std::uint64_t macro_loads=0,unknown_source_loads=0,clock_unavailable_loads=0;
    std::uint64_t correlation_uncertain_loads=0,fallback_loads=0;
    std::array<std::uint64_t,3> expected{},lower{},upper{};
    ServiceExpectationCounts& operator+=(const ServiceExpectationCounts& x){
        const auto add=[](std::uint64_t& a,std::uint64_t b){
            if(b>std::numeric_limits<std::uint64_t>::max()-a)throw std::overflow_error("service expectation overflow");a+=b;};
        add(macro_loads,x.macro_loads);add(unknown_source_loads,x.unknown_source_loads);
        add(clock_unavailable_loads,x.clock_unavailable_loads);
        add(correlation_uncertain_loads,x.correlation_uncertain_loads);add(fallback_loads,x.fallback_loads);
        for(unsigned l=0;l<3;++l){add(expected[l],x.expected[l]);add(lower[l],x.lower[l]);add(upper[l],x.upper[l]);}
        return *this;
    }
};
struct ServiceExpectationCore {
    // physical, user, kernel, workload_task, other_task
    std::array<ServiceExpectationCounts,5> scopes{};
    std::uint64_t observations=0,interval_joins=0,writes=0,warm_mixes=0,pollution_mixes=0;
    std::uint64_t future_intervals=0,reversed_clocks=0,history_replacements=0;
};
}
