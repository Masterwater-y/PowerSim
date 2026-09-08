#include "arch/x86/qemu_fst_converter.hh"
#include "arch/x86/qemu_microcode_executor.hh"
#include "arch/x86/qemu_raw_trace_reader.hh"

#include <algorithm>
#include <array>
#include <cerrno>
#include <cstring>
#include <filesystem>
#include <iostream>
#include <map>
#include <utility>

#include "arch/x86/pcstate.hh"
#include "arch/x86/regs/ccr.hh"
#include "arch/x86/regs/float.hh"
#include "arch/x86/regs/int.hh"
#include "base/logging.hh"
#include "cpu/static_inst.hh"
#include "debug/Decode.hh"
#include "sim/sim_exit.hh"

namespace gem5
{
namespace X86ISA
{
namespace
{

using dynamorio::drmemtrace::trace_entry_t;
using dynamorio::drmemtrace::trace_type_t;

constexpr uint64_t kFastSimPageSize = 4096;
constexpr uint16_t kQemuMarkerPrivilegeLevel =
    dynamorio::drmemtrace::TRACE_MARKER_TYPE_PRIVILEGE_LEVEL;
constexpr uint16_t kQemuMarkerUserStateBegin =
    qemu_trace_extensions::kMarkerUserStateBegin;
constexpr uint16_t kQemuMarkerStructuredEvent =
    qemu_trace_extensions::kMarkerStructuredEvent;
constexpr uint16_t kQemuMarkerRoiBoundary =
    qemu_trace_extensions::kMarkerRoiBoundary;
constexpr uint16_t kQemuMarkerAddressSpace =
    qemu_trace_extensions::kMarkerAddressSpace;
constexpr uint16_t kQemuMarkerMemoryAttributes =
    qemu_trace_extensions::kMarkerMemoryAttributes;
constexpr uint16_t kQemuMarkerMemoryPhysicalAddress =
    qemu_trace_extensions::kMarkerMemoryPhysicalAddress;
constexpr uint64_t kQemuRoiBoundaryBegin =
    qemu_trace_extensions::kRoiBoundaryBegin;
constexpr uint64_t kQemuRoiBoundaryEnd =
    qemu_trace_extensions::kRoiBoundaryEnd;
constexpr uint64_t kQemuRoiBoundaryMeasurement =
    qemu_trace_extensions::kRoiBoundaryMeasurement;
constexpr uint32_t kQemuMemAttrAtomic =
    qemu_trace_extensions::kMemoryAttributeAtomic;
constexpr uint32_t kQemuMemAttrPio =
    qemu_trace_extensions::kMemoryAttributePio;
constexpr uint32_t kQemuMemAttrMmio =
    qemu_trace_extensions::kMemoryAttributeMmio;
constexpr trace_type_t kQemuTraceTypeValuePart =
    dynamorio::drmemtrace::TRACE_TYPE_VALUE_PART;
constexpr trace_type_t kQemuTraceTypeValueFull =
    dynamorio::drmemtrace::TRACE_TYPE_VALUE_FULL;
constexpr trace_type_t kQemuTraceTypeUserState =
    static_cast<trace_type_t>(qemu_trace_extensions::kTraceTypeUserState);
constexpr uint64_t kQemuFiletypeX86_64 =
    dynamorio::drmemtrace::OFFLINE_FILE_TYPE_ARCH_X86_64;
constexpr uint64_t kQemuFiletypeEncodings =
    dynamorio::drmemtrace::OFFLINE_FILE_TYPE_ENCODINGS;
constexpr uint64_t kQemuFiletypeFullSystem =
    dynamorio::drmemtrace::OFFLINE_FILE_TYPE_FULL_SYSTEM;

struct QemuMacroAssembler
{
    int64_t threadId = 0;
    bool hasThreadId = false;
    bool hasProcessId = false;
    uint64_t pendingCpu = 0;
    uint64_t pendingPrivilege = 0;
    bool hasPendingCpu = false;
    bool hasPendingPrivilege = false;
    std::array<uint8_t, 16> encoding = {};
    size_t encodingSize = 0;

    void appendEncoding(
        const trace_entry_t &record, const std::filesystem::path &path)
    {
        fatal_if(encodingSize + record.size > encoding.size(),
                 "QEMU-FST instruction encoding exceeds %zu bytes in %s",
                 encoding.size(), path.c_str());
        std::memcpy(
            encoding.data() + encodingSize, record.encoding, record.size);
        encodingSize += record.size;
    }

