#pragma once
#include "fastsim/bounded_store_timing.hpp"
#include <array>
#include <cstdint>

namespace fastsim {
// Same nonnegative, ten-bin doubling geometry as gem5 Histogram(10).
// Sums are exact; no mean is reconstructed from bucket midpoints.
struct StoreLatencyHistogram {
    std::array<std::uint64_t,10> buckets{};
    std::uint64_t bucket_size=1, samples=0, sum=0;
    void grow() {
        for(std::size_t i=0;i<5;++i) buckets[i]=buckets[2*i]+buckets[2*i+1];
        for(std::size_t i=5;i<10;++i) buckets[i]=0;
        bucket_size*=2;
    }
    void sample(std::uint64_t cycles) {
        while(cycles/bucket_size>=10) grow();
        ++buckets[cycles/bucket_size];++samples;sum+=cycles;
    }
    void merge(StoreLatencyHistogram other) {
        while(bucket_size<other.bucket_size) grow();
        while(other.bucket_size<bucket_size) other.grow();
        for(std::size_t i=0;i<10;++i) buckets[i]+=other.buckets[i];
        samples+=other.samples;sum+=other.sum;
    }
};

// All fields observe the final bounded CPU demand; no callbacks into timing.
// Private-level order is HitLevel L1/L2/LLC/Remote/Memory/Unknown.
// duration[0/1] means <=5/>5 cycles, NOT gem5 hit/miss.
struct StoreDemandObservation {
    StoreLatencyHistogram demand;
    std::array<StoreLatencyHistogram,4> role{};
    std::array<StoreLatencyHistogram,6> private_level{};
    std::array<StoreLatencyHistogram,2> duration{};
    std::array<std::uint64_t,6> service_cycles{},shared_queue_cycles{},memory_queue_cycles{},dram_service_cycles{};
    void sample(const BoundedNonbindingStoreResult& result,std::size_t level,
                std::uint64_t service,std::uint64_t shared_queue,
                std::uint64_t memory_queue,std::uint64_t dram_service) {
        const auto wait=result.response-result.send;
        demand.sample(wait);role[static_cast<std::size_t>(result.role)].sample(wait);
        private_level[level].sample(wait);duration[wait<=5?0:1].sample(wait);
        service_cycles[level]+=service;shared_queue_cycles[level]+=shared_queue;
        memory_queue_cycles[level]+=memory_queue;dram_service_cycles[level]+=dram_service;
    }
    void merge(const StoreDemandObservation& other) {
        demand.merge(other.demand);
        for(std::size_t i=0;i<4;++i) role[i].merge(other.role[i]);
        for(std::size_t i=0;i<2;++i) duration[i].merge(other.duration[i]);
        for(std::size_t i=0;i<6;++i) {
            private_level[i].merge(other.private_level[i]);
            service_cycles[i]+=other.service_cycles[i];shared_queue_cycles[i]+=other.shared_queue_cycles[i];
            memory_queue_cycles[i]+=other.memory_queue_cycles[i];dram_service_cycles[i]+=other.dram_service_cycles[i];
        }
    }
};
}
