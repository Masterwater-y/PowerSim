#include <cstdint>
#include <filesystem>
#include <fstream>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

#include <unistd.h>

#include "fastsim/config.hpp"
#include "fastsim/mixed_dram.hpp"

namespace {

void require(bool condition, const std::string& message) {
    if (!condition) throw std::runtime_error(message);
}

class TemporaryConfig {
  public:
    explicit TemporaryConfig(const std::string& overlay) {
        static std::uint64_t serial = 0;
        path_ = std::filesystem::temp_directory_path() /
            ("fastsim-cross-q-config-" + std::to_string(::getpid()) + "-" +
             std::to_string(serial++) + ".cfg");
        std::ofstream output(path_);
        if (!output) {
            throw std::runtime_error("cannot create temporary cross-Q config");
        }
        output << "config.include = "
               << (std::filesystem::path(FASTSIM_PROJECT_ROOT) /
                   "configs/gem5-fs-native-kernel.cfg")
                      .string()
               << '\n'
               << overlay;
        if (!output) {
            throw std::runtime_error("cannot write temporary cross-Q config");
        }
    }

    ~TemporaryConfig() {
        std::error_code ignored;
        std::filesystem::remove(path_, ignored);
    }

    TemporaryConfig(const TemporaryConfig&) = delete;
    TemporaryConfig& operator=(const TemporaryConfig&) = delete;

    fastsim::SimulatorConfig load() const {
        return fastsim::load_simulator_config(path_.string());
    }

  private:
    std::filesystem::path path_;
};

fastsim::SimulatorConfig load_and_validate(const std::string& overlay) {
    TemporaryConfig config_file(overlay);
    auto config = config_file.load();
    config.validate();
    return config;
}

void require_cross_q_rejection(const std::string& overlay,
                               const std::string& case_name) {
    try {
        (void)load_and_validate(overlay);
    } catch (const std::invalid_argument& error) {
        require(std::string(error.what()).find("core.cross_q_service_mode") !=
                    std::string::npos,
                case_name + " must be rejected by the cross-Q contract, got: " +
                    error.what());
        return;
    }
    throw std::runtime_error(case_name + " must be rejected");
}

}  // namespace