    const uint8_t *finishEncoding(
        const trace_entry_t &record, const std::filesystem::path &path)
    {
        fatal_if(encodingSize == 0,
                 "QEMU-FST instruction lacks encoding pc=%#x", record.addr);
        fatal_if(
            encodingSize != record.size,
            "QEMU-FST instruction encoding length mismatch "
            "pc=%#x encoding=%zu instruction=%u",
            record.addr, encodingSize, unsigned(record.size));
        encodingSize = 0;
        return encoding.data();
    }
};

bool
isInstr(trace_type_t type)
{
    using namespace dynamorio::drmemtrace;
    return type == TRACE_TYPE_INSTR ||
           type == TRACE_TYPE_INSTR_DIRECT_JUMP ||
           type == TRACE_TYPE_INSTR_INDIRECT_JUMP ||
           type == TRACE_TYPE_INSTR_CONDITIONAL_JUMP ||
           type == TRACE_TYPE_INSTR_DIRECT_CALL ||
           type == TRACE_TYPE_INSTR_INDIRECT_CALL ||
           type == TRACE_TYPE_INSTR_RETURN ||
           type == TRACE_TYPE_INSTR_SYSENTER ||
           type == TRACE_TYPE_INSTR_NO_FETCH ||
           type == TRACE_TYPE_INSTR_TAKEN_JUMP ||
           type == TRACE_TYPE_INSTR_UNTAKEN_JUMP;
}

bool
isRead(trace_type_t type)
{
    return type == dynamorio::drmemtrace::TRACE_TYPE_READ;
}

bool
isWrite(trace_type_t type)
{
    return type == dynamorio::drmemtrace::TRACE_TYPE_WRITE;
}

bool
isPrefetch(trace_type_t type)
{
    return dynamorio::drmemtrace::type_is_prefetch(type);
}

bool
isValidDataRef(Addr addr, uint64_t size)
{
    return addr != 0 && size != 0;
}

bool
isControl(trace_type_t type)
{
    using namespace dynamorio::drmemtrace;
    return type == TRACE_TYPE_INSTR_DIRECT_JUMP ||
           type == TRACE_TYPE_INSTR_INDIRECT_JUMP ||
           type == TRACE_TYPE_INSTR_CONDITIONAL_JUMP ||
           type == TRACE_TYPE_INSTR_DIRECT_CALL ||
           type == TRACE_TYPE_INSTR_INDIRECT_CALL ||
           type == TRACE_TYPE_INSTR_RETURN ||
           type == TRACE_TYPE_INSTR_TAKEN_JUMP ||
           type == TRACE_TYPE_INSTR_UNTAKEN_JUMP;
}

bool
isTaken(trace_type_t type)
{
    using namespace dynamorio::drmemtrace;
    return type == TRACE_TYPE_INSTR_DIRECT_JUMP ||
           type == TRACE_TYPE_INSTR_INDIRECT_JUMP ||
           type == TRACE_TYPE_INSTR_DIRECT_CALL ||
           type == TRACE_TYPE_INSTR_INDIRECT_CALL ||
           type == TRACE_TYPE_INSTR_RETURN ||
           type == TRACE_TYPE_INSTR_TAKEN_JUMP;
}

bool
isCond(trace_type_t type)
{
    using namespace dynamorio::drmemtrace;
    return type == TRACE_TYPE_INSTR_CONDITIONAL_JUMP ||
           type == TRACE_TYPE_INSTR_TAKEN_JUMP ||
           type == TRACE_TYPE_INSTR_UNTAKEN_JUMP;
}

} // namespace

QemuFstConverter::QemuFstConverter(const X86QemuUserFstLowererParams &params)
    : SimObject(params),
      decoder(params.decoder),
      inputTrace(params.input_trace),
      outputDir(params.output_dir),
      expectedNumCores(params.expected_num_cores),
      minUserUops(params.min_user_uops),
      convertEvent([this] { convert(); }, name() + ".convert")
{
    fatal_if(!decoder, "X86QemuUserFstLowerer requires a decoder");
    fatal_if(expectedNumCores == 0,
             "x86 QEMU-FST conversion requires at least one core");
    fatal_if(minUserUops == 0,
             "QEMU-FST conversion requires a positive minimum CPL3 UOP count");
    fatal_if(!std::filesystem::is_directory(outputDir),
             "x86 QEMU-FST output directory does not exist: %s", outputDir);
    coreThreads.resize(expectedNumCores, 0);
    HandyM5Reg m5_reg = 0;
    m5_reg.mode = LongMode;
    m5_reg.submode = SixtyFourBitMode;
    m5_reg.defOp = 2;
    m5_reg.altOp = 1;
    m5_reg.defAddr = 3;
    m5_reg.altAddr = 2;
    m5_reg.stack = 3;
    decoder->setM5Reg(m5_reg);
}

QemuFstConverter::ThreadState::ThreadState()
    : dependencies(
          int_reg::NumRegs + IntFoldBit, float_reg::NumRegs, 1,
          cc_reg::NumRegs),
      microcodeExecutor(std::make_unique<QemuMicrocodeExecutor>())
{
}

QemuFstConverter::~QemuFstConverter()
{
    closeOutputs();
}

void
QemuFstConverter::recordRawFiletype(uint64_t filetype)
{
    rawCapabilities.hasFiletype = true;
    rawCapabilities.hasX86_64 = rawCapabilities.hasX86_64 ||
        (filetype & kQemuFiletypeX86_64);
    rawCapabilities.hasEncodings = rawCapabilities.hasEncodings ||
        (filetype & kQemuFiletypeEncodings);
    rawCapabilities.hasFullSystem = rawCapabilities.hasFullSystem ||
        (filetype & kQemuFiletypeFullSystem);
}

void
QemuFstConverter::validateRawCapabilities() const
{
    fatal_if(!rawCapabilities.hasHeader,
             "QEMU-FST raw trace has no header");
    fatal_if(!rawCapabilities.hasVersion,
             "QEMU-FST raw trace has no trace format version marker");
    fatal_if(rawCapabilities.version != 7,
             "QEMU-FST raw trace version must be 7, observed=%" PRIu64,
             rawCapabilities.version);
    fatal_if(!rawCapabilities.hasFiletype,
             "QEMU-FST raw trace has no filetype marker");
    fatal_if(!rawCapabilities.hasX86_64 || !rawCapabilities.hasEncodings ||
                 !rawCapabilities.hasFullSystem,
             "QEMU-FST raw trace lacks required x86_64/encodings/full-system capabilities");
    fatal_if(!rawCapabilities.hasFooter,
             "QEMU-FST raw trace lacks footer");
}

void
QemuFstConverter::startup()
{
    schedule(convertEvent, curTick());
}


StaticInstPtr
QemuFstConverter::decodeMacro(const PendingInst &inst)
{
    PCState pc(inst.pc);
    decoder->reset();
    const size_t chunk_size = decoder->moreBytesSize();
    const Addr firstFetchPc = inst.pc & ~(Addr(chunk_size) - 1);
    Addr fetchPc = firstFetchPc;
    while (decoder->needMoreBytes() &&
           fetchPc < inst.pc + inst.size) {
        std::memset(decoder->moreBytesPtr(), 0, chunk_size);
        const size_t chunkOffset =
            fetchPc < inst.pc ? inst.pc - fetchPc : 0;
        const size_t instructionOffset =
            fetchPc > inst.pc ? fetchPc - inst.pc : 0;
        const size_t remaining = inst.size - instructionOffset;
        const size_t copied =
            std::min(chunk_size - chunkOffset, remaining);
        std::memcpy(
            static_cast<uint8_t *>(decoder->moreBytesPtr()) + chunkOffset,
            inst.bytes.data() + instructionOffset, copied);
        decoder->moreBytes(pc, fetchPc);
        fetchPc += chunk_size;
    }
    if (decoder->instReady()) {
        auto decoded = decoder->decode(pc);
        if (decoded) {
            fatal_if(
                pc.size() != inst.size,
                "gem5 decoded instruction length mismatch at pc=%#x: "
                "raw=%zu decoded=%u",
                inst.pc, inst.size, pc.size());
            return decoded;
        }
    }
    fatal("failed to decode x86 instruction at %#x", inst.pc);
}

size_t
QemuFstConverter::StaticInstructionKeyHash::operator()(
    const StaticInstructionKey &key) const
{
    size_t hash = std::hash<Addr>{}(key.pc);
    hash ^= std::hash<uint8_t>{}(key.size) + 0x9e3779b9 + (hash << 6) +
        (hash >> 2);
    hash ^= std::hash<bool>{}(key.isControl) + 0x9e3779b9 + (hash << 6) +
        (hash >> 2);
    for (const uint64_t word : key.encoding) {
        hash ^= std::hash<uint64_t>{}(word) + 0x9e3779b9 +
            (hash << 6) + (hash >> 2);
    }
    return hash;
}

QemuFstConverter::StaticInstructionKey
QemuFstConverter::staticInstructionKey(const PendingInst &inst) const
{
    StaticInstructionKey key;
    key.pc = inst.pc;
    key.size = static_cast<uint8_t>(inst.size);
    key.isControl = inst.is_control;
    std::memcpy(key.encoding.data(), inst.bytes.data(), inst.size);
    return key;
}

QemuFstConverter::StaticLowering &
QemuFstConverter::staticMacro(const PendingInst &inst)
{
    const auto key = staticInstructionKey(inst);
    const auto found = staticLowerings.find(key);
    if (found != staticLowerings.end()) {
        return found->second;
    }

    StaticLowering lowering;
    lowering.macro = decodeMacro(inst);
    lowering.mnemonic = lowering.macro->getName();
    return staticLowerings.emplace(std::move(key), std::move(lowering))
        .first->second;
}

QemuFstConverter::StaticLowering &
QemuFstConverter::staticLowering(const PendingInst &inst)
{
    auto &lowering = staticMacro(inst);
    if (lowering.loweringBuilt) {
        return lowering;
    }
    const auto append = [&](const StaticInstPtr &micro) {
        const auto &descriptor = microopDescriptors.get(micro);
        lowering.microops.push_back(&descriptor);
        const size_t index = lowering.microops.size() - 1;
        if (descriptor.isMemory) {
            lowering.memoryPlan.operations.push_back({
                index,
                descriptor.dataSize,
                descriptor.displacement,
                0,
                descriptor.hasDisplacement,
                descriptor.isLoad,
                descriptor.isStore,
                descriptor.isAtomic,
            });
        }
    };
    if (!lowering.macro->isMacroop()) {
        append(lowering.macro);
    } else {
        for (MicroPC microPc = 0;; ++microPc) {
            const auto micro = lowering.macro->fetchMicroop(microPc);
            if (micro->isControl() && !micro->isLastMicroop()) {
                lowering.requiresExecution = true;
                lowering.microops.clear();
                lowering.memoryPlan = {};
                break;
            }
            append(micro);
            if (micro->isLastMicroop()) {
                break;
            }
        }
    }
    QemuMemoryBinder::buildStaticPlan(lowering.memoryPlan);
    lowering.loweringBuilt = true;
    return lowering;
}
void
QemuFstConverter::recordAddressSpace(ThreadState &state, uint64_t asid)
{
    fatal_if(asid == 0,
             "FST record has no address-space identity core=%" PRIu64,
             state.logicalCoreId);
    openCoreOutput(state);
    if (state.emission.writtenAddressSpaceId == asid) {
        return;
    }
    try {
        state.emission.writer->set_address_space_id(asid);
        state.emission.writtenAddressSpaceId = asid;
    } catch (const std::exception &error) {
        fatal("failed setting FST address space core=%" PRIu64 ": %s",
              state.logicalCoreId, error.what());
    }
}

void
QemuFstConverter::bindQemuWindowedCore(ThreadState &state,
                                       uint64_t core)
{
    fatal_if(core >= expectedNumCores,
             "QEMU-FST vCPU %" PRIu64 " exceeds expected core count %u",
             core, expectedNumCores);
    if (!state.logicalCoreIdValid) {
        fatal_if(coreThreads[core] != 0,
                 "QEMU-FST logical core %" PRIu64
                 " has multiple raw ROI shards (%" PRId64 " and %" PRId64 ")",
                 core, coreThreads[core], currentQemuShardKey);
        coreThreads[core] = currentQemuShardKey;
        state.logicalCoreId = core;
        state.logicalCoreIdValid = true;
    } else {
        fatal_if(state.logicalCoreId != core,
                 "QEMU-FST trace thread %" PRId64
                 " changed vCPU from %" PRIu64 " to %" PRIu64,
                 state.drThreadId, state.logicalCoreId, core);
    }
}

void
QemuFstConverter::finalizeQemuWindowedRoi(ThreadState &state)
{
    if (!state.logicalCoreIdValid) {
        return;
    }
    fatal_if(state.roiActive || !state.roiCompleted ||
                 state.roiBeginMarkers != 1 ||
                 state.measurementBeginMarkers != 1 ||
                 state.roiEndMarkers != 1,
             "QEMU-FST trace lacks one complete warmup and measurement "
             "interval core=%" PRIu64,
             state.logicalCoreId);
}

void
QemuFstConverter::openCoreOutput(ThreadState &state)
{
    fatal_if(!state.logicalCoreIdValid,
             "ROI logical core was not observed for DR thread %" PRId64,
             state.drThreadId);
    if (state.emission.writer) {
        return;
    }
    const std::string path = outputDir + "/core" +
        std::to_string(state.logicalCoreId) + ".fst";
    try {
        state.emission.writer = std::make_unique<fastsim::BinaryTraceWriter>(
            path, static_cast<uint32_t>(state.logicalCoreId),
            fastsim::SyscallAbi::kLinuxX86_64);
    } catch (const std::exception &error) {
        fatal("failed to open FastSim FST output %s: %s",
              path, error.what());
    }
}

void
QemuFstConverter::writeRecord(ThreadState &state, const PendingInst &inst,
                               const StaticInstPtr &micro, size_t microPc,
                               size_t microCount, const DataRef *dataRef,
                               const std::vector<EncodedReg> &sourceOperands,
                               const std::vector<EncodedReg> &producerSources,
                               const std::vector<EncodedReg> &destinations,
                               bool internalBranch, bool internalTaken)
{
    recordAddressSpace(state, inst.addressSpaceId);
    std::array<uint64_t, 4> distances;
    std::array<uint8_t, 4> classes;
    state.dependencies.producerFacts(producerSources, distances, classes);

    const bool is_memory = dataRef &&
        isValidDataRef(dataRef->vaddr, dataRef->size);
    const Addr vaddr = is_memory ? dataRef->vaddr : 0;
    const Addr paddr = is_memory ? dataRef->paddr : 0;
    const uint64_t size = is_memory ? dataRef->size : 0;
    const bool crossPage = is_memory &&
        (vaddr & (kFastSimPageSize - 1)) + size > kFastSimPageSize;
    const bool branch = micro->isControl() ||
        (inst.is_control && microPc + 1 == microCount);
    const bool taken = internalBranch ? internalTaken : branch && inst.taken;
    const bool conditional = internalBranch || (branch && inst.is_cond);
    fatal_if(branch && inst.actual_next == 0,
             "branch at %#x has no actual retired successor", inst.pc);
    fastsim::TraceRecord record;
    record.pc = inst.pc;
    record.address = paddr;
    const Addr branchNext = internalBranch ? inst.pc : inst.actual_next;
    record.target = taken ? branchNext : 0;
    record.next_pc = branch ? branchNext : 0;
    for (size_t index = 0; index < distances.size(); ++index) {
        fatal_if(distances[index] > UINT32_MAX,
                 "producer distance overflow at pc=%#x", inst.pc);
        record.producer_dists[index] = static_cast<uint32_t>(distances[index]);
    }
    fatal_if(size > UINT16_MAX, "memory size overflow at pc=%#x", inst.pc);
    record.size = static_cast<uint16_t>(size);
    const auto setFlag = [&record](fastsim::TraceFlag flag, bool value) {
        if (value) record.flags = record.flags | flag;
    };
    setFlag(fastsim::kLoad, is_memory && !dataRef->is_store);
    setFlag(fastsim::kStore, is_memory && dataRef->is_store);
    setFlag(fastsim::kAtomic, is_memory &&
            ((dataRef->attributes & kQemuMemAttrAtomic) ||
             micro->isAtomic()));
    setFlag(fastsim::kPhysicalAddress, is_memory);
    setFlag(fastsim::kBranch, branch);
    setFlag(fastsim::kConditional, conditional);
    setFlag(fastsim::kIndirect,
            !internalBranch && branch && inst.is_indirect);
    setFlag(fastsim::kCall, !internalBranch && branch && inst.is_call);
    setFlag(fastsim::kReturn, !internalBranch && branch && inst.is_return);
    setFlag(fastsim::kTaken, taken);
    setFlag(fastsim::kMicroOp, micro->isMicroop());
    setFlag(fastsim::kLastMicroOp, micro->isLastMicroop());
    setFlag(fastsim::kSerialize, micro->isSerializing());
    setFlag(fastsim::kBranchOutcomeValid, branch);
    record.op_class = static_cast<int16_t>(micro->opClass());
    fatal_if(!inst.userMode,
             "user-only lowering received a non-CPL3 macro at pc=%#x",
             inst.pc);
    fatal_if(sourceOperands.size() > UINT8_MAX ||
                 destinations.size() > UINT8_MAX,
             "register count overflow at pc=%#x", inst.pc);
    record.n_src = static_cast<uint8_t>(sourceOperands.size());
    record.n_dst = static_cast<uint8_t>(destinations.size());
    std::array<uint8_t, fastsim::kTrackedRegisterClasses> destinationCounts{};
    for (const auto &[cls, index] : destinations) {
        (void)index;
        fatal_if(cls >= destinationCounts.size(),
                 "invalid destination register class at pc=%#x", inst.pc);
        fatal_if(destinationCounts[cls] == 31,
                 "destination class count overflow at pc=%#x", inst.pc);
        ++destinationCounts[cls];
    }
    record.set_register_class_metadata(classes, destinationCounts);
    if (is_memory) {
        const auto token = dataRef->virtual_page_token;
        fatal_if(!crossPage && (token == 0 ||
                 token >= fastsim::kDestinationClassCountsMarker),
                 "invalid virtual page token at pc=%#x vaddr=%#x "
                 "paddr=%#x size=%" PRIu64 " token=%u",
                 inst.pc, vaddr, paddr, size, token);
        if (!crossPage) {
            record.reserved = fastsim::kDestinationClassCountsMarker | token;
            record.flags = record.flags | fastsim::kVirtualPageToken;
        }
    }
    try {
        state.emission.writer->append(record);
    } catch (const std::exception &error) {
        fatal("failed writing FastSim FST record core=%" PRIu64 ": %s",
              state.logicalCoreId, error.what());
    }
    retireRecord(state, inst.userMode, &destinations);
}

void
QemuFstConverter::retireRecord(
    ThreadState &state, bool userMode,
    const std::vector<EncodedReg> *destinations)
{
    ++state.emission.recordCount;
    if (state.measurementActive && userMode) {
        ++state.emission.measurementUserRecordCount;
    } else {
        ++state.emission.warmupRecordCount;
    }
    if (destinations) {
        state.dependencies.retire(*destinations);
    } else {
        state.dependencies.retire();
    }
}

void
QemuFstConverter::completeInstruction(ThreadState &state, bool userMode)
{
    if (!state.measurementActive) {
        ++state.emission.warmupInstructionCount;
        return;
    }
    ++state.emission.measurementInstructionCount;
    if (!userMode) {
        return;
    }
    if (state.emission.measurementUserRecordCount >= minUserUops) {
        state.emission.measurementUserRecordTargetReached = true;
    }
}

void
QemuFstConverter::writeSyscallRecord(
    ThreadState &state, const QemuPendingSyscall &syscall)
{
    fatal_if(syscall.argumentCount != fastsim::kMaximumSyscallArguments,
             "QEMU syscall entry metadata is incomplete at pc=%#x",
             syscall.pc);
    const bool requiresReturn =
        syscall.number == 9 || syscall.number == 11;
    fatal_if(requiresReturn && !syscall.returnValue,
             "QEMU mmap/munmap lacks a CPL3 return value at pc=%#x",
             syscall.pc);
    recordAddressSpace(state, syscall.addressSpaceId);
    openCoreOutput(state);

    fastsim::TraceRecord record;
    record.pc = syscall.pc;
    record.set_syscall_number(syscall.number);
    record.target = 0;
    record.next_pc = 0;
    record.flags = fastsim::kRetires | fastsim::kSerialize;
    record.op_class = fastsim::kSyscallOpClass;
    std::array<uint8_t, fastsim::kTrackedRegisterClasses> classes{};
    std::array<uint8_t, fastsim::kTrackedRegisterClasses> destinations{};
    classes.fill(255);
    record.set_register_class_metadata(classes, destinations);

    fastsim::SyscallMetadata metadata;
    metadata.number = syscall.number;
    metadata.arguments = syscall.arguments;
    metadata.argument_count = syscall.argumentCount;
    metadata.valid_fields =
        fastsim::kSyscallArgumentsValid;
    if (syscall.returnValue) {
        metadata.return_value_raw = *syscall.returnValue;
        metadata.valid_fields |= fastsim::kSyscallReturnValueValid |
            fastsim::kSyscallFailureValid;
        metadata.failed = syscall.failed;
    }
    if (syscall.errorNumber) {
        metadata.errno_value = *syscall.errorNumber;
        metadata.valid_fields |= fastsim::kSyscallErrnoValid;
    }
    try {
        state.emission.writer->append(record, &metadata);
    } catch (const std::exception &error) {
        fatal("failed writing FastSim syscall core=%" PRIu64 ": %s",
              state.logicalCoreId, error.what());
    }
    retireRecord(state, true, nullptr);
}

void
QemuFstConverter::finalizeOutput(ThreadState &state)
{
    if (!state.emission.writer) return;
    try {
        state.emission.writer->close();
        state.emission.writer.reset();
    } catch (const std::exception &error) {
        fatal("failed finalizing FastSim FST core=%" PRIu64 ": %s",
              state.logicalCoreId, error.what());
    }
}

void
QemuFstConverter::writeBoundaryFile() const
{
    const std::string path = outputDir + "/boundaries.json";
    std::ofstream out(path);
    fatal_if(!out, "failed to open QEMU-FST boundary output %s", path);
    out << "{\n  \"cores\": {\n";
    for (uint64_t core = 0; core < expectedNumCores; ++core) {
        const auto &state = threads.at(
            static_cast<size_t>(coreThreads.at(core) - 1));
        if (core != 0) {
            out << ",\n";
        }
        out << "    \"" << core << "\": {\n"
            << "      \"warmup_instructions\": "
            << state.emission.warmupInstructionCount << ",\n"
            << "      \"warmup_records\": "
            << state.emission.warmupRecordCount << ",\n"
            << "      \"measurement_instructions\": "
            << state.emission.measurementInstructionCount << ",\n"
            << "      \"measurement_records\": "
            << state.emission.measurementUserRecordCount << "\n"
            << "    }";
    }
    out << "\n  }\n}\n";
    fatal_if(!out, "failed to write QEMU-FST boundary output %s", path);
}

void
QemuFstConverter::emitInstruction(ThreadState &state, PendingInst &inst,
                                  const StaticInstPtr &macro)
{
    auto &lowering = staticLowering(inst);
    const auto &memoryOps = lowering.memoryPlan.operations;
    const bool hasAtomicEvidence = std::any_of(
        inst.refs.begin(), inst.refs.end(),
        [](const DataRef &ref) {
            return ref.attributes & kQemuMemAttrAtomic;
        });
    const bool hasSizeMismatch =
        memoryOps.size() == inst.refs.size() &&
        !std::equal(
            memoryOps.begin(), memoryOps.end(), inst.refs.begin(),
            [](const QemuStaticMemoryOp &micro, const DataRef &ref) {
                return micro.dataSize == ref.size;
            });
    const bool hasFragmentedEvidence =
        inst.refs.size() > memoryOps.size();
    if (lowering.requiresExecution || hasAtomicEvidence || hasSizeMismatch ||
        hasFragmentedEvidence) {
        ++dynamicMacroCount;
        dynamicInternalControlCount += lowering.requiresExecution;
        dynamicAtomicCount += hasAtomicEvidence;
        dynamicSizeMismatchCount += hasSizeMismatch;
        dynamicFragmentedEvidenceCount += hasFragmentedEvidence;
        ++lowering.dynamicExecutions;
        auto &ops = state.expandedMicroops;
        auto &boundRefs = state.boundRefs;
        bool allEvidenceConsumed = false;
        const bool completed = state.microcodeExecutor->execute(
            inst, state.userStates.at(inst.preStateSlot), macro,
            lowering.mnemonic,
            microopDescriptors, ops, boundRefs,
            &scalarSinglePaddingCount, allEvidenceConsumed);
        fatal_if(!completed, "gem5 microcode did not complete at pc=%#x",
                 inst.pc);
        dynamicMicroopCount += ops.size();
        dynamicMaximumMicroopCount = std::max(
            dynamicMaximumMicroopCount,
            static_cast<uint64_t>(ops.size()));
        size_t memoryOpCount = 0;
        size_t onlyMemoryOp = 0;
        for (size_t index = 0; index < ops.size(); ++index) {
            if (ops[index].hasDataRef()) {
                ++memoryOpCount;
                onlyMemoryOp = index;
            }
        }
        if (inst.refs.size() == 1 && memoryOpCount == 1) {
            auto &ref = boundRefs[ops[onlyMemoryOp].dataRefIndex];
            fatal_if(
                ref.is_store != inst.refs.front().is_store ||
                    bool(ref.attributes & kQemuMemAttrAtomic) !=
                        bool(inst.refs.front().attributes &
                             kQemuMemAttrAtomic),
                "dynamic memory operation disagrees with its sole QEMU "
                "callback at pc=%#x",
                inst.pc);
            ref = inst.refs.front();
            allEvidenceConsumed = true;
        }
        fatal_if(!allEvidenceConsumed,
                 "QEMU raw memory evidence was not fully consumed "
                 "pc=%#x mnemonic=%s callbacks=%zu memory_uops=%zu",
                 inst.pc, lowering.mnemonic.c_str(),
                 inst.refs.size(), memoryOpCount);
        openCoreOutput(state);
        for (size_t index = 0; index < ops.size(); ++index) {
            if (ops[index].hasDataRef()) {
                auto &ref = boundRefs[ops[index].dataRefIndex];
                resolveDynamicAddress(state, inst, ref);
            }
            const DataRef *ref = ops[index].hasDataRef()
                ? &boundRefs[ops[index].dataRefIndex] : nullptr;
            const auto &descriptor = *ops[index].descriptor;
            writeRecord(state, inst, descriptor.inst, index, ops.size(), ref,
                        descriptor.sources, descriptor.producerSources,
                        descriptor.destinations,
                        ops[index].internalBranch, ops[index].internalTaken);
        }
        return;
    }

    openCoreOutput(state);
    auto &boundRefs = state.boundRefs;
    QemuMemoryBinder::bindStatic(
        lowering.memoryPlan, inst.refs, boundRefs, inst.pc,
        lowering.mnemonic.c_str());
    size_t refIndex = 0;
    for (size_t index = 0; index < lowering.microops.size(); ++index) {
        const DataRef *ref = nullptr;
        if (refIndex < memoryOps.size() &&
            memoryOps[refIndex].microopIndex == index) {
            ref = &boundRefs[refIndex++];
        }
        const auto &micro = *lowering.microops[index];
        writeRecord(state, inst, micro.inst, index, lowering.microops.size(),
                    ref, micro.sources, micro.producerSources,
                    micro.destinations);
    }
}

void
QemuFstConverter::flushPending(ThreadState &state, Addr nextInstrPc)
{
    if (!state.havePending) {
        return;
    }
    if (state.emission.measurementUserRecordTargetReached) {
        state.pending.resetForInstruction();
        state.havePending = false;
        return;
    }
    state.pending.actual_next = nextInstrPc;
    if (state.pending.indirectTarget) {
        fatal_if(*state.pending.indirectTarget != nextInstrPc,
                 "QEMU indirect branch target mismatch pc=%#x "
                 "marker=%#x successor=%#x",
                 state.pending.pc, *state.pending.indirectTarget,
                 nextInstrPc);
    }
    const auto &lowering = staticMacro(state.pending);
    const auto macro = lowering.macro;
    const bool rawControl = state.pending.is_control;
    const bool rawConditional = state.pending.is_cond;
    const bool rawIndirect = state.pending.is_indirect;
    const bool rawCall = state.pending.is_call;
    const bool rawReturn = state.pending.is_return;
    fatal_if(
        !state.pending.isSyscallGateway &&
            ((macro->isControl() && !rawControl) ||
             (macro->isCondCtrl() && !rawConditional) ||
             (macro->isIndirectCtrl() && !rawIndirect) ||
             (macro->isCall() && !rawCall) ||
             (macro->isReturn() && !rawReturn)),
        "QEMU raw/gem5 branch classification mismatch at pc=%#x "
        "raw=(%u,%u,%u,%u,%u) gem5=(%u,%u,%u,%u,%u)",
        state.pending.pc, rawControl, rawConditional, rawIndirect, rawCall,
        rawReturn, macro->isControl(), macro->isCondCtrl(),
        macro->isIndirectCtrl(), macro->isCall(), macro->isReturn());
    state.pending.taken = state.pending.is_control &&
        nextInstrPc != state.pending.pc + state.pending.size;
    fatal_if(!state.pending.hasPreState,
             "QEMU instruction lacks its CPL3 pre-state at pc=%#x",
             state.pending.pc);
    if (state.pending.isSyscallGateway && state.pending.userMode) {
        fatal_if(!state.pending.refs.empty(),
                 "QEMU syscall gateway has memory evidence at pc=%#x",
                 state.pending.pc);
        fatal_if(state.pendingSyscall,
                 "QEMU syscall overlaps a prior unresolved syscall");
        const auto &preState = state.userStates.at(state.pending.preStateSlot);
        constexpr size_t kRax = 0;
        constexpr size_t kRdx = 2;
        constexpr size_t kRsi = 6;
        constexpr size_t kRdi = 7;
        constexpr size_t kR8 = 8;
        constexpr size_t kR9 = 9;
        constexpr size_t kR10 = 10;
        QemuPendingSyscall syscall;
        syscall.pc = state.pending.pc;
        syscall.addressSpaceId = state.pending.addressSpaceId;
        syscall.number = preState.gpr[kRax];
        syscall.arguments = {
            preState.gpr[kRdi],
            preState.gpr[kRsi],
            preState.gpr[kRdx],
            preState.gpr[kR10],
            preState.gpr[kR8],
            preState.gpr[kR9],
        };
        syscall.argumentCount = fastsim::kMaximumSyscallArguments;
        state.pendingSyscall = std::move(syscall);
        state.havePending = false;
        return;
    }
    emitInstruction(state, state.pending, macro);
    completeInstruction(state, state.pending.userMode);
    state.havePending = false;
}

Addr
QemuFstConverter::pendingSuccessorAtBoundary(ThreadState &state)
{
    const auto &pending = state.pending;
    if (pending.indirectTarget) {
        return *pending.indirectTarget;
    }
    const Addr fallthrough = pending.pc + pending.size;
    if (!pending.is_control || !pending.taken) {
        return fallthrough;
    }
    const auto &lowering = staticLowering(pending);
    for (auto iterator = lowering.microops.rbegin();
         iterator != lowering.microops.rend(); ++iterator) {
        const auto &micro = (*iterator)->inst;
        if (!micro->isDirectCtrl()) {
            continue;
        }
        PCState pc(pending.pc);
        pc.npc(fallthrough);
        pc.size(static_cast<uint8_t>(pending.size));
        return micro->branchTarget(pc)->instAddr();
    }
    fatal("QEMU direct branch has no gem5 target at pc=%#x", pending.pc);
}

void
QemuFstConverter::handleInstruction(
    ThreadState &state, uint16_t rawType, Addr pc, uint16_t size,
    const uint8_t *encoding)
{
    const auto type = static_cast<trace_type_t>(rawType);
    if (state.emission.measurementUserRecordTargetReached) {
        return;
    }
    flushPending(state, pc);
    fatal_if(!state.userStates.hasCompleteState(),
             "QEMU instruction has no preceding CPL3 state pc=%#x",
             pc);
    fatal_if(state.currentAddressSpaceId == 0,
             "QEMU instruction has no address-space identity pc=%#x",
             pc);
    if (state.pendingSyscall) {
        constexpr size_t kRax = 0;
        auto &syscall = *state.pendingSyscall;
        syscall.returnValue = state.userStates.incoming().gpr[kRax];
        const int64_t signedResult = static_cast<int64_t>(
            *syscall.returnValue);
        syscall.failed = signedResult >= -4095 && signedResult < 0;
        if (syscall.failed) {
            syscall.errorNumber = static_cast<uint32_t>(-signedResult);
        }
        writeSyscallRecord(state, syscall);
        completeInstruction(state, true);
        state.pendingSyscall.reset();
    }
    state.pending.resetForInstruction();
    auto &pending = state.pending;
    pending.pc = pc;
    pending.size = size;
    pending.userMode = true;
    pending.addressSpaceId = state.currentAddressSpaceId;
    fatal_if(pending.size > pending.bytes.size(),
             "x86 instruction at %#x has unsupported encoding length %zu",
             pending.pc, pending.size);
    std::memcpy(pending.bytes.data(), encoding, pending.size);
    pending.is_control = isControl(type);
    pending.is_cond = isCond(type);
    pending.is_indirect =
        type ==
            dynamorio::drmemtrace::TRACE_TYPE_INSTR_INDIRECT_JUMP ||
        type ==
            dynamorio::drmemtrace::TRACE_TYPE_INSTR_INDIRECT_CALL ||
        type == dynamorio::drmemtrace::TRACE_TYPE_INSTR_RETURN;
    pending.is_call =
        type ==
            dynamorio::drmemtrace::TRACE_TYPE_INSTR_DIRECT_CALL ||
        type ==
            dynamorio::drmemtrace::TRACE_TYPE_INSTR_INDIRECT_CALL;
    pending.is_return =
        type == dynamorio::drmemtrace::TRACE_TYPE_INSTR_RETURN;
    pending.isSyscallGateway =
        type ==
            dynamorio::drmemtrace::TRACE_TYPE_INSTR_SYSENTER;
    if (state.pendingBranchTarget) {
        pending.indirectTarget = *state.pendingBranchTarget;
        state.pendingBranchTarget.reset();
    }
    pending.taken = isTaken(type);
    pending.actual_next = 0;
    pending.preStateSlot = state.userStates.consumeIncoming();
    pending.hasPreState = true;
    if (!state.roiActive) {
        return;
    }
    state.havePending = true;
}

QemuFstConverter::DataRef
QemuFstConverter::translateAddress(
    ThreadState &state, Addr vaddr, uint64_t size, bool isStore)
{
    const uint32_t attributes = state.pendingMemoryAttributes;
    state.pendingMemoryAttributes = 0;
    fatal_if(attributes & (kQemuMemAttrPio | kQemuMemAttrMmio),
             "user-only conversion does not accept PIO/MMIO at pc=%#x",
             state.pending.pc);
    fatal_if(attributes & ~(kQemuMemAttrAtomic | kQemuMemAttrPio |
                            kQemuMemAttrMmio),
             "QEMU memory attributes are invalid=%#x", attributes);
    fatal_if(!state.pendingMemoryPhysicalAddress,
             "QEMU memory reference lacks a physical address at pc=%#x "
             "vaddr=%#x", state.pending.pc, vaddr);
    const Addr paddr = *state.pendingMemoryPhysicalAddress;
    state.pendingMemoryPhysicalAddress.reset();
    fatal_if(!state.logicalCoreIdValid,
             "DR memory reference has no logical-core binding tid=%" PRId64,
             state.drThreadId);
    const int64_t addressSpaceId = static_cast<int64_t>(state.pending.addressSpaceId);
    fatal_if(addressSpaceId <= 0,
             "memory reference has no address-space identity at pc=%#x",
             state.pending.pc);
    const auto resolution = state.addresses.observe(
        vaddr, paddr, size, state.emission.recordCount, state.pending.pc);
    if (resolution.newMapping) {
        openCoreOutput(state);
        try {
            state.emission.writer->register_virtual_page_mapping(
                *resolution.newMapping);
        } catch (const std::exception &error) {
            fatal("failed registering FST virtual page core=%" PRIu64 ": %s",
                  state.logicalCoreId, error.what());
        }
    }
    return {
        isStore,
        vaddr,
        resolution.physicalAddress,
        resolution.virtualPage,
        resolution.token,
        size,
        attributes,
    };
}

void
QemuFstConverter::resolveDynamicAddress(
    ThreadState &state, PendingInst &inst, DataRef &ref)
{
    const auto resolution = state.addresses.resolve(
        ref.vaddr, ref.paddr, ref.size, state.emission.recordCount, inst.pc);
    if (resolution.newMapping) {
        openCoreOutput(state);
        try {
            state.emission.writer->register_virtual_page_mapping(
                *resolution.newMapping);
        } catch (const std::exception &error) {
            fatal("failed registering FST virtual page core=%" PRIu64 ": %s",
                  state.logicalCoreId, error.what());
        }
    }
    ref.paddr = resolution.physicalAddress;
    ref.virtual_page = resolution.virtualPage;
    ref.virtual_page_token = resolution.token;
}

void
QemuFstConverter::handleData(
    ThreadState &state, Addr address, uint16_t size, bool isStore)
{
    if (state.emission.measurementUserRecordTargetReached ||
        !state.roiActive) {
        return;
    }
    const auto physical = translateAddress(state, address, size, isStore);
    fatal_if(!state.havePending,
             "data reference appears before instruction for DR thread %" PRId64,
             state.drThreadId);
    fatal_if(!isValidDataRef(address, size),
             "invalid data reference at pc=%#x addr=%#x size=%zu",
             state.pending.pc, address, size_t(size));
    state.pending.refs.push_back(physical);
}

void
QemuFstConverter::handleMarker(
    ThreadState &state, uint16_t markerType, uint64_t markerValue)
{
    using namespace dynamorio::drmemtrace;
    if (state.emission.measurementUserRecordTargetReached) {
        if (markerType == kQemuMarkerRoiBoundary &&
            markerValue == kQemuRoiBoundaryEnd) {
            fatal_if(!state.roiActive || state.roiCompleted ||
                         state.roiBeginMarkers != 1,
                     "QEMU-FST native ROI end has no matching begin marker");
            state.pending.resetForInstruction();
            state.havePending = false;
            state.roiActive = false;
            state.measurementActive = false;
            state.roiCompleted = true;
            ++state.roiEndMarkers;
        }
        return;
    }
    if (markerType == TRACE_MARKER_TYPE_CPU_ID) {
        fatal_if(markerValue > UINT32_MAX,
                 "QEMU logical CPU ID exceeds FST width");
        bindQemuWindowedCore(state, markerValue);
        return;
    }
    if (markerType == TRACE_MARKER_TYPE_TIMESTAMP) {
        return;
    }
    if (markerType == kQemuMarkerPrivilegeLevel) {
        const auto cpl = markerValue;
        fatal_if(cpl != 3,
                 "user-only QEMU raw trace contains CPL=%" PRIu64, cpl);
        return;
    }
    if (markerType == kQemuMarkerRoiBoundary) {
        const auto boundary = markerValue;
        if (boundary == kQemuRoiBoundaryBegin) {
            fatal_if(!state.logicalCoreIdValid,
                     "QEMU-FST native ROI begins before CPU binding");
            fatal_if(state.roiActive || state.roiCompleted ||
                         state.roiBeginMarkers != 0,
                     "QEMU-FST native ROI has multiple begin markers");
            state.roiActive = true;
            ++state.roiBeginMarkers;
            return;
        }
        if (boundary == kQemuRoiBoundaryMeasurement) {
            fatal_if(!state.roiActive || state.measurementActive ||
                         state.measurementBeginMarkers != 0,
                     "QEMU-FST native measurement boundary has no matching "
                     "warmup interval");
            if (state.havePending) {
                flushPending(state, pendingSuccessorAtBoundary(state));
            }
            state.measurementActive = true;
            ++state.measurementBeginMarkers;
            return;
        }
        if (boundary == kQemuRoiBoundaryEnd) {
            fatal_if(!state.roiActive || state.roiCompleted ||
                         state.roiBeginMarkers != 1,
                     "QEMU-FST native ROI end has no matching begin marker");
            state.pending.resetForInstruction();
            state.havePending = false;
            state.roiActive = false;
            state.measurementActive = false;
            state.roiCompleted = true;
            ++state.roiEndMarkers;
            return;
        }
        fatal("QEMU-FST ROI boundary marker has invalid value=%" PRIu64,
              boundary);
    }
    if (markerType == kQemuMarkerStructuredEvent) {
        const uint64_t value = markerValue;
        fatal_if(value >> 53,
                 "QEMU structured event has reserved bits set=%#" PRIx64,
                 value);
        const Addr pc = value & qemu_trace_extensions::kStructuredEventPcMask;
        const auto kind = static_cast<
            qemu_trace_extensions::StructuredEventKind>(
                (value >> qemu_trace_extensions::kStructuredEventKindShift)
                & 0x7);
        const auto disposition = static_cast<
            qemu_trace_extensions::StructuredEventDisposition>(
                (value >> qemu_trace_extensions::
                 kStructuredEventDispositionShift) & 0x3);
        fatal_if(kind != qemu_trace_extensions::StructuredEventKind::
                     kDiscontinuity &&
                     kind != qemu_trace_extensions::StructuredEventKind::
                     kSyscall,
                 "QEMU structured event has unknown kind=%u",
                 unsigned(kind));
        fatal_if(disposition != qemu_trace_extensions::
                     StructuredEventDisposition::kBetweenInstructions &&
                     disposition != qemu_trace_extensions::
                     StructuredEventDisposition::kRetiredTransfer &&
                     disposition != qemu_trace_extensions::
                     StructuredEventDisposition::kUnretiredFault,
                 "QEMU structured event has unknown disposition=%u",
                 unsigned(disposition));
        if (disposition == qemu_trace_extensions::
                StructuredEventDisposition::kUnretiredFault) {
            state.pending.resetForInstruction();
            state.havePending = false;
            return;
        }
        if (disposition == qemu_trace_extensions::
                StructuredEventDisposition::kBetweenInstructions) {
            if (state.havePending) {
                flushPending(state, pendingSuccessorAtBoundary(state));
            }
            return;
        }
        fatal_if(!state.havePending,
                 "QEMU structured event has no open CPL3 candidate");
        if (kind == qemu_trace_extensions::StructuredEventKind::kSyscall) {
            fatal_if(disposition != qemu_trace_extensions::
                         StructuredEventDisposition::kRetiredTransfer ||
                         !state.pending.isSyscallGateway,
                     "QEMU syscall disposition does not close a syscall "
                     "candidate");
            flushPending(state, pc);
            return;
        }
        fatal("QEMU CPL3 candidate transferred without syscall disposition "
              "pc=%#x", state.pending.pc);
        return;
    }
    if (markerType == kQemuMarkerUserStateBegin) {
        fatal_if(markerValue !=
                     qemu_trace_extensions::kUserStateFieldCount,
                 "QEMU CPL3 state has invalid field count=%" PRIu64,
                 markerValue);
        state.userStates.begin(
            state.havePending
                ? std::optional<uint8_t>(state.pending.preStateSlot)
                : std::nullopt);
        return;
    }
    if (markerType == kQemuMarkerAddressSpace) {
        const uint64_t asid = markerValue;
        fatal_if(asid == 0, "QEMU CPL3 macro has a zero ASID");
        state.userStates.complete();
        if (!state.fixedAddressSpaceId) {
            state.fixedAddressSpaceId = asid;
        } else {
            fatal_if(*state.fixedAddressSpaceId != asid,
                     "QEMU user-only shard changed ASID from %#" PRIx64
                     " to %#" PRIx64,
                     *state.fixedAddressSpaceId, asid);
        }
        state.currentAddressSpaceId = asid;
        return;
    }
    if (markerType == kQemuMarkerMemoryAttributes) {
        if (state.emission.measurementUserRecordTargetReached) {
            return;
        }
        const uint64_t attributes = markerValue;
        fatal_if(attributes & ~(kQemuMemAttrAtomic | kQemuMemAttrPio |
                                kQemuMemAttrMmio),
                 "QEMU memory attributes are invalid=%#" PRIx64, attributes);
        fatal_if(state.pendingMemoryAttributes != 0,
                 "QEMU memory attributes are not followed by a memref");
        state.pendingMemoryAttributes = static_cast<uint32_t>(attributes);
        return;
    }
    if (markerType == kQemuMarkerMemoryPhysicalAddress) {
        fatal_if(!state.havePending,
                 "QEMU physical address has no pending CPL3 macro");
        fatal_if(state.pendingMemoryPhysicalAddress,
                 "QEMU physical address is not followed by one memref");
        state.pendingMemoryPhysicalAddress = markerValue;
        return;
    }
    if (markerType == TRACE_MARKER_TYPE_BRANCH_TARGET) {
        if (state.emission.measurementUserRecordTargetReached) {
            return;
        }
        fatal_if(state.pendingBranchTarget,
                 "duplicate QEMU branch-target marker");
        state.pendingBranchTarget = markerValue;
        return;
    }
    if (markerType == TRACE_MARKER_TYPE_PHYSICAL_ADDRESS ||
        markerType == TRACE_MARKER_TYPE_VIRTUAL_ADDRESS ||
        markerType == TRACE_MARKER_TYPE_PAGE_SIZE ||
        markerType == TRACE_MARKER_TYPE_PHYSICAL_ADDRESS_NOT_AVAILABLE) {
        fatal("user-only QEMU raw trace contains legacy page marker=%u",
              unsigned(markerType));
    }
    fatal("QEMU raw trace contains unknown marker=%u value=%#" PRIx64,
          unsigned(markerType), markerValue);
}

void
QemuFstConverter::closeOutputs()
{
    for (auto &state : threads) {
        finalizeOutput(state);
    }
}

void
QemuFstConverter::writeDynamicTelemetry() const
{
    std::cout << "qemu-fst dynamic_macros=" << dynamicMacroCount
              << " internal_control=" << dynamicInternalControlCount
              << " atomic=" << dynamicAtomicCount
              << " size_mismatch=" << dynamicSizeMismatchCount
              << " fragmented_evidence=" << dynamicFragmentedEvidenceCount
              << " microops=" << dynamicMicroopCount
              << " max_microops=" << dynamicMaximumMicroopCount
              << " scalar_single_padding=" << scalarSinglePaddingCount
              << '\n';
    std::map<std::string, uint64_t> mnemonicCounts;
    for (const auto &[key, lowering] : staticLowerings) {
        (void)key;
        if (lowering.dynamicExecutions != 0) {
            mnemonicCounts[lowering.mnemonic] += lowering.dynamicExecutions;
        }
    }
    for (const auto &[mnemonic, count] : mnemonicCounts) {
        std::cout << "qemu-fst dynamic_mnemonic=" << mnemonic
                  << " count=" << count << '\n';
    }
}

void
QemuFstConverter::convert()
{
    convertQemuWindowedTrace();
}

void
QemuFstConverter::convertQemuWindowedTrace()
{
    using namespace dynamorio::drmemtrace;
    const auto files = QemuRawTraceReader::discover(inputTrace);
    threads.reserve(files.size());
    for (size_t fileIndex = 0; fileIndex < files.size(); ++fileIndex) {
        const auto &path = files[fileIndex];
        rawCapabilities = {};
        currentQemuShardKey = static_cast<int64_t>(fileIndex + 1);
        threads.emplace_back();
        currentThreadState = &threads.back();
        QemuRawTraceReader reader(path);
        QemuMacroAssembler assembler;
        QemuRawTraceReader::Block block;
        while (reader.nextBlock(block)) {
            for (const trace_entry_t &record : block) {
            const auto type = static_cast<trace_type_t>(record.type);
            if (rawCapabilities.hasFooter) {
                fatal("QEMU-FST trace has records after footer: %s",
                      path.c_str());
            }
            if (type == TRACE_TYPE_HEADER) {
                fatal_if(rawCapabilities.hasHeader,
                         "QEMU-FST trace has duplicate header: %s",
                         path.c_str());
                fatal_if(record.addr != 7,
                         "QEMU-FST raw header version must be 7, observed=%#x",
                         record.addr);
                rawCapabilities.hasHeader = true;
                continue;
            }
            if (type == TRACE_TYPE_FOOTER) {
                rawCapabilities.hasFooter = true;
                continue;
            }
            if (type == TRACE_TYPE_THREAD) {
                assembler.threadId = static_cast<int64_t>(record.addr);
                fatal_if(assembler.threadId < 0,
                         "QEMU-FST trace has invalid thread id in %s",
                         path.c_str());
                assembler.hasThreadId = true;
                continue;
            }
            if (type == TRACE_TYPE_PID) {
                const int64_t processId =
                    static_cast<int64_t>(record.addr);
                fatal_if(processId < 0,
                         "QEMU-FST trace has invalid process id in %s",
                         path.c_str());
                fatal_if(!assembler.hasThreadId,
                         "QEMU-FST process id precedes thread identity in %s",
                         path.c_str());
                assembler.hasProcessId = true;
                if (!currentThreadState->identityInitialized) {
                    currentThreadState->identityInitialized = true;
                    currentThreadState->drThreadId = assembler.threadId;
                } else {
                    fatal_if(
                        currentThreadState->drThreadId != assembler.threadId,
                             "QEMU-FST raw shard changed thread identity from %"
                             PRId64 " to %" PRId64,
                             currentThreadState->drThreadId,
                             assembler.threadId);
                }
                if (assembler.hasPendingCpu) {
                    handleMarker(
                        *currentThreadState, TRACE_MARKER_TYPE_CPU_ID,
                        assembler.pendingCpu);
                    assembler.hasPendingCpu = false;
                }
                if (assembler.hasPendingPrivilege) {
                    handleMarker(
                        *currentThreadState, kQemuMarkerPrivilegeLevel,
                        assembler.pendingPrivilege);
                    assembler.hasPendingPrivilege = false;
                }
                continue;
            }
            if (type == TRACE_TYPE_ENCODING) {
                assembler.appendEncoding(record, path);
                continue;
            }
            if (type == TRACE_TYPE_MARKER) {
                if (record.size == TRACE_MARKER_TYPE_VERSION) {
                    fatal_if(record.addr != 7,
                             "QEMU-FST trace marker version must be 7, "
                             "observed=%#x", record.addr);
                    rawCapabilities.hasVersion = true;
                    rawCapabilities.version = record.addr;
                    continue;
                }
                if (record.size == TRACE_MARKER_TYPE_FILETYPE) {
                    recordRawFiletype(record.addr);
                    continue;
                }
                if (record.size == TRACE_MARKER_TYPE_CACHE_LINE_SIZE ||
                    record.size == TRACE_MARKER_TYPE_CHUNK_INSTR_COUNT) {
                    continue;
                }
                if (record.size == TRACE_MARKER_TYPE_CPU_ID &&
                    !assembler.hasProcessId) {
                    assembler.pendingCpu = static_cast<uint64_t>(record.addr);
                    assembler.hasPendingCpu = true;
                    continue;
                }
                if (record.size == kQemuMarkerPrivilegeLevel &&
                    !assembler.hasProcessId) {
                    assembler.pendingPrivilege =
                        static_cast<uint64_t>(record.addr);
                    assembler.hasPendingPrivilege = true;
                    continue;
                }
                if (record.size == TRACE_MARKER_TYPE_TIMESTAMP &&
                    !assembler.hasProcessId) {
                    continue;
                }
                if (!assembler.hasProcessId) {
                    fatal("QEMU-FST marker=%u precedes thread identity in %s",
                          unsigned(record.size), path.c_str());
                }
                handleMarker(
                    *currentThreadState, record.size, record.addr);
                continue;
            }
            if (type == kQemuTraceTypeUserState) {
                fatal_if(!assembler.hasProcessId,
                         "QEMU CPL3 state precedes thread identity in %s",
                         path.c_str());
                auto &state = *currentThreadState;
                if (state.emission.measurementUserRecordTargetReached) {
                    continue;
                }
                state.userStates.set(record.size, record.addr);
                continue;
            }
            if (isInstr(type)) {
                fatal_if(!assembler.hasProcessId,
                         "QEMU instruction precedes thread identity in %s",
                         path.c_str());
                const uint8_t *encoding =
                    assembler.finishEncoding(record, path);
                handleInstruction(
                    *currentThreadState, record.type, record.addr,
                    record.size, encoding);
                continue;
            }
            if (type == TRACE_TYPE_THREAD_EXIT) {
                fatal_if(
                    !assembler.hasProcessId ||
                        static_cast<int64_t>(record.addr) !=
                            assembler.threadId,
                         "QEMU-FST thread exit identity mismatch in %s",
                         path.c_str());
                continue;
            }
            if (isRead(type) || isWrite(type) || isPrefetch(type)) {
                fatal_if(!assembler.hasProcessId,
                         "QEMU memory reference precedes thread identity in %s",
                         path.c_str());
                if (!isPrefetch(type)) {
                    handleData(
                        *currentThreadState, record.addr, record.size,
                        isWrite(type));
                }
                continue;
            }
            if (type == kQemuTraceTypeValuePart ||
                type == kQemuTraceTypeValueFull) {
                auto &state = *currentThreadState;
                if (state.emission.measurementUserRecordTargetReached ||
                    !state.roiActive) {
                    continue;
                }
                fatal_if(!state.havePending || state.pending.refs.empty(),
                         "QEMU memory value has no preceding memory reference");
                auto &ref = state.pending.refs.back();
                const size_t offset =
                    type == kQemuTraceTypeValuePart ? 0 :
                    ref.valueSize == 8 && ref.size > 8 ? 8 : 0;
                const size_t bytes = std::min<size_t>(
                    sizeof(record.addr), ref.size - offset);
                fatal_if(ref.size > ref.value.size() ||
                             offset + bytes > ref.value.size(),
                         "QEMU memory value exceeds supported width at pc=%#x",
                         state.pending.pc);
                std::memcpy(ref.value.data() + offset, &record.addr, bytes);
                ref.valueSize = static_cast<uint8_t>(offset + bytes);
                ref.hasValue = type == kQemuTraceTypeValueFull;
                continue;
            }
            fatal("QEMU-FST raw trace contains unknown record type=%u in %s",
                  unsigned(record.type), path.c_str());
            }
        }
        validateRawCapabilities();
        fatal_if(!currentThreadState->logicalCoreIdValid,
                 "QEMU-FST raw shard has no logical core binding: %s",
                 path.c_str());
        currentThreadState = nullptr;
    }
    finishConversion();
}

void
QemuFstConverter::finishConversion()
{
    for (auto &state : threads) {
        const int64_t tid = state.drThreadId;
        finalizeQemuWindowedRoi(state);
        fatal_if(state.userStates.packetActive() ||
                     state.userStates.hasCompleteState(),
                 "QEMU CPL3 macro preamble is incomplete for thread %" PRId64,
                 tid);
        fatal_if(state.pendingMemoryPhysicalAddress,
                 "QEMU physical address has no following memref for thread %"
                 PRId64, tid);
        fatal_if(state.pendingMemoryAttributes != 0,
                 "QEMU memory attributes have no following memref for "
                 "thread %" PRId64, tid);
        fatal_if(state.roiActive,
                 "DR thread %" PRId64 " ended inside ROI", tid);
        fatal_if(state.roiBeginMarkers != state.roiEndMarkers,
                 "DR thread %" PRId64 " has unmatched ROI markers", tid);
        fatal_if(state.roiBeginMarkers > 1,
                 "DR thread %" PRId64 " has multiple ROI intervals", tid);
        if (state.pendingSyscall) {
            const auto number = state.pendingSyscall->number;
            fatal_if(number == 9 || number == 11,
                     "QEMU mmap/munmap ended without a CPL3 return value");
            writeSyscallRecord(state, *state.pendingSyscall);
            completeInstruction(state, true);
            state.pendingSyscall.reset();
        }
        if (state.logicalCoreIdValid) {
            fatal_if(
                state.emission.measurementUserRecordCount < minUserUops,
                "QEMU-FST core=%" PRIu64 " CPL3 UOP minimum was not reached: "
                "minimum=%" PRIu64 " actual=%" PRIu64,
                state.logicalCoreId,
                minUserUops,
                state.emission.measurementUserRecordCount);
            fatal_if(!state.emission.measurementUserRecordTargetReached,
                     "QEMU-FST core=%" PRIu64
                     " did not close on a complete measurement macro",
                     state.logicalCoreId);
        }
        if (state.roiBeginMarkers == 1) {
            fatal_if(state.havePending,
                     "DR thread %" PRId64 " has unfinished ROI instruction", tid);
            fatal_if(state.dependencies.empty(),
                     "DR thread %" PRId64 " produced an empty ROI", tid);
        }
    }
    for (uint64_t core = 0; core < expectedNumCores; ++core) {
        fatal_if(coreThreads[core] == 0,
                 "missing ROI for logical core %" PRIu64, core);
    }
    closeOutputs();
    writeBoundaryFile();
    writeDynamicTelemetry();
    exitSimLoop("x86 QEMU-FST conversion complete", 0);
}

} // namespace X86ISA
} // namespace gem5
