#include "arch/x86/qemu_record_reader.hh"

#include <algorithm>
#include <bitset>
#include <cinttypes>
#include <cstring>
#include <limits>

#include <zlib.h>

#include "base/logging.hh"

namespace gem5
{
namespace X86ISA
{

QemuRecordReader::QemuRecordReader(const std::filesystem::path &path)
    : path(path), compressed(path.extension() == ".gz")
{
    if (compressed) {
        gzipFile = gzopen(path.c_str(), "rb");
        fatal_if(!gzipFile, "failed to open QEMU-FST gzip trace: %s",
                 path.c_str());
        gzbuffer(gzipFile, 256 * 1024);
    } else {
        inputFile.open(path, std::ios::binary);
        fatal_if(!inputFile, "failed to open QEMU-FST trace file: %s",
                 path.c_str());
    }
    qemu_fst_record::FileHeader header{};
    readExact(&header, sizeof(header), false);
    fatal_if(
        header.magic != qemu_fst_record::kMagic ||
            header.version != qemu_fst_record::kVersion ||
            header.headerBytes != sizeof(header) ||
            header.stateFieldCount != qemu_fst_record::kStateFieldCount,
        "QEMU raw shard has an invalid file header: %s", path.c_str());
    logicalCoreId = header.coreId;
    coreIdValid = true;
}

QemuRecordReader::~QemuRecordReader()
{
    if (gzipFile) {
        fatal_if(gzclose(gzipFile) != Z_OK,
                 "failed closing QEMU-FST gzip trace: %s", path.c_str());
    }
}

uint32_t
QemuRecordReader::coreId() const
{
    fatal_if(!coreIdValid, "QEMU raw shard has no logical core: %s",
             path.c_str());
    return logicalCoreId;
}

bool
QemuRecordReader::next(CommitRecord &record)
{
    qemu_fst_record::Header header{};
    while (readWireRecord(header, stateValues, record.refs)) {
        fatal_if(footerSeen, "QEMU raw shard has records after footer: %s",
                 path.c_str());
        const bool payload =
            header.stateValueCount != 0 || header.accessCount != 0 ||
            header.stateBitmap[0] != 0 || header.stateBitmap[1] != 0;
        constexpr uint32_t knownFlags =
            qemu_fst_record::kHasTsc |
            qemu_fst_record::kHasTscAux |
            qemu_fst_record::kHasSyscallEvent;
        fatal_if(header.flags & ~knownFlags,
                 "QEMU raw record has unknown flags: %s", path.c_str());
        switch (header.kind) {
          case qemu_fst_record::Kind::RoiBegin:
            fatal_if(roiActive || roiEnded || payload,
                     "QEMU raw shard has an invalid ROI begin: %s",
                     path.c_str());
            roiActive = true;
            continue;
          case qemu_fst_record::Kind::MeasurementBegin:
            fatal_if(!roiActive || measurementActive || payload,
                     "QEMU raw shard has an invalid measurement begin: %s",
                     path.c_str());
            measurementActive = true;
            continue;
          case qemu_fst_record::Kind::RoiEnd:
            fatal_if(!roiActive || payload,
                     "QEMU raw shard has ROI end outside ROI: %s",
                     path.c_str());
            roiActive = false;
            roiEnded = true;
            continue;
          case qemu_fst_record::Kind::Footer:
            fatal_if((!roiActive && !roiEnded) ||
                         !measurementActive || payload,
                     "QEMU raw shard footer precedes a clean cutoff: %s",
                     path.c_str());
            footerSeen = true;
            continue;
          case qemu_fst_record::Kind::Macro:
          case qemu_fst_record::Kind::Event:
            fatal_if(!roiActive,
                     "QEMU raw record is outside ROI: %s", path.c_str());
            break;
          default:
            fatal("QEMU raw shard has unknown record kind=%u: %s",
                  unsigned(header.kind), path.c_str());
        }
        static_cast<qemu_fst_record::Header &>(record) = header;
        fatal_if(
            record.phase !=
                (measurementActive
                     ? qemu_fst_record::Phase::Measurement
                     : qemu_fst_record::Phase::Warmup),
            "QEMU raw record phase disagrees with its boundary stream: %s",
            path.c_str());
        consumeState(header, record);
        validateRecord(record);
        return true;
    }
    fatal_if(!footerSeen, "QEMU raw shard has no footer: %s", path.c_str());
    return false;
}

bool
QemuRecordReader::readExact(
    void *destination, size_t bytes, bool allowEof)
{
    auto *output = static_cast<uint8_t *>(destination);
    size_t completed = 0;
    while (completed != bytes) {
        size_t count = 0;
        if (compressed) {
            const size_t remaining = bytes - completed;
            fatal_if(remaining > INT_MAX,
                     "QEMU-FST raw read exceeds zlib limit: %s",
                     path.c_str());
            const int result = gzread(
                gzipFile, output + completed, static_cast<unsigned>(remaining));
            fatal_if(result < 0, "failed reading QEMU-FST gzip trace: %s",
                     path.c_str());
            count = static_cast<size_t>(result);
        } else {
            inputFile.read(
                reinterpret_cast<char *>(output + completed),
                static_cast<std::streamsize>(bytes - completed));
            count = static_cast<size_t>(inputFile.gcount());
        }
        if (count == 0) {
            if (allowEof && completed == 0) {
                return false;
            }
            fatal("QEMU-FST raw shard has a truncated record: %s",
                  path.c_str());
        }
        completed += count;
    }
    return true;
}

bool
QemuRecordReader::readWireRecord(
    qemu_fst_record::Header &header,
    std::vector<uint64_t> &values,
    std::vector<qemu_fst_record::Access> &recordAccesses)
{
    if (!readExact(&header, sizeof(header), true)) {
        return false;
    }
    fatal_if(
        header.totalBytes < sizeof(header) ||
            header.totalBytes % 48 != 0,
        "QEMU-FST raw shard has an invalid record header: %s", path.c_str());
    fatal_if(
        header.stateValueCount > qemu_fst_record::kStateFieldCount,
        "QEMU-FST raw state value count is invalid: %s", path.c_str());
    const size_t valuesBytes =
        static_cast<size_t>(header.stateValueCount) * sizeof(uint64_t);
    fatal_if(
        header.accessCount >
            (std::numeric_limits<size_t>::max() - valuesBytes) /
                sizeof(qemu_fst_record::Access),
        "QEMU-FST raw access count overflows: %s", path.c_str());
    const size_t payloadBytes = valuesBytes +
        static_cast<size_t>(header.accessCount) *
            sizeof(qemu_fst_record::Access);
    fatal_if(
        payloadBytes > header.totalBytes - sizeof(header),
        "QEMU-FST raw record payload exceeds its record: %s", path.c_str());
    values.resize(header.stateValueCount);
    recordAccesses.resize(header.accessCount);
    if (valuesBytes != 0) {
        readExact(values.data(), valuesBytes, false);
    }
    const size_t accessBytes =
        recordAccesses.size() * sizeof(qemu_fst_record::Access);
    if (accessBytes != 0) {
        readExact(recordAccesses.data(), accessBytes, false);
    }
    size_t padding =
        header.totalBytes - sizeof(header) - payloadBytes;
    std::array<uint8_t, 48> ignored{};
    while (padding != 0) {
        const size_t count = std::min(padding, ignored.size());
        readExact(ignored.data(), count, false);
        padding -= count;
    }
    return true;
}

void
QemuRecordReader::consumeState(
    const qemu_fst_record::Header &header, CommitRecord &record)
{
    const uint64_t validLowMask = UINT64_MAX;
    const uint64_t validHighMask =
        (UINT64_C(1) << (qemu_fst_record::kStateFieldCount - 64)) - 1;
    fatal_if(header.stateBitmap[1] & ~validHighMask,
             "QEMU raw state bitmap exceeds canonical state");
    if (requireFullState) {
        fatal_if(header.stateBitmap[0] != validLowMask ||
                     header.stateBitmap[1] != validHighMask,
                 "QEMU initial state packet is not complete");
    }
    const size_t expectedValues =
        std::bitset<64>(header.stateBitmap[0]).count() +
        std::bitset<64>(header.stateBitmap[1]).count();
    fatal_if(expectedValues != stateValues.size(),
             "QEMU state bitmap/value count mismatch");
    size_t value = 0;
    for (size_t field = 0; field < qemu_fst_record::kStateFieldCount; ++field) {
        if (header.stateBitmap[field / 64] &
            (UINT64_C(1) << (field % 64))) {
            currentState.fields[field] = stateValues[value++];
        }
    }
    record.state = &currentState;
    requireFullState = false;
}

void
QemuRecordReader::validateRecord(const CommitRecord &record) const
{
    const uint64_t cpl = record.privilege();
    fatal_if(cpl != 0 && cpl != 3,
             "QEMU state has unsupported CPL=%" PRIu64, cpl);
    fatal_if(record.addressSpaceId() == 0,
             "QEMU transaction has a zero ASID");
    if (record.kind == qemu_fst_record::Kind::Macro) {
        fatal_if(record.size == 0 || record.size > sizeof(record.bytes),
                 "QEMU macro has invalid instruction size=%u", record.size);
        fatal_if(
            record.syscall() &&
                (record.event.kind !=
                     qemu_trace_extensions::StructuredEventKind::kSyscall ||
                 record.event.disposition !=
                     qemu_trace_extensions::StructuredEventDisposition::
                         kRetiredTransfer),
            "QEMU macro carries invalid syscall metadata");
    } else {
        fatal_if(record.kind != qemu_fst_record::Kind::Event ||
                     record.event.kind ==
                         qemu_trace_extensions::StructuredEventKind::kSyscall,
                 "QEMU raw event has invalid kind");
    }
    for (const auto &ref : record.refs) {
        const bool pio =
            ref.attributes & qemu_trace_extensions::kMemoryAttributePio;
        fatal_if(
            ref.size == 0 || ref.size > sizeof(ref.value) ||
                (pio && ref.hasPhysicalAddress()) ||
                (!pio && !ref.hasPhysicalAddress()) ||
                (ref.isStore() && ref.hasValue()) ||
                (!ref.isStore() && !ref.hasValue()) ||
                (ref.hasValue() && ref.valueSize != ref.size),
            "QEMU raw memory fact is invalid");
    }
}

} // namespace X86ISA
} // namespace gem5
