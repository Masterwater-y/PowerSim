#include "fastsim/simulator.hpp"
#include <iostream>
#include <stdexcept>

int main() {
    const auto result=fastsim::testing::run_bounded_store_shared_transaction_probe();
    if(!result.before_completion_hidden || !result.shared_permission_published ||
       !result.cross_core_invalidated || !result.rollback_restored || !result.stale_generation_rejected)
        throw std::runtime_error("bounded store shared transaction probe failed");
    std::cout << "bounded store shared transaction passed\n";
}
