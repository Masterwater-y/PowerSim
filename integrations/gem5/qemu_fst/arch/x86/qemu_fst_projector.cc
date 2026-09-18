#include "arch/x86/qemu_fst_projector.hh"

#include <algorithm>
#include <cinttypes>
#include <numeric>

#include "arch/x86/regs/ccr.hh"
#include "arch/x86/regs/float.hh"
#include "arch/x86/regs/int.hh"
#include "arch/x86/pcstate.hh"
#include "arch/x86/qemu_microop_descriptor.hh"
#include "base/logging.hh"
#include "cpu/static_inst.hh"

namespace gem5
{
namespace X86ISA
{
namespace
{

bool
canonicalStaticRegister(const RegId &reg, uint32_t &canonical)
{
    if (reg.is(IntRegClass)) {
        const auto index = reg.index() & ~IntFoldBit;
        if (index >= int_reg::NumArchRegs) {
            return false;
        }
        canonical = index;
        return true;
    }
    if (reg.is(CCRegClass)) {
        canonical = 17;
        return true;
    }
    if (reg.is(FloatRegClass)) {
        if (reg.index() < float_reg::XmmBase) {
            canonical = 24 + reg.index();
            return true;
        }
        if (reg.index() < float_reg::MicrofpBase) {
            canonical = 32 + (reg.index() - float_reg::XmmBase) / 2;
            return true;
        }
        return false;
    }
    if (reg.is(VecRegClass) && reg.index() < 32) {
        canonical = 32 + reg.index();
        return true;
    }
    return false;
}

void
accumulateStaticOperands(
    const StaticInstPtr &instruction,
    std::array<uint64_t, fastsim::kStaticRegisterMaskWords> &reads,
    std::array<uint64_t, fastsim::kStaticRegisterMaskWords> &writes)
{
    for (int index = 0; index < instruction->numSrcRegs(); ++index) {
        uint32_t canonical = 0;
        if (!canonicalStaticRegister(
                instruction->srcRegIdx(index), canonical)) {
            continue;
        }
        const uint64_t bit = uint64_t{1} << (canonical % 64);
        if ((writes[canonical / 64] & bit) == 0) {
            reads[canonical / 64] |= bit;
        }
    }
    for (int index = 0; index < instruction->numDestRegs(); ++index) {
        uint32_t canonical = 0;
        if (!canonicalStaticRegister(
                instruction->destRegIdx(index), canonical)) {
            continue;
        }
        writes[canonical / 64] |= uint64_t{1} << (canonical % 64);
    }
}

}

FstProjector::FstProjector(
    uint32_t coreId, const std::string &outputDir)
    : core(coreId),
      writer(std::make_unique<fastsim::BinaryTraceWriter>(
          outputDir + "/core" + std::to_string(coreId) + ".fst",
          coreId, fastsim::SyscallAbi::kLinuxX86_64)),
      dependencies(
          int_reg::NumRegs, float_reg::NumRegs, 1,
          cc_reg::NumRegs)
{
    writer->enable_privilege_records();
    writer->enable_complete_dependencies();
}

FstProjector::~FstProjector()
{
}

bool
FstProjector::beginFrame(
    const QemuFrameEnvelope &envelope, const StaticInstPtr &macro,
    const std::string &mnemonic)
{
    const auto &transaction = envelope.frame;
    fatal_if(activeTransaction, "overlapping QEMU frames core=%u", core);
    activeTransaction = &transaction;
    activeMacro = macro;
    activeMnemonic = mnemonic;
    activeFrameExcluded =
        transaction.kind == QemuTransactionKind::Macro &&
        transaction.privilege() == 0 && envelope.idle;
    activeFrameIsGateway = envelope.syscall();
    activeFrameMeasurement =
        envelope.phase == QemuFramePhase::Measurement;
    activeFrameKernelUops = 0;
    activeStaticInstruction.reset();
    for (const auto &ref : transaction.refs) {
        if (ref.attributes &
            qemu_trace_extensions::kMemoryAttributeFrameRam) {
            counters.frameRamBytes += ref.size;
        }
    }
    addressSpaces.insert(transaction.addressSpaceId());
    if (transaction.kind == QemuTransactionKind::Macro &&
        transaction.userMode()) {
        completeSyscallIfPresent(transaction);
    }
    if (activeFrameIsGateway) {
        activeSyscallGateway.emplace();
        activeSyscallGateway->source = transaction;
    }
    if (activeFrameExcluded) {
        return false;
    }
    dependencies.beginTransaction();
    if (transaction.kind == QemuTransactionKind::Event) {
        const auto key =
            std::make_pair(transaction.addressSpaceId(), transaction.pc);
        taintedStaticInstructions.insert(key);
        staticInstructions.erase(key);
    } else {
        fastsim::StaticInstructionInfo info;
        info.pc = transaction.pc;
        info.size = static_cast<uint8_t>(transaction.size);
        info.fallthrough_pc = transaction.pc + transaction.size;
        info.operand_semantics_valid = true;
        activeStaticInstruction = info;
    }
    return true;
}

void
FstProjector::endFrame()
{
    fatal_if(!activeTransaction, "QEMU frame end without begin core=%u", core);
    const auto &transaction = *activeTransaction;
    counters.loweredKernelUopsBeforeIdleFilter += activeFrameKernelUops;
    if (activeFrameExcluded) {
        counters.excludedIdleKernelUops += activeFrameKernelUops;
    } else {
        counters.emittedKernelUops += activeFrameKernelUops;
    }
    if (activeFrameIsGateway) {
        fatal_if(!activeSyscallGateway,
                 "syscall frame has no executed gateway core=%u", core);
        beginSyscall(*activeSyscallGateway);
        activeSyscallGateway.reset();
    } else if (!activeFrameExcluded) {
        finishStaticInstruction();
        if (activeFrameMeasurement) {
            ++counters.measurementInstructions;
            if (transaction.userMode()) {
                ++counters.measurementUserInstructions;
            }
        } else {
            ++counters.warmupInstructions;
            if (transaction.userMode()) {
                ++counters.warmupUserInstructions;
            }
        }
    }
    activeTransaction = nullptr;
    activeMacro = nullptr;
    activeStaticInstruction.reset();
}

void
FstProjector::projectMicroop(
    const QemuMicroopDescriptor &descriptor,
    std::vector<QemuDataRef> &segments, bool eventMicroop,
    bool macroCompletion)
{
    fatal_if(!activeTransaction, "UOP outside QEMU frame core=%u", core);
    const auto &transaction = *activeTransaction;
    const bool kernel = transaction.kind == QemuTransactionKind::Event ||
        transaction.privilege() == 0 || eventMicroop;
    if (kernel) {
        ++activeFrameKernelUops;
    }
    if (activeFrameExcluded) {
        dependencies.forget(descriptor.destinations);
        return;
    }
    if (activeFrameIsGateway) {
        activeSyscallGateway->sources.insert(
            descriptor.producerSources.begin(),
            descriptor.producerSources.end());
        activeSyscallGateway->destinations.insert(
            descriptor.destinations.begin(), descriptor.destinations.end());
        return;
    }
    observeStaticMicroop(descriptor);

    const bool memory = !segments.empty();
    const bool pio = memory &&
        (segments.front().attributes &
         qemu_trace_extensions::kMemoryAttributePio);
    const bool mmio = memory &&
        (segments.front().attributes &
         qemu_trace_extensions::kMemoryAttributeMmio);
    fatal_if(pio && segments.size() != 1,
             "PIO micro-op has multiple segments core=%u pc=%#x",
             core, transaction.pc);
    if (memory) {
        const auto accessKind =
            segments.front().attributes &
            (qemu_trace_extensions::kMemoryAttributePio |
             qemu_trace_extensions::kMemoryAttributeMmio);
        fatal_if(
            std::any_of(
                segments.begin(), segments.end(),
                [ref = segments.front(), accessKind](
                    const QemuDataRef &segment) {
                    return segment.is_store != ref.is_store ||
                        (segment.attributes &
                         (qemu_trace_extensions::kMemoryAttributePio |
                          qemu_trace_extensions::kMemoryAttributeMmio)) !=
                            accessKind;
                }),
            "one microop has mixed memory segment semantics core=%u pc=%#x",
            core, transaction.pc);
    }
    if (memory && !pio) {
        for (auto &segment : segments) {
            const auto resolution = addresses.observe(
                transaction.addressSpaceId(), segment.vaddr, segment.paddr,
                segment.size, transaction.pc);
            segment.paddr = resolution.physicalAddress;
            segment.virtual_page = resolution.virtualPage;
            segment.virtual_page_token = resolution.token;
        }
    }
    if (currentAddressSpaceId != transaction.addressSpaceId()) {
        writer->set_address_space_id(transaction.addressSpaceId());
        currentAddressSpaceId = transaction.addressSpaceId();
    }

    const bool event = transaction.kind == QemuTransactionKind::Event;
    const bool originalMicroOpFlag =
        event || !activeMacro || activeMacro->isMacroop() ||
        descriptor.isMicroop;

    const bool branch =
        !event && macroCompletion && descriptor.isControl;
    const bool taken =
        branch && transaction.successor != transaction.pc + transaction.size;
    const QemuDataRef *ref = memory ? &segments.front() : nullptr;
    std::vector<uint64_t> &distances = producerDistancesScratch;
    std::array<uint8_t, 4> classes;
    classes.fill(255);
    dependencies.producerFacts(
        descriptor.producerSources, distances, classes);

    fastsim::TraceRecord record;
    record.pc = transaction.pc;
    if (memory) {
        const uint64_t totalSize = std::accumulate(
            segments.begin(), segments.end(), uint64_t{0},
            [](uint64_t total, const QemuDataRef &segment) {
                return total + segment.size;
            });
        fatal_if(totalSize == 0 || totalSize > UINT16_MAX,
                 "memory width overflow core=%u pc=%#x", core,
                 transaction.pc);
        record.address = pio ? ref->vaddr : ref->paddr;
        record.size = static_cast<uint16_t>(totalSize);
    }
    if (branch) {
        record.next_pc = transaction.successor;
        record.target = taken ? transaction.successor : 0;
    }
    for (size_t producer = 0;
         producer < std::min(distances.size(), record.producer_dists.size());
         ++producer) {
        fatal_if(distances[producer] > UINT32_MAX,
                 "producer distance overflow core=%u pc=%#x",
                 core, transaction.pc);
        record.producer_dists[producer] =
            static_cast<uint32_t>(distances[producer]);
    }
    const auto setFlag = [&record](fastsim::TraceFlag flag, bool value) {
        if (value) {
            record.flags = record.flags | flag;
        }
    };
    setFlag(fastsim::kLoad, memory && !ref->is_store);
    setFlag(fastsim::kStore, memory && ref->is_store);
    setFlag(fastsim::kAtomic, memory && descriptor.isAtomic);
    setFlag(fastsim::kPhysicalAddress, memory && !pio);
    setFlag(fastsim::kBranch, branch);
    setFlag(fastsim::kConditional, branch && descriptor.isConditionalControl);
    setFlag(fastsim::kIndirect, branch && descriptor.isIndirectControl);
    setFlag(fastsim::kCall, branch && descriptor.isCall);
    setFlag(fastsim::kReturn, branch && descriptor.isReturn);
    setFlag(fastsim::kTaken, taken);
    setFlag(fastsim::kMicroOp, originalMicroOpFlag);
    setFlag(fastsim::kLastMicroOp, macroCompletion);
    setFlag(fastsim::kSerialize, descriptor.isSerializing);
    setFlag(fastsim::kBranchOutcomeValid, branch);
    record.op_class = descriptor.opClass;
    record.set_kernel_mode(
        transaction.kind == QemuTransactionKind::Event ||
        !transaction.userMode() || eventMicroop);
    record.n_src = descriptor.sourceCount;
    record.n_dst = descriptor.destinationCount;
    record.set_register_class_metadata(
        classes, descriptor.destinationCounts);

    if (memory && !pio) {
        for (const auto &segment : segments) {
            fatal_if(segment.virtual_page_token == 0 ||
                         segment.virtual_page_token >=
                             fastsim::kDestinationClassCountsMarker,
                     "invalid virtual page token core=%u pc=%#x",
                     core, transaction.pc);
            const auto mapping = addresses.firstRecordMapping(
                transaction.addressSpaceId(), segment.vaddr, segment.paddr,
                segment.virtual_page_token,
                counters.warmupRecords + counters.measurementRecords,
                transaction.pc);
            if (mapping) {
                writer->register_virtual_page_mapping(*mapping);
            }
        }
        if (segments.size() == 1) {
            record.reserved =
                fastsim::kDestinationClassCountsMarker |
                ref->virtual_page_token;
            record.flags = record.flags | fastsim::kVirtualPageToken;
        }
    }
    auto &dependencyExtensions = dependencyExtensionsScratch;
    dependencyExtensions.clear();
    dependencyExtensions.reserve(
        distances.size() > record.producer_dists.size()
            ? distances.size() - record.producer_dists.size()
            : 0);
    for (size_t producer = record.producer_dists.size();
         producer < distances.size(); ++producer) {
        fatal_if(distances[producer] > UINT32_MAX,
                 "producer distance overflow core=%u pc=%#x",
                 core, transaction.pc);
        dependencyExtensions.push_back(
            static_cast<uint32_t>(distances[producer]));
    }
    const auto accessDomain = pio
        ? fastsim::AccessDomain::kPio
        : mmio ? fastsim::AccessDomain::kMmio
               : fastsim::AccessDomain::kRam;
    writer->append(
        record, nullptr, dependencyExtensions, accessDomain);
    if (activeFrameMeasurement) {
        ++counters.measurementRecords;
        if (!record.is_kernel()) {
            ++counters.measurementUserRecords;
        }
        if (memory && (ref->attributes &
                       qemu_trace_extensions::kMemoryAttributeMmio)) {
            ++counters.measurementMmioRecords;
        }
        if (pio) {
            ++counters.measurementPioRecords;
        }
    } else {
        ++counters.warmupRecords;
        if (!record.is_kernel()) {
            ++counters.warmupUserRecords;
        }
        if (mmio) {
            ++counters.warmupMmioRecords;
        }
        if (pio) {
            ++counters.warmupPioRecords;
        }
    }
    dependencies.retire(descriptor.destinations);
}

void
FstProjector::beginSyscall(const ActiveSyscallGateway &gatewayFrame)
{
    const auto &gateway = gatewayFrame.source;
    constexpr size_t Rax = 0;
    constexpr size_t Rdx = 2;
    constexpr size_t Rsp = 4;
    constexpr size_t Rsi = 6;
    constexpr size_t Rdi = 7;
    constexpr size_t R8 = 8;
    constexpr size_t R9 = 9;
    constexpr size_t R10 = 10;
    QemuPendingSyscall syscall;
    syscall.pc = gateway.pc;
    syscall.addressSpaceId = gateway.addressSpaceId();
    syscall.userStackPointer =
        gateway.state.get(qemu_trace_extensions::kStateGprBase + Rsp);
    fatal_if(gateway.pc > MaxAddr - gateway.size,
             "syscall return PC overflows core=%u pc=%#x", core, gateway.pc);
    syscall.expectedReturnPc = gateway.pc + gateway.size;
    syscall.recordOrdinal =
        counters.warmupRecords + counters.measurementRecords;
    syscall.measurementActive = activeFrameMeasurement;
    syscall.number =
        gateway.state.get(qemu_trace_extensions::kStateGprBase + Rax);
    syscall.arguments = {
        gateway.state.get(qemu_trace_extensions::kStateGprBase + Rdi),
        gateway.state.get(qemu_trace_extensions::kStateGprBase + Rsi),
        gateway.state.get(qemu_trace_extensions::kStateGprBase + Rdx),
        gateway.state.get(qemu_trace_extensions::kStateGprBase + R10),
        gateway.state.get(qemu_trace_extensions::kStateGprBase + R8),
        gateway.state.get(qemu_trace_extensions::kStateGprBase + R9),
    };
    syscall.argumentCount = fastsim::kMaximumSyscallArguments;
    const auto key = std::make_tuple(
        syscall.addressSpaceId, syscall.userStackPointer,
        syscall.expectedReturnPc);
    writeSyscall(syscall, false, &gatewayFrame);
    const auto ambiguous = ambiguousSyscalls.find(key);
    if (ambiguous != ambiguousSyscalls.end()) {
        ++ambiguous->second;
        return;
    }
    const auto existing = pendingSyscalls.find(key);
    if (existing == pendingSyscalls.end()) {
        pendingSyscalls.emplace(key, std::move(syscall));
        return;
    }
    pendingSyscalls.erase(existing);
    ambiguousSyscalls.emplace(key, 2);
}

void
FstProjector::completeSyscallIfPresent(const QemuTransaction &transaction)
{
    constexpr size_t Rax = 0;
    constexpr size_t Rsp = 4;
    const auto key = std::make_tuple(
        transaction.addressSpaceId(),
        transaction.state.get(qemu_trace_extensions::kStateGprBase + Rsp),
        transaction.pc);
    const auto ambiguous = ambiguousSyscalls.find(key);
    if (ambiguous != ambiguousSyscalls.end()) {
        if (--ambiguous->second == 0) {
            ambiguousSyscalls.erase(ambiguous);
        }
        return;
    }
    const auto found = pendingSyscalls.find(key);
    if (found == pendingSyscalls.end()) {
        return;
    }
    auto syscall = found->second;
    syscall.returnValue =
        transaction.state.get(qemu_trace_extensions::kStateGprBase + Rax);
    const int64_t result = static_cast<int64_t>(*syscall.returnValue);
    syscall.failed = result >= -4095 && result < 0;
    if (syscall.failed) {
        syscall.errorNumber = static_cast<uint32_t>(-result);
    }
    writeSyscall(syscall, true);
    pendingSyscalls.erase(found);
}

void
FstProjector::writeSyscall(
    const QemuPendingSyscall &syscall, bool update,
    const ActiveSyscallGateway *gateway)
{
    fastsim::SyscallMetadata metadata;
    metadata.number = syscall.number;
    metadata.arguments = syscall.arguments;
    metadata.argument_count = syscall.argumentCount;
    metadata.valid_fields = fastsim::kSyscallArgumentsValid;
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
    if (update) {
        metadata.record_ordinal = syscall.recordOrdinal;
        writer->update_syscall_metadata(metadata);
        return;
    }
    if (currentAddressSpaceId != syscall.addressSpaceId) {
        writer->set_address_space_id(syscall.addressSpaceId);
        currentAddressSpaceId = syscall.addressSpaceId;
    }
    fastsim::TraceRecord record;
    record.pc = syscall.pc;
    record.set_syscall_number(syscall.number);
    record.flags = fastsim::kRetires | fastsim::kSerialize;
    record.op_class = fastsim::kSyscallOpClass;
    fatal_if(!gateway || gateway->sources.empty(),
             "syscall marker lacks official lowering core=%u", core);
    // Collapse the executed microcode to its architectural inputs and outputs.
    // Reads satisfied inside this gateway and scratch registers must not link
    // to producers preceding the marker.
    std::set<QemuEncodedReg> inputs, outputs;
    const auto architectural = [](const QemuEncodedReg &reg) {
        const auto [kind, index] = reg;
        return (kind == 0 && index < int_reg::NumArchRegs) ||
            (kind == 1 && index < float_reg::MicrofpBase) ||
            (kind == 3 && index < cc_reg::_EcfIdx);
    };
    for (const auto &reg : gateway->sources) {
        if (architectural(reg) && !gateway->destinations.count(reg)) {
            inputs.insert(reg);
        }
    }
    for (const auto &reg : gateway->destinations) {
        if (architectural(reg)) outputs.insert(reg);
    }
    const std::vector<QemuEncodedReg> sources(inputs.begin(), inputs.end());
    const std::vector<QemuEncodedReg> destinations(outputs.begin(), outputs.end());
    dependencies.beginTransaction();
    std::vector<uint64_t> distances;
    std::array<uint8_t, 4> classes;
    dependencies.producerFacts(sources, distances, classes);
    for (size_t slot = 0;
         slot < std::min(distances.size(), record.producer_dists.size());
         ++slot) {
        fatal_if(distances[slot] > UINT32_MAX,
                 "syscall producer distance overflow core=%u", core);
        record.producer_dists[slot] = static_cast<uint32_t>(distances[slot]);
    }
    std::array<uint8_t, fastsim::kTrackedRegisterClasses> destinationCounts{};
    for (const auto &[kind, index] : destinations) {
        (void)index;
        fatal_if(destinationCounts[kind] == 31,
                 "syscall destination class overflow core=%u", core);
        ++destinationCounts[kind];
    }
    fatal_if(
        sources.size() > UINT8_MAX || destinations.size() > UINT8_MAX,
        "syscall operand count exceeds FST uint8 fields core=%u", core);
    record.n_src = static_cast<uint8_t>(sources.size());
    record.n_dst = static_cast<uint8_t>(destinations.size());
    record.set_register_class_metadata(classes, destinationCounts);
    auto &dependencyExtensions = dependencyExtensionsScratch;
    dependencyExtensions.clear();
    dependencyExtensions.reserve(
        distances.size() > record.producer_dists.size()
            ? distances.size() - record.producer_dists.size()
            : 0);
    for (size_t slot = record.producer_dists.size();
         slot < distances.size(); ++slot) {
        fatal_if(distances[slot] > UINT32_MAX,
                 "syscall producer distance overflow core=%u", core);
        dependencyExtensions.push_back(
            static_cast<uint32_t>(distances[slot]));
    }
    writer->append(record, &metadata, dependencyExtensions);
    if (syscall.measurementActive) {
        ++counters.measurementRecords;
        ++counters.measurementUserRecords;
        ++counters.measurementInstructions;
        ++counters.measurementUserInstructions;
    } else {
        ++counters.warmupRecords;
        ++counters.warmupInstructions;
        ++counters.warmupUserRecords;
        ++counters.warmupUserInstructions;
    }
    dependencies.retire(destinations);
}

void
FstProjector::observeStaticMicroop(
    const QemuMicroopDescriptor &descriptor)
{
    if (!activeStaticInstruction) {
        return;
    }
    auto &info = *activeStaticInstruction;
    const auto setFlag = [&info](
        fastsim::StaticInstructionFlag flag, bool enabled) {
        if (enabled) {
            info.flags = static_cast<uint16_t>(info.flags | flag);
        }
    };
    setFlag(fastsim::kStaticMemory, descriptor.isMemory);
    setFlag(fastsim::kStaticReadBarrier, descriptor.inst->isReadBarrier());
    setFlag(fastsim::kStaticWriteBarrier, descriptor.inst->isWriteBarrier());
    setFlag(fastsim::kStaticLockedRmw, descriptor.isAtomic);
    setFlag(fastsim::kStaticBranch, descriptor.inst->isControl());
    setFlag(fastsim::kStaticConditional, descriptor.inst->isCondCtrl());
    setFlag(fastsim::kStaticIndirect, descriptor.inst->isIndirectCtrl());
    setFlag(fastsim::kStaticCall, descriptor.inst->isCall());
    setFlag(fastsim::kStaticReturn, descriptor.inst->isReturn());
    accumulateStaticOperands(
        descriptor.inst, info.read_register_mask, info.write_register_mask);
    if (descriptor.inst->isDirectCtrl()) {
        PCState pc(activeTransaction->pc);
        pc.size(static_cast<uint8_t>(activeTransaction->size));
        pc.npc(activeTransaction->pc + activeTransaction->size);
        info.direct_target = descriptor.inst->branchTarget(pc)->instAddr();
        info.flags = static_cast<uint16_t>(
            info.flags | fastsim::kStaticDirectTargetValid);
    }
}

void
FstProjector::finishStaticInstruction()
{
    if (!activeStaticInstruction) {
        return;
    }
    const auto &transaction = *activeTransaction;
    const auto key =
        std::make_pair(transaction.addressSpaceId(), transaction.pc);
    if (taintedStaticInstructions.find(key) !=
        taintedStaticInstructions.end()) {
        return;
    }
    auto info = *activeStaticInstruction;
    if (activeStaticInstruction &&
        fastsim::has_static_instruction_flag(
            activeStaticInstruction->flags, fastsim::kStaticBranch) &&
        !fastsim::has_static_instruction_flag(
            activeStaticInstruction->flags, fastsim::kStaticIndirect)) {
        if (!fastsim::has_static_instruction_flag(
                info.flags, fastsim::kStaticDirectTargetValid)) {
            taintedStaticInstructions.insert(key);
            staticInstructions.erase(key);
            return;
        }
    }
    const auto [found, inserted] =
        staticInstructions.emplace(key, info);
    if (inserted) {
        return;
    }
    const auto &existing = found->second;
    if (existing.fallthrough_pc != info.fallthrough_pc ||
        existing.direct_target != info.direct_target ||
        existing.flags != info.flags ||
        existing.size != info.size ||
        existing.read_register_mask != info.read_register_mask ||
        existing.write_register_mask != info.write_register_mask) {
        staticInstructions.erase(found);
        taintedStaticInstructions.insert(key);
    }
}

void
FstProjector::close()
{
    if (closed) {
        return;
    }
    fatal_if(activeTransaction || activeSyscallGateway,
             "core=%u did not end at a frame boundary", core);
    fatal_if(
        counters.loweredKernelUopsBeforeIdleFilter !=
            counters.emittedKernelUops + counters.excludedIdleKernelUops,
        "kernel idle accounting does not conserve UOPs core=%u", core);
    if (addressSpaces.size() == 1) {
        writer->set_static_instruction_isa(
            fastsim::StaticInstructionIsa::kX86_64);
        std::vector<std::pair<
            StaticInstructionKey, fastsim::StaticInstructionInfo>>
            orderedStaticInstructions(
                staticInstructions.begin(), staticInstructions.end());
        std::sort(
            orderedStaticInstructions.begin(),
            orderedStaticInstructions.end(),
            [](const auto &left, const auto &right) {
                return left.first < right.first;
            });
        for (const auto &[key, instruction] : orderedStaticInstructions) {
            (void)key;
            writer->register_static_instruction(instruction);
        }
        writer->set_static_instruction_map_complete(false);
    }
    writer->close();
    writer.reset();
    closed = true;
}

} // namespace X86ISA
} // namespace gem5
