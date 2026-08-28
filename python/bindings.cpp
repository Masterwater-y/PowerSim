#include <cstdint>
#include <memory>
#include <mutex>
#include <optional>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

#include "fastsim/config.hpp"
#include "fastsim/simulator.hpp"
#include "fastsim/trace.hpp"
#include "fastsim/types.hpp"

namespace py = pybind11;

namespace {

constexpr const char* kWindowResultSchema =
    "fastsim-window-result-v1";

py::dict cache_to_dict(const fastsim::CacheCounters& counters) {
    py::dict result;
    result["accesses"] = counters.accesses;
    result["hits"] = counters.hits;
    result["misses"] = counters.misses;
    result["evictions"] = counters.evictions;
    result["writebacks"] = counters.writebacks;
    return result;
}

py::dict core_to_dict(const fastsim::CoreWindowStats& core) {
    py::dict result;
    result["core"] = core.core;
    result["frequency_hz"] = core.frequency_hz;
    result["cycles"] = core.cycles;
    result["retired_instructions"] = core.retired_instructions;
    result["retired_uops"] = core.retired_uops;
    result["memory_uops"] = core.memory_uops;
    result["memory_accesses"] = core.memory_accesses;
    result["retired_branches"] = core.retired_branches;
    result["retired_branch_misses"] = core.retired_branch_misses;
    result["dtlb_accesses"] = core.dtlb_accesses;
    result["dtlb_misses"] = core.dtlb_misses;
    result["cpi"] = core.cpi_available
        ? py::cast(core.cpi)
        : py::none();
    result["uop_cpi"] = core.uop_cpi_available
        ? py::cast(core.uop_cpi)
        : py::none();
    result["l1d"] = cache_to_dict(core.l1d);
    result["l2"] = cache_to_dict(core.l2);
    return result;
}

py::dict shared_to_dict(const fastsim::SharedWindowStats& shared) {
    py::dict result;
    result["llc"] = cache_to_dict(shared.llc);
    result["cha_requests"] = shared.cha_requests;
    result["cha_reads"] = shared.cha_reads;
    result["cha_writes"] = shared.cha_writes;
    result["llc_hits"] = shared.llc_hits;
    result["llc_misses"] = shared.llc_misses;
    result["permission_upgrades"] = shared.permission_upgrades;
    result["invalidations"] = shared.invalidations;
    result["remote_supplies"] = shared.remote_supplies;
    result["llc_unique_fills"] = shared.llc_unique_fills;
    result["llc_merged_misses"] = shared.llc_merged_misses;
    result["llc_merged_wait_cycles"] =
        shared.llc_merged_wait_cycles;
    result["dram_reads"] = shared.dram_reads;
    result["dram_writes"] = shared.dram_writes;
    result["queue_cycles"] = shared.queue_cycles;
    return result;
}

py::dict window_to_dict(
    const fastsim::SimulationWindowResult& window) {
    py::dict result;
    result["schema"] = kWindowResultSchema;
    result["window_id"] = window.window_id;
    result["start_time_fs"] = window.start_time_fs;
    result["end_time_fs"] = window.end_time_fs;
    result["requested_value"] = window.requested_value;
    result["retired_instructions"] = window.retired_instructions;
    result["instruction_overshoot"] = window.instruction_overshoot;
    result["finished"] = window.finished;
    py::list cores;
    for (const auto& core : window.cores) {
        cores.append(core_to_dict(core));
    }
    result["cores"] = std::move(cores);
    result["shared"] = shared_to_dict(window.shared);
    return result;
}

class DvfsSession {
  public:
    DvfsSession(
        const std::string& config_path,
        const std::string& manifest_path,
        const std::optional<std::string>& measurement_scope,
        const std::optional<std::uint64_t>& reference_frequency_hz,
        const std::optional<std::vector<std::uint64_t>>&
            initial_core_frequencies_hz) {
        auto config = fastsim::load_simulator_config(config_path);
        if (measurement_scope.has_value()) {
            config.measurement_scope = fastsim::parse_measurement_scope(
                *measurement_scope);
        }
        if (config.measurement_scope ==
            fastsim::MeasurementScope::kUnspecified) {
            throw std::invalid_argument(
                "measurement_scope must be 'user' or "
                "'user-plus-kernel'");
        }
        if (reference_frequency_hz.has_value()) {
            config.reference_frequency_hz = *reference_frequency_hz;
        }
        if (initial_core_frequencies_hz.has_value()) {
            config.core_frequencies_hz = *initial_core_frequencies_hz;
        }
        config.validate();
        auto traces = fastsim::open_trace_manifest(
            manifest_path, config.cores);
        initialize(std::move(config), std::move(traces));
    }

