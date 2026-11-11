#pragma once
#include <atomic>
#include <cstdint>
#include <limits>
#include <memory>
#include <stdexcept>
namespace fastsim {
// Copies represent snapshots of one resource owner and deliberately share an
// ID source. Rollback must never make a stale asynchronous ticket valid again.
class SprTransactionId {
 public:
    SprTransactionId():next_(std::make_shared<std::atomic<std::uint64_t>>(1)) {}
    std::uint64_t allocate() {
        auto value=next_->load(std::memory_order_relaxed);
        do {
            if(value==std::numeric_limits<std::uint64_t>::max())
                throw std::overflow_error("transaction id overflow");
        } while(!next_->compare_exchange_weak(value,value+1,
                    std::memory_order_relaxed,std::memory_order_relaxed));
        return value;
    }
 private:
    std::shared_ptr<std::atomic<std::uint64_t>> next_;
};
}
