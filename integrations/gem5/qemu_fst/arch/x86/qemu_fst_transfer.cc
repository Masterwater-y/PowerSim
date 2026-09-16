#include "arch/x86/qemu_fst_transfer.hh"

#include <cinttypes>
#include <filesystem>
#include <fstream>
#include <optional>
#include <sstream>
#include <string>

#include "arch/x86/qemu_raw_trace_reader.hh"
#include "arch/x86/qemu_transaction_assembler.hh"
#include "base/logging.hh"

namespace gem5
{
namespace X86ISA
{
X86QemuFstTransfer::X86QemuFstTransfer(const Params &params)
    : SimObject(params),
      inputTrace(params.input_trace),
      inputShard(params.input_shard),
      outputDir(params.output_dir),
      expectedNumCores(params.expected_num_cores),
      userFstTarget(params.user_fst_target),
      workerCoreIndex(params.worker_core_index),
      cpus(params.cpus)
{
    fatal_if(userFstTarget == 0,
             "X86QemuFstTransfer requires a positive user UOP target");
    fatal_if(!std::filesystem::is_directory(outputDir),
             "QEMU-FST output directory does not exist: %s", outputDir);
    fatal_if(expectedNumCores < 2 || expectedNumCores > 32,
             "X86QemuFstTransfer requires 2..32 logical cores");
    fatal_if(workerCoreIndex < 0 ||
                 static_cast<uint32_t>(workerCoreIndex) >= expectedNumCores,
             "QEMU-FST worker core index %d is outside [0,%u)",
             workerCoreIndex, expectedNumCores);
    fatal_if(cpus.size() != 1,
             "QEMU-FST worker requires exactly one lowering CPU");
    projectors.emplace_back(
        std::make_unique<FstProjector>(
            static_cast<uint32_t>(workerCoreIndex), outputDir,
            userFstTarget));
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
    // The assembler still re-validates the shard's CPU_ID against `core`, so it
    // remains the authoritative owner of the shard/core contract.
    QemuTransactionAssembler assembler(inputShard);
    auto &projector = *projectors.at(0);
    uint64_t rawMeasurementUserMacros = 0;
    QemuRawItem item;
    bool ready = assembler.next(item);
    bool coreVerified = false;
    while (ready) {
        if (!coreVerified && assembler.coreIdKnown()) {
            fatal_if(assembler.coreId() != core,
                     "QEMU-FST worker core=%u received shard for core=%u: %s",
                     core, assembler.coreId(), inputShard.c_str());
            coreVerified = true;
        }
        if (item.kind == QemuRawItemKind::Transaction &&
            item.transaction.kind == QemuTransactionKind::Macro &&
            item.transaction.measurement &&
            item.transaction.userMode()) {
            ++rawMeasurementUserMacros;
        }
        if (projector.targetReached()) {
            ready = assembler.next(item);
            continue;
        }
        if (item.kind == QemuRawItemKind::Transaction) {
            cpus.at(0)->lower(item.transaction, projector);
        } else {
            switch (item.kind) {
              case QemuRawItemKind::RoiBegin:
                projector.beginRoi();
                break;
              case QemuRawItemKind::MeasurementBegin:
                projector.beginMeasurement();
                break;
              case QemuRawItemKind::RoiEnd:
                projector.endRoi();
                break;
              case QemuRawItemKind::VcpuIdle:
                projector.vcpuIdle();
                break;
              case QemuRawItemKind::VcpuResume:
                projector.vcpuResume();
                break;
              case QemuRawItemKind::Transaction:
                break;
            }
        }
        ready = assembler.next(item);
    }
    fatal_if(!coreVerified,
             "QEMU-FST worker found no logical core in shard: %s",
             inputShard.c_str());
    fatal_if(
        !projector.targetReached() ||
            projector.counts().measurementUserRecords < userFstTarget,
        "QEMU-FST core=%u user UOP target not reached expected>=%" PRIu64
        " actual=%" PRIu64,
        core, userFstTarget,
        projector.counts().measurementUserRecords);
    projector.close();
    writeWorkerBoundaries(core, rawMeasurementUserMacros);
}

std::string
X86QemuFstTransfer::workerBoundaryJson(
    uint32_t core, uint64_t rawMeasurementUserMacros) const
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
           << "    \"raw_measurement_user_macros\": "
           << rawMeasurementUserMacros << ",\n"
           << "    \"measurement_mmio_records\": "
           << counts.measurementMmioRecords << ",\n"
           << "    \"measurement_pio_records\": "
           << counts.measurementPioRecords << ",\n"
           << "    \"frame_ram_bytes\": "
           << counts.frameRamBytes << ",\n"
           << "    \"user_fst_target\": " << userFstTarget << ",\n"
           << "    \"target_reached\": "
           << (counts.measurementUserRecords >= userFstTarget
                   ? "true" : "false")
           << ",\n"
           << "    \"stop_reason\": "
              "\"qemu-per-core-user-uop-target\",\n"
           << "    \"overshoot\": "
           << counts.measurementUserRecords - userFstTarget << ",\n"
           << "    \"lowered_kernel_uops_before_idle_filter\": "
           << counts.loweredKernelUopsBeforeIdleFilter << ",\n"
           << "    \"emitted_kernel_uops\": "
           << counts.emittedKernelUops << ",\n"
           << "    \"excluded_idle_kernel_uops\": "
           << counts.excludedIdleKernelUops << ",\n"
           << "    \"trace_scope\": \"user-plus-kernel\"\n"
           << "  }";
    return output.str();
}

void
X86QemuFstTransfer::writeWorkerBoundaries(
    uint32_t core, uint64_t rawMeasurementUserMacros) const
{
    const std::string path =
        outputDir + "/core" + std::to_string(core) + ".boundaries.json";
    std::ofstream output(path);
    fatal_if(!output,
             "failed to open QEMU-FST worker boundaries: %s", path);
    output << "{\n"
           << workerBoundaryJson(core, rawMeasurementUserMacros)
           << "\n}\n";
    fatal_if(!output,
             "failed to write QEMU-FST worker boundaries: %s", path);
}

} // namespace X86ISA
} // namespace gem5