    static std::unique_ptr<DvfsSession> synthetic(
        std::uint32_t cores,
        std::uint64_t instructions_per_core,
        std::uint32_t memory_percent,
        std::uint32_t shared_percent,
        std::uint64_t working_set_lines,
        std::uint64_t seed,
        std::uint64_t reference_frequency_hz,
        const std::optional<std::vector<std::uint64_t>>&
            initial_core_frequencies_hz) {
        fastsim::SimulatorConfig config;
        config.measurement_scope = fastsim::MeasurementScope::kUser;
        config.cores = cores;
        config.core_model = "interval_weave";
        config.interval_scheduler = "time_epoch";
        config.interval_full_order_audit = false;
        config.interval_same_line_order_audit = false;
        config.chunk_instructions = 64;
        config.interval_target_uops = 64;
        config.interval_max_cycles = 32;
        config.lookahead_chunks = 2;
        config.l1d.size_bytes = 4ull << 10;
        config.l2.size_bytes = 16ull << 10;
        config.llc.size_bytes = 64ull << 10;
        config.cha_count = 1;
        config.dram.channels = 1;
        config.dram.banks_per_channel = 2;
        config.reference_frequency_hz = reference_frequency_hz;
        config.core_frequency_hz = reference_frequency_hz;
        if (initial_core_frequencies_hz.has_value()) {
            config.core_frequencies_hz = *initial_core_frequencies_hz;
        }
        config.validate();

        auto traces = fastsim::make_synthetic_traces(
            cores, instructions_per_core, memory_percent,
            shared_percent, working_set_lines, seed);
        auto session = std::unique_ptr<DvfsSession>(new DvfsSession());
        session->initialize(std::move(config), std::move(traces));
        return session;
    }

    fastsim::SimulationWindowResult advance(
        const fastsim::SimulationWindow& window) {
        std::lock_guard<std::mutex> lock(mutex_);
        return simulator_->advance(window);
    }

    fastsim::SimulationWindowResult advance_time_ns(
        std::uint64_t nanoseconds) {
        return advance(
            fastsim::SimulationWindow::simulated_time_ns(nanoseconds));
    }

    fastsim::SimulationWindowResult advance_instructions(
        std::uint64_t instructions) {
        return advance(
            fastsim::SimulationWindow::retired_instructions(instructions));
    }

    void set_core_frequencies(
        const std::vector<std::uint64_t>& frequencies_hz) {
        std::lock_guard<std::mutex> lock(mutex_);
        simulator_->set_core_frequencies(frequencies_hz);
        current_core_frequencies_hz_ = frequencies_hz;
    }

    bool finished() const {
        std::lock_guard<std::mutex> lock(mutex_);
        return simulator_->finished();
    }

    std::uint32_t core_count() const { return core_count_; }

    std::uint64_t reference_frequency_hz() const {
        return reference_frequency_hz_;
    }

    const std::string& measurement_scope() const {
        return measurement_scope_;
    }

    std::vector<std::uint64_t> initial_core_frequencies_hz() const {
        return initial_core_frequencies_hz_;
    }

    std::vector<std::uint64_t> current_core_frequencies_hz() const {
        std::lock_guard<std::mutex> lock(mutex_);
        return current_core_frequencies_hz_;
    }

  private:
    DvfsSession() = default;

    void initialize(
        fastsim::SimulatorConfig config,
        std::vector<std::unique_ptr<fastsim::TraceSource>> traces) {
        core_count_ = config.cores;
        reference_frequency_hz_ = config.reference_frequency_hz;
        measurement_scope_ =
            fastsim::measurement_scope_name(config.measurement_scope);
        initial_core_frequencies_hz_.reserve(config.cores);
        for (std::uint32_t core = 0; core < config.cores; ++core) {
            initial_core_frequencies_hz_.push_back(
                config.frequency_hz(core));
        }
        current_core_frequencies_hz_ = initial_core_frequencies_hz_;
        simulator_ = std::make_unique<fastsim::Simulator>(
            std::move(config), std::move(traces));
    }

