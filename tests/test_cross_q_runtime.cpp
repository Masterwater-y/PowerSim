#include "fastsim/simulator.hpp"

#include <memory>
#include <stdexcept>
#include <string>
#include <vector>

namespace {
class EmptyTrace final : public fastsim::TraceSource {
  public:
    bool next(fastsim::TraceRecord&) override { return false; }
    std::string description() const override { return "cross-q-runtime-gate"; }
};
}

// Until retained core dispatch/retirement is wired to pending services, accepting
// this mode would silently report legacy results as a cross-Q experiment.
void test_cross_q_runtime() {
    auto c = fastsim::load_simulator_config(
        std::string(FASTSIM_PROJECT_ROOT) + "/configs/gem5-fs-native-kernel.cfg");
    c.cores = 2;
    for (const auto* mode : {"controller", "admission", "combined"}) {
        c.cross_q_service_mode = mode;
        c.validate();
        std::vector<std::unique_ptr<fastsim::TraceSource>> traces;
        traces.push_back(std::make_unique<EmptyTrace>());
        traces.push_back(std::make_unique<EmptyTrace>());
        bool rejected = false;
        try {
            fastsim::Simulator simulator(c, std::move(traces));
        } catch (const std::invalid_argument& error) {
            rejected = std::string(error.what()).find(
                "cross-Q core continuation is not integrated") != std::string::npos;
        }
        if (!rejected) throw std::runtime_error(
            std::string("cross-Q mode silently ran the legacy engine: ") + mode);
    }
}

#ifdef FASTSIM_CROSS_Q_RUNTIME_STANDALONE
#include <iostream>
int main() {
    try {
        test_cross_q_runtime();
        std::cout << "cross-Q runtime gate tests passed\n";
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return 1;
    }
}
#endif
