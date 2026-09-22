#include "arch/x86/qemu_fst_transfer.hh"

#include <chrono>
#include <cinttypes>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <optional>
#include <sstream>
#include <string>

#include "arch/x86/qemu_record_reader.hh"
#include "base/logging.hh"

namespace gem5
{
namespace X86ISA
{
X86QemuFstTransfer::X86QemuFstTransfer(const Params &params)
    : SimObject(params),
      inputShard(params.input_shard),
      outputDir(params.output_dir),
      expectedNumCores(params.expected_num_cores),
      userMacroTarget(params.user_macro_target),
      triggerCore(params.trigger_core),
      workerCoreIndex(params.worker_core_index),
      cpus(params.cpus)
{
    fatal_if(userMacroTarget == 0,
             "X86QemuFstTransfer requires a positive user macro target");
    fatal_if(!std::filesystem::is_directory(outputDir),
             "QEMU-FST output directory does not exist: %s", outputDir);
    fatal_if(expectedNumCores < 2 || expectedNumCores > 32,
             "X86QemuFstTransfer requires 2..32 logical cores");
    fatal_if(workerCoreIndex < 0 ||
                 static_cast<uint32_t>(workerCoreIndex) >= expectedNumCores,
             "QEMU-FST worker core index %d is outside [0,%u)",
             workerCoreIndex, expectedNumCores);
    fatal_if(triggerCore >= expectedNumCores,
             "QEMU-FST trigger core=%u is outside [0,%u)",
             triggerCore, expectedNumCores);
    fatal_if(cpus.size() != 1,
             "QEMU-FST worker requires exactly one lowering CPU");
    projectors.emplace_back(
        std::make_unique<FstProjector>(
            static_cast<uint32_t>(workerCoreIndex), outputDir));
}

void
X86QemuFstTransfer::convert()
{
    convertWorker(static_cast<uint32_t>(workerCoreIndex));
}

void
X86QemuFstTransfer::convertWorker(uint32_t core)
{
    // Each worker lowers exactly one raw shard: the Python driver reads every
    // shard's CPU_ID once and hands this worker the shard for its logical core.
    // The reader re-validates the shard's file header against `core`, so it
    // remains the authoritative owner of the shard/core contract.
    QemuRecordReader reader(inputShard);
    auto &projector = *projectors.at(0);
    uint64_t measurementUserMacros = 0;
    const bool profile = std::getenv("QEMU_FST_PROFILE") != nullptr;
    std::chrono::steady_clock::duration assembleTime{};
    std::chrono::steady_clock::duration lowerTime{};
    CommitRecord frame;
    std::chrono::steady_clock::time_point started;
    if (profile) {
        started = std::chrono::steady_clock::now();
    }
    bool ready = reader.next(frame);
    if (profile) {
        assembleTime += std::chrono::steady_clock::now() - started;
    }
    bool coreVerified = false;
    while (ready) {
        if (!coreVerified && reader.coreIdKnown()) {
            fatal_if(reader.coreId() != core,
                     "QEMU-FST worker core=%u received shard for core=%u: %s",
                     core, reader.coreId(), inputShard.c_str());
            coreVerified = true;
        }
        if (frame.kind == CommitKind::Macro &&
            frame.phase == QemuFramePhase::Measurement &&
            frame.userMode()) {
            ++measurementUserMacros;
        }
        if (profile) {
            started = std::chrono::steady_clock::now();
        }
        cpus.at(0)->lower(frame, projector);
        if (profile) {
            lowerTime += std::chrono::steady_clock::now() - started;
        }
        if (profile) {
            started = std::chrono::steady_clock::now();
        }
        ready = reader.next(frame);
        if (profile) {
            assembleTime += std::chrono::steady_clock::now() - started;
        }
    }
    fatal_if(!coreVerified,
             "QEMU-FST worker found no logical core in shard: %s",
             inputShard.c_str());
    if (profile) {
        started = std::chrono::steady_clock::now();
    }
    projector.close();
    if (profile) {
        const auto closeTime = std::chrono::steady_clock::now() - started;
        const auto seconds = [](const auto duration) {
            return std::chrono::duration<double>(duration).count();
        };
        std::cerr << "QEMU-FST profile core=" << core
                  << " assemble=" << seconds(assembleTime)
                  << " lower=" << seconds(lowerTime)
                  << " close=" << seconds(closeTime) << '\n';
    }
    writeWorkerBoundaries(core, measurementUserMacros);
}

std::string
X86QemuFstTransfer::workerBoundaryJson(
    uint32_t core, uint64_t measurementUserMacros) const
{
    const auto &counts = projectors.at(0)->counts();
    std::ostringstream output;
    output << "  \"" << core << "\": {\n"
           << "    \"warmup_instructions\": "
           << counts.warmupInstructions << ",\n"
           << "    \"warmup_records\": " << counts.warmupRecords << ",\n"
           << "    \"warmup_user_instructions\": "
           << counts.warmupUserInstructions << ",\n"
           << "    \"warmup_user_records\": "
           << counts.warmupUserRecords << ",\n"
           << "    \"warmup_mmio_records\": "
           << counts.warmupMmioRecords << ",\n"
           << "    \"warmup_pio_records\": "
           << counts.warmupPioRecords << ",\n"
           << "    \"measurement_instructions\": "
           << counts.measurementInstructions << ",\n"
           << "    \"measurement_records\": "
           << counts.measurementRecords << ",\n"
           << "    \"measurement_user_instructions\": "
           << counts.measurementUserInstructions << ",\n"
           << "    \"measurement_user_records\": "
           << counts.measurementUserRecords << ",\n"
           << "    \"measurement_user_macros\": "
           << measurementUserMacros << ",\n"
           << "    \"measurement_mmio_records\": "
           << counts.measurementMmioRecords << ",\n"
           << "    \"measurement_pio_records\": "
           << counts.measurementPioRecords << ",\n"
           << "    \"frame_ram_bytes\": "
           << counts.frameRamBytes << ",\n"
           << "    \"target_user_macros\": " << userMacroTarget << ",\n"
           << "    \"target_reached\": "
           << (core == triggerCore ? "true" : "false") << ",\n"
           << "    \"trigger_core\": " << triggerCore << ",\n"
           << "    \"participant_count\": " << expectedNumCores << ",\n"
           << "    \"measurement_policy\": "
              "\"first-core-macro-common-end\",\n"
           << "    \"stop_observed\": true,\n"
           << "    \"stop_reason\": "
              "\"first-core-user-macro-target\",\n"
           << "    \"trace_scope\": \"user-plus-kernel\"\n"
           << "  }";
    return output.str();
}

void
X86QemuFstTransfer::writeWorkerBoundaries(
    uint32_t core, uint64_t measurementUserMacros) const
{
    const std::string path =
        outputDir + "/core" + std::to_string(core) + ".boundaries.json";
    std::ofstream output(path);
    fatal_if(!output,
             "failed to open QEMU-FST worker boundaries: %s", path);
    output << "{\n"
           << workerBoundaryJson(core, measurementUserMacros)
           << "\n}\n";
    fatal_if(!output,
             "failed to write QEMU-FST worker boundaries: %s", path);
}

} // namespace X86ISA
} // namespace gem5
