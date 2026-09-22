#include "arch/x86/qemu_fst_lowering_cpu.hh"

#include <algorithm>
#include <cstring>
#include <stdexcept>

#include "arch/x86/faults.hh"
#include "arch/x86/generated/decoder.hh"
#include "arch/x86/insts/macroop.hh"
#include "arch/x86/insts/microfpop.hh"
#include "arch/x86/insts/micromediaop.hh"
#include "arch/x86/ldstflags.hh"
#include "arch/x86/pcstate.hh"
#include "arch/x86/regs/ccr.hh"
#include "arch/x86/regs/float.hh"
#include "arch/x86/regs/int.hh"
#include "arch/x86/regs/misc.hh"
#include "arch/x86/regs/msr.hh"
#include "arch/x86/utility.hh"
#include "base/logging.hh"
#include "cpu/simple/exec_context.hh"
#include "cpu/simple_thread.hh"
#include "cpu/static_inst.hh"

namespace gem5
{
namespace X86ISA
{
namespace
{

SegAttr
segmentAttributes(uint64_t value)
{
    using namespace qemu_trace_extensions;
    SegAttr result = 0;
    result.type = value & kSegmentTypeMask;
    result.system = !!(value & kSegmentSystem);
    result.dpl = (value & kSegmentDplMask) >> kSegmentDplShift;
    result.present = !!(value & kSegmentPresent);
    result.avl = !!(value & kSegmentAvailable);
    result.longMode = !!(value & kSegmentLong);
    result.defaultSize = !!(value & kSegmentDefaultSize);
    result.granularity = !!(value & kSegmentGranularity);
    result.unusable = !result.present;
    return result;
}

Fault
eventFault(const CommitRecord &transaction)
{
    const auto &event = transaction.event;
    fatal_if(!event.vectorValid,
             "QEMU event has no vector pc=%#x", transaction.pc);
    if (event.kind == qemu_trace_extensions::StructuredEventKind::kInterrupt) {
        return std::make_shared<ExternalInterrupt>(
            static_cast<uint8_t>(event.vector));
    }
    fatal_if(event.kind !=
                 qemu_trace_extensions::StructuredEventKind::kException,
             "QEMU event kind is not lowerable through gem5 ROM pc=%#x "
             "kind=%u",
             transaction.pc, unsigned(event.kind));
    const uint32_t errorCode = event.errorCodeValid ? event.errorCode : 0;
    switch (event.vector) {
      case 0: return std::make_shared<DivideError>();
      case 1: return std::make_shared<DebugException>();
      case 2: return std::make_shared<NonMaskableInterrupt>(2);
      case 3: return std::make_shared<Breakpoint>();
      case 4: return std::make_shared<OverflowTrap>();
      case 5: return std::make_shared<BoundRange>();
      case 6: return std::make_shared<InvalidOpcode>();
      case 7: return std::make_shared<DeviceNotAvailable>();
      case 8:
        fatal("double fault is outside the native FST V1 contract pc=%#x",
              transaction.pc);
      case 10: return std::make_shared<InvalidTSS>(errorCode);
      case 11: return std::make_shared<SegmentNotPresent>(errorCode);
      case 12: return std::make_shared<StackFault>(errorCode);
      case 13: return std::make_shared<GeneralProtection>(errorCode);
      case 14:
        return std::make_shared<PageFault>(
            transaction.state->get(qemu_trace_extensions::kStateCr2),
            errorCode);
      case 16: return std::make_shared<X87FpExceptionPending>();
      case 17: return std::make_shared<AlignmentCheck>();
      case 18:
        fatal("machine check is outside the native FST V1 contract pc=%#x",
              transaction.pc);
      case 19: return std::make_shared<SIMDFloatingPointFault>();
      case 30: return std::make_shared<SecurityException>();
      default:
        fatal("unsupported x86 exception vector=%u pc=%#x",
              event.vector, transaction.pc);
    }
}

}

X86QemuFstOfflineLowererHost::X86QemuFstOfflineLowererHost(const Params &params)
    : BaseSimpleCPU(params),
      instructionPort(name() + ".icache_port"),
      dataPort(name() + ".dcache_port")
{
    fatal_if(params.numThreads != 1,
             "X86QemuFstOfflineLowererHost requires exactly one thread");
    context = threadInfo[0];
    _status = Idle;
}

void
X86QemuFstOfflineLowererHost::init()
{
    BaseSimpleCPU::init();
    auto *thread = threadInfo[0]->thread;
    thread->getIsaPtr()->setThreadContext(thread->getTC());
    initializeProvidedTemplates();
}

void
X86QemuFstOfflineLowererHost::startup()
{
    BaseSimpleCPU::startup();
    threadInfo[0]->thread->suspend();
    _status = Idle;
}

void
X86QemuFstOfflineLowererHost::prepareDecoder(
    const CommitRecord &transaction)
{
    auto *decoder = threadInfo[0]->thread->getDecoderPtr();
    decoder->reset();
    stageDecoderBytes(
        transaction,
        transaction.pc & ~(Addr(decoder->moreBytesSize()) - 1));
}

void
X86QemuFstOfflineLowererHost::stageDecoderBytes(
    const CommitRecord &transaction, Addr fetchPc)
{
    auto *decoder = threadInfo[0]->thread->getDecoderPtr();
    const size_t chunkSize = decoder->moreBytesSize();
    std::memset(decoder->moreBytesPtr(), 0, chunkSize);
    const size_t chunkOffset =
        fetchPc < transaction.pc ? transaction.pc - fetchPc : 0;
    const size_t instructionOffset =
        fetchPc > transaction.pc ? fetchPc - transaction.pc : 0;
    fatal_if(instructionOffset >= transaction.size,
             "gem5 decoder requested bytes beyond QEMU frame pc=%#x",
             transaction.pc);
    const size_t remaining = transaction.size - instructionOffset;
    const size_t copied = std::min(chunkSize - chunkOffset, remaining);
    std::memcpy(
        static_cast<uint8_t *>(decoder->moreBytesPtr()) + chunkOffset,
        transaction.bytes + instructionOffset, copied);
}

void
X86QemuFstOfflineLowererHost::initializeProvidedTemplates()
{
    integerProvided = {};
    floatingProvided = {};
    conditionProvided = {};
    miscProvided = {};
    for (RegIndex index = 0; index < int_reg::NumRegs; ++index) {
        provide(intRegClass[index]);
    }
    for (RegIndex index = 0; index < 8; ++index) {
        provide(float_reg::fpr(index));
    }
    for (RegIndex index = 0; index < 16; ++index) {
        provide(float_reg::xmmLow(index));
        provide(float_reg::xmmHigh(index));
    }
    for (RegIndex index = float_reg::MicrofpBase;
         index < float_reg::NumRegs; ++index) {
        provide(floatRegClass[index]);
    }
    for (const auto &reg :
         {cc_reg::Zaps, cc_reg::Cfof, cc_reg::Df, cc_reg::Ecf, cc_reg::Ezf}) {
        provide(reg);
    }
    for (const auto reg : {
             misc_reg::Cr0, misc_reg::Cr2, misc_reg::Cr3, misc_reg::Cr4,
             misc_reg::Cr8, misc_reg::Efer, misc_reg::Rflags,
             misc_reg::Star, misc_reg::Lstar, misc_reg::Cstar,
             misc_reg::SfMask, misc_reg::KernelGsBase,
             misc_reg::SysenterCs, misc_reg::SysenterEsp,
             misc_reg::SysenterEip, misc_reg::TscAux, misc_reg::Mxcsr,
             misc_reg::X87Top, misc_reg::Fcw, misc_reg::Fsw,
             misc_reg::Ftw, misc_reg::Ftag, misc_reg::Fop,
             misc_reg::Fioff, misc_reg::Fooff, misc_reg::M5Reg,
             misc_reg::TsgBase, misc_reg::TsgEffBase, misc_reg::TsgLimit,
             misc_reg::IdtrBase, misc_reg::IdtrEffBase,
             misc_reg::IdtrLimit}) {
        provide(miscRegClass[reg]);
    }
    for (const int segment :
         {segment_idx::Cs, segment_idx::Ss, segment_idx::Es,
          segment_idx::Ds, segment_idx::Fs, segment_idx::Gs,
          segment_idx::Tsl, segment_idx::Tr, segment_idx::Hs,
          segment_idx::Ls, segment_idx::Ms}) {
        provide(miscRegClass[misc_reg::segSel(segment)]);
        provide(miscRegClass[misc_reg::segBase(segment)]);
        provide(miscRegClass[misc_reg::segEffBase(segment)]);
        provide(miscRegClass[misc_reg::segLimit(segment)]);
        provide(miscRegClass[misc_reg::segAttr(segment)]);
    }
    integerProvidedTemplate = integerProvided;
    floatingProvidedTemplate = floatingProvided;
    conditionProvidedTemplate = conditionProvided;
    miscProvidedTemplate = miscProvided;
}

void
X86QemuFstOfflineLowererHost::resetExecutionScratch()
{
    auto *thread = threadInfo[0]->thread;
    integerProvided = integerProvidedTemplate;
    floatingProvided = floatingProvidedTemplate;
    conditionProvided = conditionProvidedTemplate;
    miscProvided = miscProvidedTemplate;
    for (RegIndex index = int_reg::MicroBegin;
         index < int_reg::NumRegs; ++index) {
        thread->setReg(intRegClass[index], RegVal(0));
    }
    for (RegIndex index = float_reg::MicrofpBase;
         index < float_reg::NumRegs; ++index) {
        thread->setReg(floatRegClass[index], RegVal(0));
    }
    for (const int segment :
         {segment_idx::Hs, segment_idx::Ls, segment_idx::Ms}) {
        thread->setMiscRegNoEffect(misc_reg::segSel(segment), 0);
        thread->setMiscRegNoEffect(misc_reg::segBase(segment), 0);
        thread->setMiscRegNoEffect(misc_reg::segEffBase(segment), 0);
        thread->setMiscRegNoEffect(misc_reg::segLimit(segment), 0);
        thread->setMiscRegNoEffect(misc_reg::segAttr(segment), 0);
    }
    threadInfo[0]->setPredicate(true);
    threadInfo[0]->setMemAccPredicate(true);
}

void
X86QemuFstOfflineLowererHost::importQemuState(
    const CommitRecord &transaction)
{
    auto *thread = threadInfo[0]->thread;
    const auto &state = *transaction.state;
    // The assembler always materializes the full canonical state (delta packets
    // are folded into currentState before the transaction is emitted), so every
    // frame injects the complete architectural register file. The fresh state
    // overwrites whatever the previous frame's micro-ops left behind; no undo
    // log or per-field delta gating is needed, and gem5 recomputes all derived
    // registers (M5Reg, effective segment bases) from the effectful setters.
    //
    // resetExecutionScratch() already restores the full-architectural "provided"
    // template each frame, so this importer only writes register values and does
    // not re-mark the guard baseline. The sole exception is Tsc, which the
    // template deliberately omits so the read-only guard still fatals on a TSC
    // read that carries no QEMU evidence; it is provided here only when present.
    for (RegIndex index = 0; index < int_reg::NumArchRegs; ++index) {
        thread->setReg(
            intRegClass[index],
            state.get(qemu_trace_extensions::kStateGprBase + index));
    }
    for (RegIndex index = 0; index < 8; ++index) {
        const auto lowField =
            qemu_trace_extensions::kStateFp80Base + 2 * index;
        std::array<uint8_t, 10> bits{};
        const uint64_t low = state.get(lowField);
        const uint16_t high = static_cast<uint16_t>(state.get(
            lowField + 1));
        std::memcpy(bits.data(), &low, sizeof(low));
        std::memcpy(bits.data() + sizeof(low), &high, sizeof(high));
        thread->setReg(
            float_reg::fpr(index),
            floatToBits64(loadFloat80(bits.data())));
    }
    for (RegIndex index = 0; index < 16; ++index) {
        const auto lowField =
            qemu_trace_extensions::kStateXmmBase +
            index * qemu_trace_extensions::kStateXmmWords;
        thread->setReg(
            float_reg::xmmLow(index),
            state.get(lowField));
        thread->setReg(
            float_reg::xmmHigh(index),
            state.get(lowField + 1));
    }
    const uint64_t rflags =
        state.get(qemu_trace_extensions::kStateRflags);
    setRFlags(thread->getTC(), rflags);
    for (const auto &[reg, field] : {
             std::pair{misc_reg::Cr0, qemu_trace_extensions::kStateCr0},
             std::pair{misc_reg::Cr2, qemu_trace_extensions::kStateCr2},
             std::pair{misc_reg::Cr3, qemu_trace_extensions::kStateCr3},
             std::pair{misc_reg::Cr4, qemu_trace_extensions::kStateCr4},
             std::pair{misc_reg::Cr8, qemu_trace_extensions::kStateCr8},
             std::pair{misc_reg::Efer, qemu_trace_extensions::kStateEfer}}) {
        thread->setMiscRegNoEffect(reg, state.get(field));
    }
    for (const auto &[reg, field] : {
             std::pair{misc_reg::Star,
                       qemu_trace_extensions::kStateStar},
             std::pair{misc_reg::Lstar,
                       qemu_trace_extensions::kStateLstar},
             std::pair{misc_reg::Cstar,
                       qemu_trace_extensions::kStateCstar},
             std::pair{misc_reg::SfMask,
                       qemu_trace_extensions::kStateFmask},
             std::pair{misc_reg::KernelGsBase,
                       qemu_trace_extensions::kStateKernelGsBase},
             std::pair{misc_reg::SysenterCs,
                       qemu_trace_extensions::kStateSysenterCs},
             std::pair{misc_reg::SysenterEsp,
                       qemu_trace_extensions::kStateSysenterEsp},
             std::pair{misc_reg::SysenterEip,
                       qemu_trace_extensions::kStateSysenterEip},
             std::pair{misc_reg::TscAux,
                       qemu_trace_extensions::kStateTscAux},
             std::pair{misc_reg::Mxcsr,
                       qemu_trace_extensions::kStateMxcsr},
             std::pair{misc_reg::X87Top,
                       qemu_trace_extensions::kStateX87Top},
             std::pair{misc_reg::Fcw,
                       qemu_trace_extensions::kStateFcw},
             std::pair{misc_reg::Fsw,
                       qemu_trace_extensions::kStateFsw},
             std::pair{misc_reg::Ftw,
                       qemu_trace_extensions::kStateFtw},
             std::pair{misc_reg::Ftag,
                       qemu_trace_extensions::kStateFtw},
             std::pair{misc_reg::Fop,
                       qemu_trace_extensions::kStateFop},
             std::pair{misc_reg::Fioff,
                       qemu_trace_extensions::kStateFip},
             std::pair{misc_reg::Fooff,
                       qemu_trace_extensions::kStateFdp}}) {
        thread->setMiscRegNoEffect(reg, state.get(field));
    }
    if (const auto value = transaction.tscRead()) {
        thread->setMiscRegNoEffect(misc_reg::Tsc, *value);
        provide(miscRegClass[misc_reg::Tsc]);
    }
    if (const auto value = transaction.tscAuxRead()) {
        thread->setMiscRegNoEffect(
            misc_reg::TscAux, *value);
    }
    const auto setSegment = [&](int segment, uint16_t selectorField,
                                uint16_t baseField, uint16_t limitField,
                                uint16_t attributesField) {
        thread->setMiscReg(
            misc_reg::segSel(segment), state.get(selectorField));
        thread->setMiscReg(
            misc_reg::segBase(segment), state.get(baseField));
        thread->setMiscReg(
            misc_reg::segLimit(segment), state.get(limitField));
        thread->setMiscReg(
            misc_reg::segAttr(segment),
            segmentAttributes(state.get(attributesField)));
    };
    setSegment(
        segment_idx::Cs, qemu_trace_extensions::kStateCsSelector,
        qemu_trace_extensions::kStateCsBase,
        qemu_trace_extensions::kStateCsLimit,
        qemu_trace_extensions::kStateCsFlags);
    setSegment(
        segment_idx::Ss, qemu_trace_extensions::kStateSsSelector,
        qemu_trace_extensions::kStateSsBase,
        qemu_trace_extensions::kStateSsLimit,
        qemu_trace_extensions::kStateSsFlags);
    setSegment(
        segment_idx::Es, qemu_trace_extensions::kStateEsSelector,
        qemu_trace_extensions::kStateEsBase,
        qemu_trace_extensions::kStateEsLimit,
        qemu_trace_extensions::kStateEsFlags);
    setSegment(
        segment_idx::Ds, qemu_trace_extensions::kStateDsSelector,
        qemu_trace_extensions::kStateDsBase,
        qemu_trace_extensions::kStateDsLimit,
        qemu_trace_extensions::kStateDsFlags);
    setSegment(
        segment_idx::Fs, qemu_trace_extensions::kStateFsSelector,
        qemu_trace_extensions::kStateFsBase,
        qemu_trace_extensions::kStateFsLimit,
        qemu_trace_extensions::kStateFsFlags);
    setSegment(
        segment_idx::Gs, qemu_trace_extensions::kStateGsSelector,
        qemu_trace_extensions::kStateGsBase,
        qemu_trace_extensions::kStateGsLimit,
        qemu_trace_extensions::kStateGsFlags);
    setSegment(
        segment_idx::Tsl, qemu_trace_extensions::kStateLdtSelector,
        qemu_trace_extensions::kStateLdtBase,
        qemu_trace_extensions::kStateLdtLimit,
        qemu_trace_extensions::kStateLdtFlags);
    setSegment(
        segment_idx::Tr, qemu_trace_extensions::kStateTrSelector,
        qemu_trace_extensions::kStateTrBase,
        qemu_trace_extensions::kStateTrLimit,
        qemu_trace_extensions::kStateTrFlags);
    thread->setMiscRegNoEffect(
        misc_reg::TsgBase,
        state.get(qemu_trace_extensions::kStateGdtBase));
    thread->setMiscRegNoEffect(
        misc_reg::TsgEffBase,
        state.get(qemu_trace_extensions::kStateGdtBase));
    thread->setMiscRegNoEffect(
        misc_reg::TsgLimit,
        state.get(qemu_trace_extensions::kStateGdtLimit));
    thread->setMiscRegNoEffect(
        misc_reg::IdtrBase,
        state.get(qemu_trace_extensions::kStateIdtBase));
    thread->setMiscRegNoEffect(
        misc_reg::IdtrEffBase,
        state.get(qemu_trace_extensions::kStateIdtBase));
    thread->setMiscRegNoEffect(
        misc_reg::IdtrLimit,
        state.get(qemu_trace_extensions::kStateIdtLimit));

    const Efer efer = state.get(qemu_trace_extensions::kStateEfer);
    const SegAttr csAttr = segmentAttributes(
        state.get(qemu_trace_extensions::kStateCsFlags));
    const RFLAGS architecturalFlags = rflags;
    fatal_if(!efer.lma || !csAttr.longMode || architecturalFlags.vm ||
                 csAttr.dpl != transaction.privilege(),
             "QEMU transaction is outside x86-64 long-mode contract pc=%#x",
             transaction.pc);
    // Like KVM import, derive the mode and update the decoder after loading all
    // architectural control and segment registers. Full-state injection always
    // reloads the mode inputs, so the derived M5Reg is recomputed every frame.
    thread->setMiscReg(misc_reg::M5Reg, 0);
    for (const auto &[reg, field] : {
             std::pair{misc_reg::Mxcsr,
                       qemu_trace_extensions::kStateMxcsr},
             std::pair{misc_reg::Fcw,
                       qemu_trace_extensions::kStateFcw},
             std::pair{misc_reg::Fsw,
                       qemu_trace_extensions::kStateFsw},
             std::pair{misc_reg::Ftw,
                       qemu_trace_extensions::kStateFtw},
             std::pair{misc_reg::Ftag,
                       qemu_trace_extensions::kStateFtw},
             std::pair{misc_reg::Fop,
                       qemu_trace_extensions::kStateFop},
             std::pair{misc_reg::Fioff,
                       qemu_trace_extensions::kStateFip},
             std::pair{misc_reg::Fooff,
                       qemu_trace_extensions::kStateFdp}}) {
        thread->setMiscRegNoEffect(reg, state.get(field));
    }

    PCState pc(transaction.pc);
    pc.size(static_cast<uint8_t>(transaction.size));
    pc.npc(transaction.pc + transaction.size);
    thread->pcState(pc);
}

void
X86QemuFstOfflineLowererHost::lower(
    const CommitRecord &envelope, FstProjector &projector)
{
    const auto &transaction = envelope;
    fatal_if(transaction.privilege() != 0 && transaction.privilege() != 3,
             "unsupported QEMU CPL=%u", unsigned(transaction.privilege()));
    resetExecutionScratch();
    importQemuState(transaction);
    activeRecord = &transaction;
    activeDescriptor = nullptr;
    consumedRefs.clear();
    try {
        memory.reset(transaction.refs);
        if (transaction.kind == CommitKind::Macro) {
            prepareDecoder(transaction);
            executeFrame(envelope, false, projector);
        } else {
            lowerEvent(envelope, projector);
        }
        memory.finish();
        activeRecord = nullptr;
        activeDescriptor = nullptr;
    } catch (const std::runtime_error &error) {
        fatal("QEMU lowering failed pc=%#x: %s", transaction.pc, error.what());
    }
}

void
X86QemuFstOfflineLowererHost::lowerEvent(
    const CommitRecord &envelope, FstProjector &projector)
{
    const auto &transaction = envelope;
    if (transaction.event.kind ==
        qemu_trace_extensions::StructuredEventKind::kSyscall) {
        (void)projector.beginFrame(envelope, nullptr, "syscall");
        projector.endFrame();
        return;
    }
    auto fault = eventFault(transaction);
    fault->invoke(threadInfo[0]->thread->getTC());
    // X86FaultBase::invoke supplies these ROM entry operands.
    provide(intRegMicro(1));
    provide(intRegMicro(7));
    if (transaction.event.errorCodeValid) {
        provide(intRegMicro(15));
    }
    auto pc = threadInfo[0]->thread->pcState().as<PCState>();
    fatal_if(!isRomMicroPC(pc.microPC()),
             "gem5 event did not enter ROM microcode pc=%#x",
             transaction.pc);
    executeFrame(envelope, true, projector);
}

void
X86QemuFstOfflineLowererHost::executeFrame(
    const CommitRecord &envelope, bool rom, FstProjector &projector)
{
    const auto &transaction = envelope;
    auto *thread = threadInfo[0]->thread;
    curStaticInst = nullptr;
    curMacroStaticInst = nullptr;
    StaticInstPtr macro;
    std::string mnemonic;
    bool frameStarted = false;
    for (;;) {
        preExecute();
        if (!curStaticInst) {
            stageDecoderBytes(
                transaction,
                (transaction.pc &
                 ~(Addr(thread->getDecoderPtr()->moreBytesSize()) - 1)) +
                    context->fetchOffset);
            continue;
        }
        const MicroPC microPc = thread->pcState().microPC();
        const bool inRom = isRomMicroPC(microPc);
        StaticInstPtr micro = curStaticInst;
        fatal_if(!micro, "gem5 preExecute produced no UOP pc=%#x",
                 transaction.pc);
        if (!frameStarted) {
            macro = curMacroStaticInst ? curMacroStaticInst : micro;
            mnemonic = rom ? "event" : macro->getName();
            if (!projector.beginFrame(envelope, macro, mnemonic)) {
                projector.endFrame();
                return;
            }
            frameStarted = true;
        }
        if (!rom) {
            micro = architecturalWidthMemoryLoad(
                transaction, macro, micro);
        }
        const auto &descriptor = descriptors.get(micro, *thread->getIsaPtr());
        activeDescriptor = &descriptor;
        consumedRefs.clear();
        requireSources(descriptor);
        const Fault fault = micro->execute(context, nullptr);
        provideDestinations(descriptor);
        const auto faultPc = thread->pcState().as<PCState>();
        uint64_t encoding = 0;
        std::memcpy(
            &encoding, transaction.bytes,
            std::min(sizeof(encoding), static_cast<size_t>(transaction.size)));
        fatal_if(fault != NoFault,
                 "QEMU committed transaction faults in gem5 pc=%#x "
                 "macro=%s encoding=%#llx micro=%u/%u mnemonic=%s "
                 "gem5_pc=%#x npc=%#x qemu_successor=%#x",
                 transaction.pc, mnemonic.c_str(),
                 static_cast<unsigned long long>(encoding),
                 microPc, faultPc.nupc(), descriptor.mnemonic.c_str(),
                 faultPc.instAddr(), faultPc.npc(), transaction.successor);
        const bool entersEventRom =
            !rom && !inRom && isRomMicroPC(faultPc.nupc());
        const bool completion =
            entersEventRom || !micro->isMicroop() || micro->isLastMicroop();
        projector.projectMicroop(
            descriptor, consumedRefs, rom || inRom, completion);
        consumedRefs.clear();
        advancePC(fault);
        if (entersEventRom) {
            projector.endFrame();
            return;
        }
        if (rom || inRom) {
            const auto pc = thread->pcState().as<PCState>();
            if (!isRomMicroPC(pc.microPC())) {
                fatal_if(pc.instAddr() != transaction.successor,
                         "gem5 event target disagrees with QEMU pc=%#x "
                         "gem5=%#x qemu=%#x",
                         transaction.pc, pc.instAddr(),
                         transaction.successor);
                projector.endFrame();
                return;
            }
        } else if (!macro->isMacroop() || micro->isLastMicroop()) {
            fatal_if(
                thread->pcState().instAddr() != transaction.successor,
                "gem5 macro successor disagrees with QEMU pc=%#x "
                "gem5=%#x qemu=%#x mnemonic=%s",
                transaction.pc, thread->pcState().instAddr(),
                transaction.successor, mnemonic.c_str());
            projector.endFrame();
            return;
        }
    }
}

StaticInstPtr
X86QemuFstOfflineLowererHost::architecturalWidthMemoryLoad(
    const CommitRecord &transaction,
    const StaticInstPtr &macro, const StaticInstPtr &micro)
{
    if (!macro->isMacroop() || !micro->isLoad() || !micro->isFloating()) {
        return micro;
    }
    auto *macroop = dynamic_cast<MacroopBase *>(macro.get());
    const auto *load = dynamic_cast<const X86ISAInst::Ldfp *>(micro.get());
    if (!macroop || !load) {
        return micro;
    }
    const auto architecturalSize = macroop->getEmulEnv().dataSize;
    const auto encoding = macroop->getExtMachInst();
    if (!encoding.legacy.rep || architecturalSize != 4 ||
        load->dataSize != 8) {
        return micro;
    }
    const auto matching = std::count_if(
        transaction.refs.begin(), transaction.refs.end(),
        [architecturalSize](const qemu_fst_record::Access &ref) {
            return !ref.isStore() &&
                !(ref.attributes &
                  (qemu_trace_extensions::kMemoryAttributePio |
                   qemu_trace_extensions::kMemoryAttributeMmio)) &&
                ref.size == static_cast<uint64_t>(architecturalSize);
        });
    if (matching != 1) {
        return micro;
    }
    auto found = architecturalWidthLoads.find(micro.get());
    if (found != architecturalWidthLoads.end()) {
        return found->second.second;
    }
    uint64_t flags = uint64_t{1} << StaticInst::IsMicroop;
    if (micro->isFirstMicroop()) {
        flags |= uint64_t{1} << StaticInst::IsFirstMicroop;
    }
    if (micro->isLastMicroop()) {
        flags |= uint64_t{1} << StaticInst::IsLastMicroop;
    }
    if (micro->isDelayedCommit()) {
        flags |= uint64_t{1} << StaticInst::IsDelayedCommit;
    }
    const auto memoryFlags =
        (floorLog2(load->addressSize) & AddrSizeFlagMask) <<
        AddrSizeFlagShift;
    StaticInstPtr narrowed = new X86ISAInst::Ldfp(
        load->machInst, "architectural-width", flags,
        static_cast<uint8_t>(architecturalSize), load->addressSize,
        memoryFlags, FpRegIndex(load->data), load->scale,
        GpRegIndex(load->index & ~IntFoldBit),
        GpRegIndex(load->base & ~IntFoldBit), load->disp,
        SegRegIndex(load->segment));
    architecturalWidthLoads.emplace(
        micro.get(), std::make_pair(micro, narrowed));
    return narrowed;
}

void
X86QemuFstOfflineLowererHost::provide(const RegId &reg)
{
    const RegId flattened =
        reg.flatten(*threadInfo[0]->thread->getIsaPtr());
    provideFlattened(flattened);
}

void
X86QemuFstOfflineLowererHost::provideFlattened(const RegId &flattened)
{
    switch (flattened.classValue()) {
      case IntRegClass:
        integerProvided.at(flattened.index()) = true;
        break;
      case FloatRegClass:
        floatingProvided.at(flattened.index()) = true;
        break;
      case CCRegClass:
        conditionProvided.at(flattened.index()) = true;
        break;
      case MiscRegClass:
        miscProvided.at(flattened.index()) = true;
        break;
      default:
        break;
    }
}

void
X86QemuFstOfflineLowererHost::requireSources(
    const QemuMicroopDescriptor &descriptor)
{
    for (const RegId &flattened : descriptor.flattenedSources) {
        bool provided = true;
        switch (flattened.classValue()) {
          case InvalidRegClass:
            continue;
          case IntRegClass:
            provided = integerProvided.at(flattened.index());
            break;
          case FloatRegClass:
            provided = floatingProvided.at(flattened.index());
            break;
          case CCRegClass:
            provided = conditionProvided.at(flattened.index());
            break;
          case MiscRegClass:
            provided = miscProvided.at(flattened.index());
            break;
          default:
            break;
        }
        fatal_if(!provided,
                 "gem5 microcode reads state not provided by QEMU "
                 "pc=%#x mnemonic=%s register=%s class=%u index=%u",
                 activeRecord->pc, descriptor.mnemonic.c_str(),
                 flattened.regClass().regName(flattened).c_str(),
                 unsigned(flattened.classValue()),
                 unsigned(flattened.index()));
    }
}

void
X86QemuFstOfflineLowererHost::provideDestinations(
    const QemuMicroopDescriptor &descriptor)
{
    for (const RegId &flattened : descriptor.flattenedDestinations) {
        provideFlattened(flattened);
    }
}

void
X86QemuFstOfflineLowererHost::consumeMemory(
    Addr addr, unsigned size, bool store, Request::Flags flags, uint8_t *data,
    uint32_t accessAttributes)
{
    fatal_if(!activeRecord,
             "gem5 memory access is outside one QEMU transaction");
    (void)flags;
    try {
        memory.access(
            addr, size, store, data, consumedRefs, accessAttributes);
    } catch (const std::runtime_error &) {
        fatal_if(!activeDescriptor,
                 "QEMU transaction memory diagnostic lacks a micro-op");
        uint64_t encoding = 0;
        std::memcpy(
            &encoding, activeRecord->bytes,
            std::min(
                sizeof(encoding),
                static_cast<size_t>(activeRecord->size)));
        warn("QEMU-FST missing AccessFact pc=%#x uop=%s "
             "addr=%#x size=%u store=%d facts=%lu kind=%u "
             "event_kind=%u vector=%u encoding=%#llx",
             activeRecord->pc, activeDescriptor->mnemonic.c_str(),
             addr, size, int(store),
             static_cast<unsigned long>(activeRecord->refs.size()),
             unsigned(activeRecord->kind),
             unsigned(activeRecord->event.kind),
             activeRecord->event.vector,
             static_cast<unsigned long long>(encoding));
        for (const auto &ref : activeRecord->refs) {
            warn("QEMU-FST fact va=%#x pa=%#x size=%lu store=%d attr=%#x",
                 ref.vaddr, ref.paddr, static_cast<unsigned long>(ref.size),
                 int(ref.isStore()), unsigned(ref.attributes));
        }
        throw;
    }
}

Fault
X86QemuFstOfflineLowererHost::readMem(
    Addr addr, uint8_t *data, unsigned size, Request::Flags flags,
    const std::vector<bool> &byteEnable)
{
    fatal_if(!byteEnable.empty() && byteEnable.size() != size,
             "gem5 read byte-enable size mismatch");
    if (flags & (Request::NO_ACCESS | Request::PREFETCH)) {
        return NoFault;
    }
    const Addr translated = addr >> 3;
    if ((flags & SegmentFlagMask) == segment_idx::Ms) {
        const Addr prefix = translated & IntAddrPrefixMask;
        if (prefix == IntAddrPrefixIO) {
            const Addr port = addr & ~(IntAddrPrefixMask << 3);
            fatal_if(port > UINT16_MAX,
                     "gem5 requested invalid PIO port pc=%#x port=%#x",
                     activeRecord->pc, port);
            consumeMemory(
                port, size, false, flags, data,
                qemu_trace_extensions::kMemoryAttributePio);
            return NoFault;
        }
        fatal_if(prefix != IntAddrPrefixMSR,
                 "unsupported internal read pc=%#x address=%#x prefix=%#x size=%u",
                 activeRecord->pc, addr, prefix, size);
        RegIndex index = 0;
        const Addr msr = translated & ~IntAddrPrefixMask;
        fatal_if(size != sizeof(RegVal) || !msrAddrToIndex(index, msr),
                 "gem5 requested unsupported MSR read pc=%#x msr=%#x size=%u",
                 activeRecord->pc, msr, size);
        fatal_if(!miscProvided.at(index),
                 "gem5 MSR read lacks QEMU state pc=%#x msr=%#x",
                 activeRecord->pc, msr);
        const RegVal value =
            threadInfo[0]->thread->readMiscReg(index);
        std::memcpy(data, &value, sizeof(value));
        return NoFault;
    }
    consumeMemory(addr, size, false, flags, data);
    return NoFault;
}

Fault
X86QemuFstOfflineLowererHost::writeMem(
    uint8_t *data, unsigned size, Addr addr, Request::Flags flags,
    uint64_t *result, const std::vector<bool> &byteEnable)
{
    fatal_if(!byteEnable.empty() && byteEnable.size() != size,
             "gem5 write byte-enable size mismatch");
    if (flags & Request::NO_ACCESS) {
        if (result) *result = 0;
        return NoFault;
    }
    const Addr translated = addr >> 3;
    if ((flags & SegmentFlagMask) == segment_idx::Ms) {
        const Addr prefix = translated & IntAddrPrefixMask;
        if (prefix == IntAddrPrefixIO) {
            const Addr port = addr & ~(IntAddrPrefixMask << 3);
            fatal_if(port > UINT16_MAX,
                     "gem5 requested invalid PIO port pc=%#x port=%#x",
                     activeRecord->pc, port);
            consumeMemory(
                port, size, true, flags, data,
                qemu_trace_extensions::kMemoryAttributePio);
            if (result) *result = 0;
            return NoFault;
        }
        fatal_if(prefix != IntAddrPrefixMSR,
                 "unsupported internal write pc=%#x address=%#x prefix=%#x size=%u",
                 activeRecord->pc, addr, prefix, size);
        RegIndex index = 0;
        const Addr msr = translated & ~IntAddrPrefixMask;
        fatal_if(size != sizeof(RegVal) || !msrAddrToIndex(index, msr),
                 "gem5 requested unsupported MSR write pc=%#x msr=%#x size=%u",
                 activeRecord->pc, msr, size);
        RegVal value = 0;
        std::memcpy(&value, data, sizeof(value));
        threadInfo[0]->thread->setMiscReg(index, value);
        provide(miscRegClass[index]);
        return NoFault;
    }
    consumeMemory(addr, size, true, flags, data);
    if (result) {
        *result = 0;
    }
    return NoFault;
}

Fault
X86QemuFstOfflineLowererHost::amoMem(
    Addr addr, uint8_t *data, unsigned size, Request::Flags flags,
    AtomicOpFunctorPtr operation)
{
    if (flags & Request::NO_ACCESS) {
        return NoFault;
    }
    Request::Flags atomicFlags = flags;
    atomicFlags.set(Request::ATOMIC_RETURN_OP);
    consumeMemory(addr, size, false, atomicFlags, data);
    if (operation) {
        std::array<uint8_t, 16> updated{};
        std::copy_n(data, size, updated.begin());
        (*operation)(updated.data());
        memory.access(addr, size, true, updated.data(), consumedRefs);
    }
    return NoFault;
}

Fault
X86QemuFstOfflineLowererHost::initiateMemMgmtCmd(Request::Flags flags)
{
    (void)flags;
    fatal("QEMU transaction issued an unsupported memory-management command");
}

} // namespace X86ISA
} // namespace gem5