void test_cross_q_config() {
    // These write-direction fields previously disappeared in parsing, leaving
    // the mixed controller's unrelated 22-cycle fixture defaults in force.
    for (const auto* field : {"t_cwl", "t_rcd_wr", "t_ccd_l_wr", "t_rtw",
                              "t_wtr", "t_wtr_l", "t_wr"}) {
        bool rejected = false;
        try {
            (void)load_and_validate(std::string("dram.") + field + " = 1048577\n");
        } catch (const std::invalid_argument&) {
            rejected = true;
        }
        require(rejected, std::string("DRAM write timing must not be ignored: ") + field);
    }
    const std::vector<std::string> supported_modes = {
        "off", "controller", "admission", "combined"};
    for (const auto& mode : supported_modes) {
        const auto config = load_and_validate(
            "core.cross_q_service_mode = " + mode + "\n");
        require(config.cross_q_service_mode == mode,
                "real config parser must retain supported cross-Q mode " + mode);
        require(config.ordinary_load_latency == 3 &&
                    config.load_response_to_ready == 1,
                "cross-Q mode must not change maintained load timing");
    }

    const auto inherited = load_and_validate(
        "core.cross_q_service_mode = combined\n");
    const auto mixed = fastsim::make_mixed_dram_config(inherited.dram);
    require(mixed.t_cwl == inherited.dram.t_cl &&
                mixed.t_rcd_wr == inherited.dram.t_rcd &&
                mixed.dram.burst_cycles == inherited.dram.burst_cycles,
            "production mixed controller must preserve profile timing units");
    const auto explicit_write = load_and_validate(
        "dram.t_cwl = 41\ndram.t_rcd_wr = 42\ndram.t_ccd_l_wr = 16\n"
        "dram.t_rtw = 6\ndram.t_wtr = 15\ndram.t_wtr_l = 17\ndram.t_wr = 46\n");
    const auto explicit_mixed = fastsim::make_mixed_dram_config(explicit_write.dram);
    require(explicit_mixed.t_cwl == 41 && explicit_mixed.t_rcd_wr == 42 &&
                explicit_mixed.t_ccd_l_wr == 16 && explicit_mixed.t_rtw == 6 &&
                explicit_mixed.t_wtr == 15 && explicit_mixed.t_wtr_l == 17 &&
                explicit_mixed.t_wr == 46,
            "parsed write timing must reach the actual mixed-controller config");
    require(inherited.core_model == "interval_weave" &&
                inherited.interval_scheduler == "time_epoch" &&
                inherited.response_queue_feedback &&
                inherited.response_sparse_scoreboard && inherited.needs_tso &&
                inherited.memory_exposure == 1.0 &&
                inherited.interval_parallel_feedback &&
                inherited.response_materialized_uop_fast_kernel &&
                inherited.native_kernel_trace,
            "maintained profile must support cross-Q without disabling allowed "
            "parallel, materialized-kernel, or native-trace behavior");

    const auto identity_per_core = load_and_validate(
        "core.cross_q_service_mode = admission\n"
        "core.frequencies_hz = 3000000000,3000000000,3000000000,3000000000\n");
    require(identity_per_core.core_frequencies_hz.size() == 4,
            "identity per-core frequencies must remain supported");

    TemporaryConfig old_profile_file("");
    auto old_profile = old_profile_file.load();
    old_profile.validate();
    require(old_profile.cross_q_service_mode == "off" &&
                old_profile.ordinary_load_latency == 3 &&
                old_profile.load_response_to_ready == 1,
            "profiles without the new key must remain valid and default off");

    require_cross_q_rejection(
        "core.cross_q_service_mode = speculative\n", "unknown mode");

    const std::vector<std::pair<std::string, std::string>>
        missing_requirements = {
        {"core model", "core.model = scalar\n"},
        {"interval scheduler", "sim.interval_scheduler = frontier\n"},
        {"queue feedback", "core.response_queue_feedback = false\n"},
        {"sparse scoreboard", "core.response_sparse_scoreboard = false\n"},
        {"TSO", "core.needs_tso = false\n"},
        {"full memory exposure", "core.memory_exposure = 0.5\n"},
    };
    for (const auto& [name, setting] : missing_requirements) {
        require_cross_q_rejection(
            "core.cross_q_service_mode = controller\n" + setting,
            "cross-Q without required " + name);
    }

    const std::vector<std::pair<std::string, std::string>> incompatible = {
        {"event-only approximation",
         "core.response_event_only_approximation = true\n"},
        {"causal block transfer",
         "core.response_causal_block_transfer = true\n"},
        {"pending fill", "core.response_pending_fill = true\n"},
        {"private read services",
         "core.response_private_read_services = true\n"},
        {"shared service constraints",
         "core.response_shared_service_constraints = true\n"},
        {"post-commit store request",
         "core.store_post_commit_request = true\n"},
        {"causal timing", "sim.interval_causal_timing = true\n"},
        {"response retime", "sim.interval_response_retime = true\n"},
        {"corrected suffix carry",
         "sim.interval_corrected_suffix_carry = true\n"},
        {"sequencer line coalescing",
         "ruby.sequencer_line_coalescing = true\n"},
        {"sequencer load admission",
         "ruby.sequencer_load_admission = true\n"},
        {"DTLB hierarchy walk", "dtlb.hierarchy_walk = true\n"},
    };
    for (const auto& [name, setting] : incompatible) {
        require_cross_q_rejection(
            "core.cross_q_service_mode = combined\n" + setting,
            "cross-Q with incompatible " + name);
    }

    require_cross_q_rejection(
        "core.cross_q_service_mode = admission\n"
        "core.frequency_hz = 2400000000\n",
        "cross-Q with scalar frequency override");
    require_cross_q_rejection(
        "core.cross_q_service_mode = admission\n"
        "core.frequencies_hz = 3000000000,3000000000,2400000000,3000000000\n",
        "cross-Q with per-core DVFS override");
    require_cross_q_rejection(
        "core.cross_q_service_mode = admission\n"
        "sim.reference_frequency_hz = 2400000000\n",
        "cross-Q with reference frequency override");
}

#ifdef FASTSIM_CROSS_Q_CONFIG_STANDALONE
#include <iostream>

int main() {
    try {
        test_cross_q_config();
        std::cout << "cross-Q config tests passed\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "cross-Q config test failure: " << error.what() << '\n';
        return 1;
    }
}
#endif