    mutable std::mutex mutex_;
    std::unique_ptr<fastsim::Simulator> simulator_;
    std::uint32_t core_count_ = 0;
    std::uint64_t reference_frequency_hz_ = 0;
    std::string measurement_scope_;
    std::vector<std::uint64_t> initial_core_frequencies_hz_;
    std::vector<std::uint64_t> current_core_frequencies_hz_;
};

py::object optional_cpi(
    double value, bool available) {
    return available ? py::cast(value) : py::none();
}

}  // namespace

PYBIND11_MODULE(fastsim_py, module) {
    module.doc() =
        "In-process Python control API for FastSim windowed DVFS";
    module.attr("__version__") = FASTSIM_PY_VERSION;
    module.attr("WINDOW_RESULT_SCHEMA") = kWindowResultSchema;

    py::enum_<fastsim::SimulationWindowKind>(
        module, "SimulationWindowKind")
        .value("SIMULATED_TIME",
               fastsim::SimulationWindowKind::kSimulatedTime)
        .value("RETIRED_INSTRUCTIONS",
               fastsim::SimulationWindowKind::kRetiredInstructions)
        .export_values();

    py::class_<fastsim::SimulationWindow>(module, "SimulationWindow")
        .def_readonly("kind", &fastsim::SimulationWindow::kind)
        .def_readonly("value", &fastsim::SimulationWindow::value)
        .def_static(
            "simulated_time_ns",
            &fastsim::SimulationWindow::simulated_time_ns,
            py::arg("nanoseconds"))
        .def_static(
            "retired_instructions",
            &fastsim::SimulationWindow::retired_instructions,
            py::arg("instructions"));

    py::class_<fastsim::CacheCounters>(module, "CacheCounters")
        .def_readonly("accesses", &fastsim::CacheCounters::accesses)
        .def_readonly("hits", &fastsim::CacheCounters::hits)
        .def_readonly("misses", &fastsim::CacheCounters::misses)
        .def_readonly("evictions", &fastsim::CacheCounters::evictions)
        .def_readonly("writebacks", &fastsim::CacheCounters::writebacks)
        .def("to_dict", &cache_to_dict);

    py::class_<fastsim::CoreWindowStats>(module, "CoreWindowStats")
        .def_readonly("core", &fastsim::CoreWindowStats::core)
        .def_readonly(
            "frequency_hz", &fastsim::CoreWindowStats::frequency_hz)
        .def_readonly("cycles", &fastsim::CoreWindowStats::cycles)
        .def_readonly(
            "retired_instructions",
            &fastsim::CoreWindowStats::retired_instructions)
        .def_readonly(
            "retired_uops", &fastsim::CoreWindowStats::retired_uops)
        .def_readonly(
            "memory_uops", &fastsim::CoreWindowStats::memory_uops)
        .def_readonly(
            "memory_accesses",
            &fastsim::CoreWindowStats::memory_accesses)
        .def_readonly(
            "retired_branches",
            &fastsim::CoreWindowStats::retired_branches)
        .def_readonly(
            "retired_branch_misses",
            &fastsim::CoreWindowStats::retired_branch_misses)
        .def_readonly(
            "dtlb_accesses", &fastsim::CoreWindowStats::dtlb_accesses)
        .def_readonly(
            "dtlb_misses", &fastsim::CoreWindowStats::dtlb_misses)
        .def_readonly(
            "cpi_available", &fastsim::CoreWindowStats::cpi_available)
        .def_readonly(
            "uop_cpi_available",
            &fastsim::CoreWindowStats::uop_cpi_available)
        .def_property_readonly(
            "cpi",
            [](const fastsim::CoreWindowStats& core) {
                return optional_cpi(core.cpi, core.cpi_available);
            })
        .def_property_readonly(
            "uop_cpi",
            [](const fastsim::CoreWindowStats& core) {
                return optional_cpi(
                    core.uop_cpi, core.uop_cpi_available);
            })
        .def_readonly("l1d", &fastsim::CoreWindowStats::l1d)
        .def_readonly("l2", &fastsim::CoreWindowStats::l2)
        .def("to_dict", &core_to_dict);

    py::class_<fastsim::SharedWindowStats>(module, "SharedWindowStats")
        .def_readonly("llc", &fastsim::SharedWindowStats::llc)
        .def_readonly(
            "cha_requests", &fastsim::SharedWindowStats::cha_requests)
        .def_readonly("cha_reads", &fastsim::SharedWindowStats::cha_reads)
        .def_readonly(
            "cha_writes", &fastsim::SharedWindowStats::cha_writes)
        .def_readonly("llc_hits", &fastsim::SharedWindowStats::llc_hits)
        .def_readonly(
            "llc_misses", &fastsim::SharedWindowStats::llc_misses)
        .def_readonly(
            "permission_upgrades",
            &fastsim::SharedWindowStats::permission_upgrades)
        .def_readonly(
            "invalidations", &fastsim::SharedWindowStats::invalidations)
        .def_readonly(
            "remote_supplies", &fastsim::SharedWindowStats::remote_supplies)
        .def_readonly(
            "llc_unique_fills",
            &fastsim::SharedWindowStats::llc_unique_fills)
        .def_readonly(
            "llc_merged_misses",
            &fastsim::SharedWindowStats::llc_merged_misses)
        .def_readonly(
            "llc_merged_wait_cycles",
            &fastsim::SharedWindowStats::llc_merged_wait_cycles)
        .def_readonly(
            "dram_reads", &fastsim::SharedWindowStats::dram_reads)
        .def_readonly(
            "dram_writes", &fastsim::SharedWindowStats::dram_writes)
        .def_readonly(
            "queue_cycles", &fastsim::SharedWindowStats::queue_cycles)
        .def("to_dict", &shared_to_dict);

    py::class_<fastsim::SimulationWindowResult>(
        module, "SimulationWindowResult")
        .def_readonly(
            "window_id", &fastsim::SimulationWindowResult::window_id)
        .def_readonly(
            "start_time_fs",
            &fastsim::SimulationWindowResult::start_time_fs)
        .def_readonly(
            "end_time_fs", &fastsim::SimulationWindowResult::end_time_fs)
        .def_readonly(
            "requested_value",
            &fastsim::SimulationWindowResult::requested_value)
        .def_readonly(
            "retired_instructions",
            &fastsim::SimulationWindowResult::retired_instructions)
        .def_readonly(
            "instruction_overshoot",
            &fastsim::SimulationWindowResult::instruction_overshoot)
        .def_readonly(
            "finished", &fastsim::SimulationWindowResult::finished)
        .def_readonly("cores", &fastsim::SimulationWindowResult::cores)
        .def_readonly("shared", &fastsim::SimulationWindowResult::shared)
        .def("to_dict", &window_to_dict);

    py::class_<DvfsSession>(module, "DvfsSession")
        .def(
            py::init<
                const std::string&, const std::string&,
                const std::optional<std::string>&,
                const std::optional<std::uint64_t>&,
                const std::optional<std::vector<std::uint64_t>>&>(),
            py::arg("config_path"),
            py::arg("manifest_path"),
            py::arg("measurement_scope") = std::nullopt,
            py::arg("reference_frequency_hz") = std::nullopt,
            py::arg("initial_core_frequencies_hz") = std::nullopt)
        .def_static(
            "synthetic", &DvfsSession::synthetic,
            py::arg("cores") = 1,
            py::arg("instructions_per_core") = 100000,
            py::arg("memory_percent") = 30,
            py::arg("shared_percent") = 5,
            py::arg("working_set_lines") = 1ull << 18,
            py::arg("seed") = 1,
            py::arg("reference_frequency_hz") = 3000000000ull,
            py::arg("initial_core_frequencies_hz") = std::nullopt)
        .def(
            "advance", &DvfsSession::advance,
            py::arg("window"),
            py::call_guard<py::gil_scoped_release>())
        .def(
            "advance_time_ns", &DvfsSession::advance_time_ns,
            py::arg("nanoseconds"),
            py::call_guard<py::gil_scoped_release>())
        .def(
            "advance_instructions", &DvfsSession::advance_instructions,
            py::arg("instructions"),
            py::call_guard<py::gil_scoped_release>())
        .def(
            "set_core_frequencies", &DvfsSession::set_core_frequencies,
            py::arg("frequencies_hz"),
            py::call_guard<py::gil_scoped_release>())
        .def(
            "finished", &DvfsSession::finished,
            py::call_guard<py::gil_scoped_release>())
        .def_property_readonly("core_count", &DvfsSession::core_count)
        .def_property_readonly(
            "reference_frequency_hz",
            &DvfsSession::reference_frequency_hz)
        .def_property_readonly(
            "measurement_scope", &DvfsSession::measurement_scope)
        .def_property_readonly(
            "initial_core_frequencies_hz",
            &DvfsSession::initial_core_frequencies_hz)
        .def_property_readonly(
            "current_core_frequencies_hz",
            &DvfsSession::current_core_frequencies_hz);
}
