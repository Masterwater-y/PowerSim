#include "arch/x86/qemu_transaction_assembler.hh"

#include <algorithm>
#include <cinttypes>
#include <cstring>

#include "base/logging.hh"
#include "lib/drmemtrace/trace_entry.h"

namespace gem5
{
namespace X86ISA
{
namespace
{

using namespace dynamorio::drmemtrace;

bool
isInstruction(trace_type_t type)
{
    return type == TRACE_TYPE_INSTR;
}

}

QemuTransactionAssembler::QemuTransactionAssembler(
    const std::filesystem::path &path)
    : reader(path), path(path)
{
}

uint32_t
QemuTransactionAssembler::coreId() const
{
    fatal_if(!coreIdValid, "QEMU raw shard has no logical core: %s",
             path.c_str());
    return logicalCoreId;
}

bool
QemuTransactionAssembler::nextRaw(QemuRawItem &item)
{
    if (bufferedRaw) {
        item = std::move(*bufferedRaw);
        bufferedRaw.reset();
        return true;
    }
    Entry entry;
    while (reader.next(entry)) {
        fatal_if(footerSeen, "QEMU raw shard has records after footer: %s",
                 path.c_str());
        if (consume(entry, item)) {
            return true;
        }
    }
    if (!eofValidated) {
        validateEnd();
        eofValidated = true;
    }
    return false;
}

bool
QemuTransactionAssembler::nextFrame(QemuFrameEnvelope &frame)
{
    QemuRawItem item;
    while (nextRaw(item)) {
        if (item.kind != QemuRawItemKind::Transaction) {
            if (item.kind == QemuRawItemKind::VcpuIdle) {
                vcpuIdle = true;
            } else if (item.kind == QemuRawItemKind::VcpuResume) {
                vcpuIdle = false;
            }
            continue;
        }
        if (pendingMacro) {
            if (item.transaction.kind == QemuTransactionKind::Event &&
                item.transaction.event.kind ==
                    qemu_trace_extensions::StructuredEventKind::kSyscall &&
                item.transaction.event.disposition ==
                    qemu_trace_extensions::StructuredEventDisposition::
                        kRetiredTransfer &&
                item.transaction.pc == pendingMacro->transaction.pc &&
                item.transaction.successor ==
                    pendingMacro->transaction.successor) {
                frame.frame = std::move(pendingMacro->transaction);
                frame.phase = pendingMacro->phase;
                frame.idle = pendingMacro->idle;
                frame.syscallEvent = item.transaction.event;
                pendingMacro.reset();
                return true;
            }
            frame.frame = std::move(pendingMacro->transaction);
            frame.phase = pendingMacro->phase;
            frame.idle = pendingMacro->idle;
            frame.syscallEvent.reset();
            bufferedRaw = std::move(item);
            pendingMacro.reset();
            return true;
        }
        fatal_if(
            item.transaction.kind == QemuTransactionKind::Event &&
                item.transaction.event.kind ==
                    qemu_trace_extensions::StructuredEventKind::kSyscall,
            "syscall event is not adjacent to its user gateway");
        if (item.transaction.kind == QemuTransactionKind::Macro &&
            item.transaction.userMode()) {
            pendingMacro = std::move(item);
            continue;
        }
        frame.frame = std::move(item.transaction);
        frame.phase = item.phase;
        frame.idle = item.idle;
        frame.syscallEvent.reset();
        return true;
    }
    if (pendingMacro) {
        frame.frame = std::move(pendingMacro->transaction);
        frame.phase = pendingMacro->phase;
        frame.idle = pendingMacro->idle;
        frame.syscallEvent.reset();
        pendingMacro.reset();
        return true;
    }
    return false;
}

bool
QemuTransactionAssembler::consume(const Entry &entry, QemuRawItem &item)
{
    const auto type = static_cast<trace_type_t>(entry.type);
    const bool memoryValue =
        type == TRACE_TYPE_VALUE_PART || type == TRACE_TYPE_VALUE_FULL;
    // Only load facts carry QEMU-returned bytes. Stores are checked by their
    // executed address/domain and write the transaction-local overlay from
    // official gem5 microcode results.
    fatal_if(
        !memoryValue && transaction && !transaction->refs.empty() &&
            !transaction->refs.back().is_store &&
            !transaction->refs.back().hasValue,
        "QEMU memory evidence has no complete value before the next record");
    if (type == TRACE_TYPE_HEADER) {
        fatal_if(capabilities.hasHeader || entry.addr != 7,
                 "QEMU raw shard has invalid header: %s", path.c_str());
        capabilities.hasHeader = true;
        return false;
    }
    if (type == TRACE_TYPE_FOOTER) {
        fatal_if(transaction || stateBitmapLowSeen || stateBitmapHighSeen ||
                     encodingSize != 0 || pendingPhysicalAddress ||
                     pendingMemoryAttributesValid,
                 "QEMU raw shard footer crosses an open transaction: %s",
                 path.c_str());
        capabilities.hasFooter = true;
        footerSeen = true;
        return false;
    }
    if (type == TRACE_TYPE_THREAD || type == TRACE_TYPE_PID) {
        fatal_if(transaction,
                 "QEMU shard identity crosses a transaction: %s",
                 path.c_str());
        identitySeen = true;
        return false;
    }
    if (type == TRACE_TYPE_MARKER) {
        if (entry.size == TRACE_MARKER_TYPE_VERSION) {
            fatal_if(capabilities.hasVersion || entry.addr != 7,
                     "QEMU raw shard has invalid version marker: %s",
                     path.c_str());
            capabilities.hasVersion = true;
            capabilities.version = entry.addr;
            return false;
        }
        if (entry.size == TRACE_MARKER_TYPE_FILETYPE) {
            fatal_if(capabilities.hasFiletype,
                     "QEMU raw shard has duplicate filetype: %s",
                     path.c_str());
            capabilities.hasFiletype = true;
            capabilities.hasX86_64 =
                entry.addr & OFFLINE_FILE_TYPE_ARCH_X86_64;
            capabilities.hasEncodings =
                entry.addr & OFFLINE_FILE_TYPE_ENCODINGS;
            capabilities.hasFullSystem =
                entry.addr & OFFLINE_FILE_TYPE_FULL_SYSTEM;
            capabilities.hasTransactional =
                entry.addr &
                qemu_trace_extensions::kFileTypeQemuFstTransactional;
            return false;
        }
        if (entry.size == TRACE_MARKER_TYPE_CACHE_LINE_SIZE ||
            entry.size == TRACE_MARKER_TYPE_CHUNK_INSTR_COUNT) {
            fatal_if(transaction,
                     "QEMU header marker crosses a transaction: %s",
                     path.c_str());
            return false;
        }
        if (entry.size == TRACE_MARKER_TYPE_TIMESTAMP) {
            fatal_if(transaction,
                     "QEMU timestamp crosses a transaction: %s",
                     path.c_str());
            return false;
        }
        return consumeMarker(entry.size, entry.addr, item);
    }
    fatal_if(!identitySeen, "QEMU raw record precedes shard identity: %s",
             path.c_str());
    if (type == static_cast<trace_type_t>(
                    qemu_trace_extensions::kTraceTypeStateField)) {
        consumeState(entry.size, entry.addr);
        return false;
    }
    if (type == TRACE_TYPE_ENCODING) {
        appendEncoding(entry);
        return false;
    }
    // Advisory hints have no successful RAM access/value in QEMU. Their
    // containing instruction still follows the ordinary macro path.
    if (type_is_prefetch(type)) return false;
    if (isInstruction(type)) {
        consumeInstruction(entry);
        return false;
    }
    if (type == TRACE_TYPE_READ || type == TRACE_TYPE_WRITE) {
        consumeMemory(entry, type == TRACE_TYPE_WRITE);
        return false;
    }
    if (type == TRACE_TYPE_VALUE_PART || type == TRACE_TYPE_VALUE_FULL) {
        consumeValue(entry, type == TRACE_TYPE_VALUE_FULL);
        return false;
    }
    fatal("QEMU raw shard contains unsupported record type=%u: %s",
          unsigned(entry.type), path.c_str());
}

bool
QemuTransactionAssembler::consumeMarker(
    uint16_t type, uint64_t value, QemuRawItem &item)
{
    using namespace qemu_trace_extensions;
    if (type == TRACE_MARKER_TYPE_CPU_ID) {
        fatal_if(transaction || !identitySeen,
                 "QEMU core identity crosses a transaction: %s",
                 path.c_str());
        fatal_if(value > UINT32_MAX, "QEMU core ID exceeds uint32: %s",
                 path.c_str());
        if (coreIdValid) {
            fatal_if(logicalCoreId != value,
                     "QEMU raw shard changed logical core: %s",
                     path.c_str());
        } else {
            logicalCoreId = value;
            coreIdValid = true;
        }
        return false;
    }
    if (type == TRACE_MARKER_TYPE_PRIVILEGE_LEVEL) return false;
    if (type == kMarkerRoiBoundary) {
        fatal_if(transaction || !coreIdValid,
                 "ROI boundary crosses a raw transaction or shard identity");
        if (value == kRoiBoundaryBegin) {
            fatal_if(roiActive || roiEnded, "duplicate QEMU ROI begin");
            roiActive = true;
            item.kind = QemuRawItemKind::RoiBegin;
            return true;
        }
        if (value == kRoiBoundaryMeasurement) {
            fatal_if(!roiActive || measurementActive,
                     "invalid QEMU measurement boundary");
            measurementActive = true;
            item.kind = QemuRawItemKind::MeasurementBegin;
            return true;
        }
        if (value == kRoiBoundaryEnd) {
            fatal_if(!roiActive || roiEnded, "invalid QEMU ROI end");
            roiActive = false;
            roiEnded = true;
            item.kind = QemuRawItemKind::RoiEnd;
            return true;
        }
        fatal("unknown QEMU ROI boundary=%" PRIu64, value);
    }
    if (type == kMarkerVcpuState) {
        fatal_if(transaction || !coreIdValid,
                 "vCPU state marker crosses a transaction or shard identity");
        fatal_if(value != static_cast<uint64_t>(VcpuState::kIdle) &&
                     value != static_cast<uint64_t>(VcpuState::kResume),
                 "unknown QEMU vCPU state=%" PRIu64, value);
        item.kind = value == static_cast<uint64_t>(VcpuState::kIdle)
            ? QemuRawItemKind::VcpuIdle : QemuRawItemKind::VcpuResume;
        return true;
    }
    if (type == kMarkerMacroBegin) {
        beginTransaction(QemuTransactionKind::Macro, value);
        return false;
    }
    if (type == kMarkerEventBegin) {
        beginTransaction(QemuTransactionKind::Event, value);
        return false;
    }
    if (type == kMarkerTscRead || type == kMarkerTscAuxRead) {
        fatal_if(!transaction ||
                     transaction->kind != QemuTransactionKind::Macro ||
                     !transactionHasInstruction ||
                     stateBitmapLowSeen || stateBitmapHighSeen ||
                     pendingMemoryAttributesValid || pendingPhysicalAddress,
                 "QEMU timestamp observation is outside a completed macro body");
        if (type == kMarkerTscRead) {
            fatal_if(transaction->tscRead,
                     "duplicate QEMU timestamp observation");
            transaction->tscRead = value;
        } else {
            fatal_if(transaction->tscAuxRead || value > UINT32_MAX,
                     "invalid QEMU TSC_AUX observation");
            transaction->tscAuxRead = static_cast<uint32_t>(value);
        }
        return false;
    }
    if (type == kMarkerMemoryAttributes) {
        fatal_if(!transaction || pendingPhysicalAddress ||
                     pendingMemoryAttributesValid,
                 "QEMU memory attributes overlap another reference");
        fatal_if(value & ~(kMemoryAttributeFrameRam |
                           kMemoryAttributePio | kMemoryAttributeMmio),
                 "QEMU memory attributes have unknown bits=%#" PRIx64,
                 value);
        fatal_if(
            (value & kMemoryAttributeFrameRam) &&
                (value & (kMemoryAttributePio |
                          kMemoryAttributeMmio)),
            "QEMU frame RAM observation has invalid domain");
        pendingMemoryAttributes = static_cast<uint32_t>(value);
        pendingMemoryAttributesValid = true;
        return false;
    }
    if (type == kMarkerMemoryPhysicalAddress) {
        fatal_if(!transaction || pendingPhysicalAddress,
                 "QEMU physical address overlaps another reference");
        pendingPhysicalAddress = value;
        return false;
    }
    if (type == kMarkerEventMetadata) {
        const auto kind = static_cast<StructuredEventKind>(value & 0x7);
        const auto disposition =
            static_cast<StructuredEventDisposition>((value >> 3) & 0x3);
        fatal_if(!transaction ||
                     transaction->kind != QemuTransactionKind::Event ||
                     transactionHasEventMetadata || value >> 47 ||
                     kind > StructuredEventKind::kSyscall ||
                     disposition >
                         StructuredEventDisposition::kUnretiredFault ||
                     ((kind == StructuredEventKind::kInterrupt ||
                       kind == StructuredEventKind::kException) &&
                      !(value & (uint64_t{1} << 5))),
                 "invalid QEMU event metadata");
        transaction->event.kind = kind;
        transaction->event.disposition = disposition;
        transaction->event.vectorValid = value & (uint64_t{1} << 5);
        transaction->event.errorCodeValid = value & (uint64_t{1} << 6);
        transaction->event.vector = (value >> 7) & 0xff;
        transaction->event.errorCode = value >> 15;
        transactionHasEventMetadata = true;
        return false;
    }
    if (type == kMarkerMacroCommit || type == kMarkerEventEnd) {
        fatal_if(!transaction ||
                     (type == kMarkerMacroCommit &&
                      transaction->kind != QemuTransactionKind::Macro) ||
                     (type == kMarkerEventEnd &&
                      transaction->kind != QemuTransactionKind::Event),
                 "QEMU transaction terminator does not match begin");
        fatal_if(stateBitmapLowSeen || stateBitmapHighSeen ||
                     !transactionHasState ||
                     pendingPhysicalAddress ||
                     pendingMemoryAttributesValid || encodingSize != 0,
                 "QEMU transaction is incomplete at commit");
        fatal_if(transaction->kind == QemuTransactionKind::Macro &&
                     !transactionHasInstruction,
                 "QEMU macro transaction lacks instruction or privilege");
        fatal_if(transaction->kind == QemuTransactionKind::Event &&
                     !transactionHasEventMetadata,
                 "QEMU event transaction has no metadata");
        transaction->successor = value;
        item.kind = QemuRawItemKind::Transaction;
        item.transaction = std::move(*transaction);
        item.phase = measurementActive
            ? QemuFramePhase::Measurement : QemuFramePhase::Warmup;
        item.idle = vcpuIdle;
        transaction.reset();
        transactionHasState = false;
        transactionHasInstruction = false;
        transactionHasEventMetadata = false;
        return true;
    }
    fatal("QEMU raw shard contains unknown marker=%u value=%#" PRIx64,
          unsigned(type), value);
}

void
QemuTransactionAssembler::beginTransaction(
    QemuTransactionKind kind, Addr pc)
{
    fatal_if(!coreIdValid || !roiActive || transaction ||
                 stateBitmapLowSeen || stateBitmapHighSeen ||
                 encodingSize != 0 || pendingPhysicalAddress ||
                 pendingMemoryAttributesValid,
             "QEMU transaction begins in an invalid state");
    transaction.emplace();
    transaction->kind = kind;
    transaction->pc = pc;
}

void
QemuTransactionAssembler::consumeState(uint16_t kind, uint64_t value)
{
    using namespace qemu_trace_extensions;
    fatal_if(!transaction || transactionHasState,
             "QEMU state record is outside a transaction");
    if (kind == kStateBitmapLow) {
        fatal_if(stateBitmapLowSeen || stateBitmapHighSeen,
                 "invalid QEMU low state bitmap");
        stateBitmap[0] = value;
        stateBitmapLowSeen = true;
        return;
    }
    if (kind == kStateBitmapHigh) {
        fatal_if(!stateBitmapLowSeen || stateBitmapHighSeen ||
                     (kStateFieldCount > 64 &&
                      (value >> (kStateFieldCount - 64)) != 0) ||
                     (kStateFieldCount <= 64 && value != 0),
                 "invalid QEMU high state bitmap");
        stateBitmap[1] = value;
        stateBitmapHighSeen = true;
        pendingState = requireFullState ? QemuX86State{} : currentState;
        nextStateField = 0;
        while (nextStateField < kStateFieldCount &&
               !(stateBitmap[nextStateField / 64] &
                 (UINT64_C(1) << (nextStateField % 64)))) {
            ++nextStateField;
        }
        if (nextStateField == kStateFieldCount) {
            finishState();
        }
        return;
    }
    fatal_if(kind != kStateValue || !stateBitmapHighSeen ||
                 nextStateField >= kStateFieldCount,
             "invalid QEMU compact state value");
    pendingState.fields[nextStateField] = value;
    ++nextStateField;
    while (nextStateField < kStateFieldCount &&
           !(stateBitmap[nextStateField / 64] &
             (UINT64_C(1) << (nextStateField % 64)))) {
        ++nextStateField;
    }
    if (nextStateField == kStateFieldCount) {
        finishState();
    }
}

void
QemuTransactionAssembler::finishState()
{
    using namespace qemu_trace_extensions;
    const uint64_t validLowMask = kStateFieldCount >= 64
        ? UINT64_MAX
        : (UINT64_C(1) << kStateFieldCount) - 1;
    const uint64_t validHighMask = kStateFieldCount > 64
        ? (UINT64_C(1) << (kStateFieldCount - 64)) - 1
        : 0;
    fatal_if(requireFullState &&
                 (stateBitmap[0] != validLowMask ||
                  stateBitmap[1] != validHighMask),
             "QEMU initial state packet is not complete");
    currentState = pendingState;
    transaction->state = currentState;
    const uint64_t stateCpl = transaction->state.get(kStateCpl);
    fatal_if(stateCpl != 0 && stateCpl != 3,
             "QEMU state has unsupported CPL=%" PRIu64, stateCpl);
    fatal_if(transaction->addressSpaceId() == 0,
             "QEMU transaction has a zero ASID");
    transactionHasState = true;
    requireFullState = false;
    stateBitmap = {};
    stateBitmapLowSeen = false;
    stateBitmapHighSeen = false;
    nextStateField = 0;
}

void
QemuTransactionAssembler::appendEncoding(const Entry &entry)
{
    fatal_if(!transaction ||
                 transaction->kind != QemuTransactionKind::Macro ||
                 transactionHasInstruction ||
                 encodingSize + entry.size > encoding.size(),
             "invalid QEMU instruction encoding sequence");
    std::memcpy(encoding.data() + encodingSize, entry.encoding, entry.size);
    encodingSize += entry.size;
}

void
QemuTransactionAssembler::consumeInstruction(const Entry &entry)
{
    const auto type = static_cast<trace_type_t>(entry.type);
    fatal_if(!transaction ||
                 transaction->kind != QemuTransactionKind::Macro ||
                 !transactionHasState || transactionHasInstruction ||
                 entry.addr != transaction->pc ||
                 encodingSize == 0 || encodingSize != entry.size,
             "invalid QEMU macro instruction framing pc=%#x", entry.addr);
    fatal_if(entry.size == 0 || entry.size > transaction->bytes.size(),
             "unsupported x86 instruction length=%u", unsigned(entry.size));
    fatal_if(type != TRACE_TYPE_INSTR,
             "QEMU macro uses producer-side instruction classification=%u",
             unsigned(type));
    transaction->size = entry.size;
    std::memcpy(transaction->bytes.data(), encoding.data(), encodingSize);
    encodingSize = 0;
    transactionHasInstruction = true;
}

void
QemuTransactionAssembler::consumeMemory(const Entry &entry, bool store)
{
    using namespace qemu_trace_extensions;
    const uint32_t attributes =
        pendingMemoryAttributesValid ? pendingMemoryAttributes : 0;
    // A single AccessFact is one successful QEMU access; load bytes are
    // bounded by the transport value payload and stores carry no value.
    fatal_if(!transaction || !transactionHasState || entry.size == 0 ||
                 entry.size > 16,
             "invalid QEMU memory evidence addr=%#x size=%u",
             entry.addr, unsigned(entry.size));
    const bool pio = attributes & kMemoryAttributePio;
    const bool frameRam = attributes & kMemoryAttributeFrameRam;
    fatal_if(pio ? (pendingPhysicalAddress.has_value() ||
                       entry.addr > 0xffff ||
                       (entry.size != 1 && entry.size != 2 && entry.size != 4) ||
                       attributes != kMemoryAttributePio)
                 : !pendingPhysicalAddress.has_value(),
             "QEMU memory evidence has invalid address-domain metadata");
    fatal_if(frameRam && store,
             "QEMU frame RAM evidence cannot be a store");
    QemuDataRef ref;
    ref.is_store = store;
    ref.vaddr = entry.addr;
    ref.paddr = pendingPhysicalAddress.value_or(0);
    ref.size = entry.size;
    ref.attributes = attributes;
    transaction->refs.push_back(ref);
    pendingPhysicalAddress.reset();
    pendingMemoryAttributes = 0;
    pendingMemoryAttributesValid = false;
}

void
QemuTransactionAssembler::consumeValue(const Entry &entry, bool final)
{
    fatal_if(!transaction || transaction->refs.empty(),
             "QEMU value has no memory reference");
    auto &ref = transaction->refs.back();
    fatal_if(
        ref.is_store || ref.hasValue || entry.size != ref.size ||
            (!final && (ref.size <= 8 || ref.valueSize != 0)) ||
            (final && ((ref.size <= 8 && ref.valueSize != 0) ||
                       (ref.size > 8 && ref.valueSize != 8))),
        "QEMU memory value framing is invalid");
    const size_t offset =
        !final ? 0 : ref.valueSize == 8 && ref.size > 8 ? 8 : 0;
    const size_t bytes =
        std::min<size_t>(sizeof(entry.addr), ref.size - offset);
    fatal_if(offset + bytes > ref.value.size(),
             "QEMU value exceeds the 16-byte contract");
    std::memcpy(ref.value.data() + offset, &entry.addr, bytes);
    ref.valueSize = static_cast<uint8_t>(offset + bytes);
    ref.hasValue = final;
    fatal_if(final && ref.valueSize != ref.size,
             "QEMU memory value does not cover its reference");
}

void
QemuTransactionAssembler::validateCapabilities() const
{
    fatal_if(!capabilities.hasHeader || !capabilities.hasVersion ||
                 capabilities.version != 7 || !capabilities.hasFiletype ||
                 !capabilities.hasX86_64 || !capabilities.hasEncodings ||
                 !capabilities.hasFullSystem ||
                 !capabilities.hasTransactional ||
                 !capabilities.hasFooter,
             "QEMU raw shard lacks the transactional structure: %s",
             path.c_str());
}

void
QemuTransactionAssembler::validateEnd()
{
    validateCapabilities();
    fatal_if(!coreIdValid || !identitySeen || !roiActive ||
                 !measurementActive || roiEnded || transaction ||
                 stateBitmapLowSeen || stateBitmapHighSeen ||
                 encodingSize != 0 || pendingPhysicalAddress ||
                 pendingMemoryAttributesValid,
             "QEMU raw shard did not end at a clean target cutoff: %s",
             path.c_str());
}

} // namespace X86ISA
} // namespace gem5
