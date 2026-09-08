#include "arch/x86/qemu_microcode_executor.hh"

#include <algorithm>
#include <array>
#include <cstring>
#include <limits>
#include <optional>
#include <string>
#include <string_view>
#include <unordered_map>
#include <utility>

#include "arch/x86/insts/macroop.hh"
#include "arch/x86/insts/microldstop.hh"
#include "arch/x86/insts/microregop.hh"
#include "arch/x86/pcstate.hh"
#include "arch/x86/regs/ccr.hh"
#include "arch/x86/regs/float.hh"
#include "arch/x86/regs/int.hh"
#include "arch/x86/regs/misc.hh"
#include "arch/x86/regs/msr.hh"
#include "arch/x86/x86_traits.hh"
#include "base/logging.hh"
#include "cpu/exec_context.hh"
#include "cpu/static_inst.hh"

namespace gem5
{
namespace X86ISA
{
namespace
{

constexpr uint64_t kFastSimPageSize = 4096;
constexpr uint32_t kQemuMemAttrAtomic =
    qemu_trace_extensions::kMemoryAttributeAtomic;
constexpr uint32_t kQemuMemAttrPio =
    qemu_trace_extensions::kMemoryAttributePio;
constexpr uint32_t kQemuMemAttrMmio =
    qemu_trace_extensions::kMemoryAttributeMmio;

bool
isScalarSingleMemorySource(const std::string &name)
{
    static const std::array<std::string_view, 14> names = {
        "addss", "subss", "mulss", "divss", "sqrtss", "rcpss",
        "rsqrtss", "minss", "maxss", "comiss", "ucomiss", "cvtss2sd",
        "cvtss2si", "cvttss2si",
    };
    return std::find(names.begin(), names.end(), name) != names.end();
}

} // namespace

class QemuMicrocodeExecutor::Context final : public ExecContext
{
  public:
    void begin(const QemuX86FunctionalState &state,
               const std::vector<QemuDataRef> &refs, Addr pcAddr,
               const std::string &name, bool allowPadding,
               uint64_t *nextScalarSinglePaddingCount)
    {
        ++generation;
        if (generation == 0) {
            intGeneration.fill(0);
            floatGeneration.fill(0);
            ccGeneration.fill(0);
            miscGeneration.fill(0);
            generation = 1;
        }
        sparseRegisters.clear();
        consumedMasks.assign(refs.size(), 0);
        memoryRefs = &refs;
        macroPc = pcAddr;
        macroName = &name;
        allowScalarSingleLoadPadding = allowPadding;
        scalarSinglePaddingCount = nextScalarSinglePaddingCount;
        consumedDataRefValid = false;
        for (const auto &ref : refs) {
            fatal_if(ref.size > 16,
                     "QEMU memory evidence exceeds 16-byte contract "
                     "pc=%#x mnemonic=%s size=%" PRIu64,
                     macroPc, macroName->c_str(), ref.size);
        }
        for (size_t index = 0; index < state.gpr.size(); ++index) {
            setInitial(intRegClass[index], state.gpr[index]);
        }
        for (RegIndex index = int_reg::MicroBegin;
             index < int_reg::NumRegs; ++index) {
            setInitial(intRegClass[index], 0);
        }
        for (size_t index = 0;
             index < qemu_trace_extensions::kUserStateXmmCount; ++index) {
            setInitial(float_reg::xmmLow(index),
                state.xmm[index * qemu_trace_extensions::kUserStateXmmWords]);
            setInitial(float_reg::xmmHigh(index),
                state.xmm[index * qemu_trace_extensions::kUserStateXmmWords +
                          1]);
        }
        for (RegIndex index = 0; index < NumMicroFpRegs; ++index) {
            setInitial(float_reg::microfp(index), 0);
        }
        setInitial(cc_reg::Zaps, state.rflags & CcFlagMask);
        setInitial(cc_reg::Cfof, state.rflags & CfofMask);
        setInitial(cc_reg::Df, state.rflags & DFBit);
        setInitial(cc_reg::Ecf, 0);
        setInitial(cc_reg::Ezf, 0);
        setInitial(miscRegClass[misc_reg::Rflags],
            state.rflags & ~(CcFlagMask | CfofMask | DFBit));
        static constexpr int userSegments[] = {
            segment_idx::Es, segment_idx::Cs, segment_idx::Ss,
            segment_idx::Ds, segment_idx::Fs, segment_idx::Gs,
        };
        for (const int segment : userSegments) {
            const RegVal base = segment == segment_idx::Fs ? state.fsBase :
                segment == segment_idx::Gs ? state.gsBase : 0;
            setInitial(miscRegClass[misc_reg::segBase(segment)], base);
            setInitial(miscRegClass[misc_reg::segEffBase(segment)], base);
        }
        HandyM5Reg m5Reg = 0;
        m5Reg.mode = LongMode;
        m5Reg.submode = SixtyFourBitMode;
        m5Reg.cpl = 3;
        m5Reg.defOp = 2;
        m5Reg.altOp = 1;
        m5Reg.defAddr = 3;
        m5Reg.altAddr = 2;
        m5Reg.stack = 3;
        setInitial(miscRegClass[misc_reg::M5Reg], m5Reg);
        pc = PCState(pcAddr);
    }

    RegVal getRegOperand(const StaticInst *inst, int index) override
    {
        return get(inst->srcRegIdx(index));
    }
    void getRegOperand(const StaticInst *inst, int index, void *value) override
    {
        *static_cast<RegVal *>(value) = getRegOperand(inst, index);
    }
    void *getWritableRegOperand(const StaticInst *inst, int index) override
    {
        const RegId reg = canonicalReg(inst->destRegIdx(index));
        return &writable(reg);
    }
    void setRegOperand(const StaticInst *inst, int index, RegVal value) override
    {
        const RegId reg = canonicalReg(inst->destRegIdx(index));
        writable(reg) = value;
    }
    void setRegOperand(const StaticInst *inst, int index,
                       const void *value) override
    {
        setRegOperand(inst, index, *static_cast<const RegVal *>(value));
    }
    RegVal readMiscRegOperand(const StaticInst *inst, int index) override
    {
        return get(inst->srcRegIdx(index));
    }
    void setMiscRegOperand(const StaticInst *inst, int index,
                           RegVal value) override
    {
        set(inst->destRegIdx(index), value);
    }
    RegVal readMiscReg(int index) override { return get(miscRegClass[index]); }
    void setMiscReg(int index, RegVal value) override
    {
        set(miscRegClass[index], value);
    }
    const PCStateBase &pcState() const override { return pc; }
    void pcState(const PCStateBase &value) override { pc = value.as<PCState>(); }
    Fault initiateMemMgmtCmd(Request::Flags) override { return NoFault; }
    Fault writeMem(uint8_t *data, unsigned int size, Addr addr,
                   Request::Flags flags, uint64_t *,
                   const std::vector<bool> &) override
    {
        RegIndex miscIndex = 0;
        if (internalMsrIndex(addr, miscIndex)) {
            fatal_if(size != sizeof(RegVal),
                     "gem5 MSR write has invalid size=%u", size);
            RegVal value = 0;
            std::memcpy(&value, data, sizeof(value));
            set(miscRegClass[miscIndex], value);
            return NoFault;
        }
        consumeMemory(addr, size, true, flags, nullptr);
        return NoFault;
    }
    void setStCondFailures(unsigned int) override {}
    unsigned int readStCondFailures() const override { return 0; }
    ThreadContext *tcBase() const override { return nullptr; }
    bool readPredicate() const override { return true; }
    void setPredicate(bool) override {}
    bool readMemAccPredicate() const override { return true; }
    void setMemAccPredicate(bool) override {}
    uint64_t newHtmTransactionUid() const override { return 0; }
    uint64_t getHtmTransactionUid() const override { return 0; }
    bool inHtmTransactionalState() const override { return false; }
    uint64_t getHtmTransactionalDepth() const override { return 0; }
    void demapPage(Addr, uint64_t) override {}
    void armMonitor(Addr) override {}
    bool mwait(PacketPtr) override { return false; }
    void mwaitAtomic(ThreadContext *) override {}
    AddressMonitor *getAddrMonitor() override { return nullptr; }

    void set(const RegId &reg, RegVal value)
    {
        const RegId canonical = canonicalReg(reg);
        writable(canonical) = value;
    }
    RegVal get(const RegId &reg) const
    {
        const RegId canonical = canonicalReg(reg);
        if (canonical.is(InvalidRegClass)) {
            return 0;
        }
        const auto value = read(canonical);
        fatal_if(!value,
                 "gem5 microcode read unsupported architectural state "
                 "pc=%#x mnemonic=%s class=%d register=%u",
                 macroPc, macroName->c_str(), reg.classValue(), reg.index());
        return *value;
    }
    void setPc(Addr addr, uint8_t size, MicroPC upc, MicroPC nupc)
    {
        pc = PCState(addr);
        pc.size(size);
        pc.npc(addr + size);
        pc.upc(upc);
        pc.nupc(nupc);
    }
    MicroPC nextMicroPc() const { return pc.nupc(); }
    bool takeConsumedDataRef(QemuDataRef &result)
    {
        if (!consumedDataRefValid) {
            return false;
        }
        result = consumedDataRef;
        consumedDataRefValid = false;
        return true;
    }
    bool allMemoryEvidenceConsumed() const
    {
        for (size_t index = 0; index < consumedMasks.size(); ++index) {
            const auto requiredMask =
                (uint32_t{1} << (*memoryRefs)[index].size) - 1;
            if (consumedMasks[index] != requiredMask) {
                return false;
            }
        }
        return true;
    }
    Fault readMem(Addr addr, uint8_t *data, unsigned int size,
                  Request::Flags flags, const std::vector<bool> &) override
    {
        RegIndex miscIndex = 0;
        if (internalMsrIndex(addr, miscIndex)) {
            fatal_if(size != sizeof(RegVal),
                     "gem5 MSR read has invalid size=%u", size);
            const RegVal value = get(miscRegClass[miscIndex]);
            std::memcpy(data, &value, sizeof(value));
            return NoFault;
        }
        consumeMemory(addr, size, false, flags, data);
        return NoFault;
    }

  private:
    static RegId canonicalReg(const RegId &reg)
    {
        if (reg.classValue() == IntRegClass) {
            return intRegClass[reg.index() & ~IntFoldBit];
        }
        return reg;
    }
    RegVal &writable(const RegId &reg)
    {
        if (reg.classValue() == IntRegClass && reg.index() < intValues.size()) {
            intGeneration[reg.index()] = generation;
            return intValues[reg.index()];
        }
        if (reg.classValue() == FloatRegClass && reg.index() < floatValues.size()) {
            floatGeneration[reg.index()] = generation;
            return floatValues[reg.index()];
        }
        if (reg.classValue() == CCRegClass && reg.index() < ccValues.size()) {
            ccGeneration[reg.index()] = generation;
            return ccValues[reg.index()];
        }
        if (reg.classValue() == MiscRegClass && reg.index() < miscValues.size()) {
            miscGeneration[reg.index()] = generation;
            return miscValues[reg.index()];
        }
        return sparseRegisters[reg];
    }
    std::optional<RegVal> read(const RegId &reg) const
    {
        if (reg.classValue() == IntRegClass && reg.index() < intValues.size() &&
            intGeneration[reg.index()] == generation) return intValues[reg.index()];
        if (reg.classValue() == FloatRegClass && reg.index() < floatValues.size() &&
            floatGeneration[reg.index()] == generation) return floatValues[reg.index()];
        if (reg.classValue() == CCRegClass && reg.index() < ccValues.size() &&
            ccGeneration[reg.index()] == generation) return ccValues[reg.index()];
        if (reg.classValue() == MiscRegClass && reg.index() < miscValues.size() &&
            miscGeneration[reg.index()] == generation) return miscValues[reg.index()];
        const auto found = sparseRegisters.find(reg);
        return found == sparseRegisters.end()
            ? std::nullopt : std::optional<RegVal>(found->second);
    }
    void setInitial(const RegId &reg, RegVal value) { writable(reg) = value; }
    static bool internalMsrIndex(Addr addr, RegIndex &index)
    {
        const Addr translated = addr >> 3;
        if ((translated & IntAddrPrefixMask) != IntAddrPrefixMSR) return false;
        const Addr msr = translated & ~IntAddrPrefixMask;
        fatal_if(!msrAddrToIndex(index, msr),
                 "gem5 microcode references unsupported MSR=%#x", msr);
        return true;
    }
    void consumeMemory(Addr addr, unsigned int size, bool store,
                       Request::Flags flags, uint8_t *data)
    {
        fatal_if(consumedDataRefValid,
                 "gem5 micro-op issued multiple memory operations");
        const bool gem5Pio = ((addr >> 3) & IntAddrPrefixMask) == IntAddrPrefixIO;
        const bool gem5Atomic = flags.isSet(
            Request::LOCKED_RMW | Request::READ_MODIFY_WRITE |
            Request::ATOMIC_RETURN_OP | Request::ATOMIC_NO_RETURN_OP);
        const Addr functionalAddr = gem5Pio ? ((addr >> 3) & ~IntAddrPrefixMask) : addr;
        QemuDataRef result{};
        result.is_store = store;
        result.vaddr = functionalAddr;
        result.size = size;
        bool haveResult = false;
        Addr cursor = functionalAddr;
        size_t outputOffset = 0;
        while (outputOffset < size) {
            size_t evidenceIndex = memoryRefs->size();
            size_t evidenceOffset = 0;
            for (size_t index = 0; index < memoryRefs->size(); ++index) {
                const auto &ref = (*memoryRefs)[index];
                if (ref.is_store != store ||
                    bool(ref.attributes & kQemuMemAttrPio) != gem5Pio ||
                    bool(ref.attributes & kQemuMemAttrAtomic) != gem5Atomic ||
                    cursor < ref.vaddr || cursor - ref.vaddr >= ref.size) continue;
                const size_t offset = cursor - ref.vaddr;
                if (consumedMasks[index] & (uint16_t{1} << offset)) continue;
                evidenceIndex = index;
                evidenceOffset = offset;
                break;
            }
            if (evidenceIndex == memoryRefs->size()) break;
            const auto &evidence = (*memoryRefs)[evidenceIndex];
            const size_t available = std::min<size_t>(
                evidence.size - evidenceOffset, size - outputOffset);
            size_t chunk = 0;
            while (chunk < available &&
                   !(consumedMasks[evidenceIndex] &
                     (uint16_t{1} << (evidenceOffset + chunk)))) ++chunk;
            if (chunk == 0) break;
            if (!store) {
                fatal_if(!evidence.hasValue || evidenceOffset + chunk > evidence.valueSize,
                         "QEMU load value is unavailable pc=%#x mnemonic=%s "
                         "addr=%#x size=%zu", macroPc, macroName->c_str(),
                         cursor, chunk);
                std::copy_n(evidence.value.begin() + evidenceOffset, chunk,
                            result.value.begin() + outputOffset);
            }
            if (!haveResult) {
                result.paddr = evidence.paddr + evidenceOffset;
                result.virtual_page = functionalAddr / kFastSimPageSize;
                result.virtual_page_token = evidence.virtual_page_token;
                result.attributes = evidence.attributes;
                haveResult = true;
            } else {
                const bool crossedVirtualPage =
                    (cursor & (kFastSimPageSize - 1)) == 0 && evidenceOffset == 0;
                fatal_if((!crossedVirtualPage &&
                          result.paddr + outputOffset != evidence.paddr + evidenceOffset) ||
                         result.attributes != evidence.attributes,
                         "QEMU memory evidence is not physically or "
                         "semantically contiguous pc=%#x mnemonic=%s addr=%#x",
                         macroPc, macroName->c_str(), cursor);
            }
            const auto mask = ((uint32_t{1} << chunk) - 1) << evidenceOffset;
            consumedMasks[evidenceIndex] |= static_cast<uint16_t>(mask);
            cursor += chunk;
            outputOffset += chunk;
        }
        const bool mayPadScalarSingleLoad =
            allowScalarSingleLoadPadding && !store && !gem5Pio && !gem5Atomic &&
            size == 8 && outputOffset == 4 && memoryRefs->size() == 1 &&
            memoryRefs->front().vaddr == functionalAddr &&
            memoryRefs->front().size == 4 &&
            !(memoryRefs->front().attributes &
              (kQemuMemAttrPio | kQemuMemAttrMmio | kQemuMemAttrAtomic));
        if (mayPadScalarSingleLoad) {
            std::fill_n(data + outputOffset, size - outputOffset, 0);
            outputOffset = size;
            if (scalarSinglePaddingCount) ++*scalarSinglePaddingCount;
        }
        fatal_if(outputOffset != size,
                 "gem5 dynamic microcode has no covering QEMU memory evidence "
                 "pc=%#x mnemonic=%s op=%s addr=%#x size=%u raw_refs=%zu",
                 macroPc, macroName->c_str(), store ? "write" : "read",
                 functionalAddr, size, memoryRefs->size());
        if (!store) {
            result.valueSize = size;
            result.hasValue = true;
            std::copy_n(result.value.begin(), size, data);
        }
        if ((functionalAddr & (kFastSimPageSize - 1)) + size > kFastSimPageSize) {
            result.virtual_page_token = 0;
        }
        consumedDataRef = std::move(result);
        consumedDataRefValid = true;
    }

    std::array<RegVal, int_reg::NumRegs> intValues = {};
    std::array<uint32_t, int_reg::NumRegs> intGeneration = {};
    std::array<RegVal, float_reg::NumRegs> floatValues = {};
    std::array<uint32_t, float_reg::NumRegs> floatGeneration = {};
    std::array<RegVal, cc_reg::NumRegs> ccValues = {};
    std::array<uint32_t, cc_reg::NumRegs> ccGeneration = {};
    std::array<RegVal, misc_reg::NumRegs> miscValues = {};
    std::array<uint32_t, misc_reg::NumRegs> miscGeneration = {};
    uint32_t generation = 0;
    std::unordered_map<RegId, RegVal> sparseRegisters;
    PCState pc;
    const std::vector<QemuDataRef> *memoryRefs = nullptr;
    std::vector<uint16_t> consumedMasks;
    Addr macroPc = 0;
    const std::string *macroName = nullptr;
    bool allowScalarSingleLoadPadding = false;
    uint64_t *scalarSinglePaddingCount = nullptr;
    QemuDataRef consumedDataRef;
    bool consumedDataRefValid = false;
};

QemuMicrocodeExecutor::QemuMicrocodeExecutor()
    : context(std::make_unique<Context>())
{
}

QemuMicrocodeExecutor::~QemuMicrocodeExecutor() = default;

bool
QemuMicrocodeExecutor::execute(
    const QemuPendingInst &inst, const QemuX86FunctionalState &preState,
    const StaticInstPtr &macro, const std::string &macroMnemonic,
    QemuMicroopDescriptorCache &descriptors,
    std::vector<QemuExpandedMicroop> &expanded,
    std::vector<QemuDataRef> &boundRefs,
    uint64_t *scalarSinglePaddingCount,
    bool &allEvidenceConsumed)
{
    fatal_if(!inst.hasPreState,
             "QEMU user pre-state is incomplete at pc=%#x", inst.pc);
    const bool scalarSingleMemorySource =
        isScalarSingleMemorySource(macroMnemonic) &&
        inst.refs.size() == 1 &&
        !inst.refs.front().is_store && inst.refs.front().size == 4 &&
        inst.refs.front().hasValue;
    context->begin(
        preState, inst.refs, inst.pc, macroMnemonic,
        scalarSingleMemorySource, scalarSinglePaddingCount);
    expanded.clear();
    boundRefs.clear();
    MicroPC microPc = 0;
    for (size_t guard = 0; guard < 65536; ++guard) {
        const auto micro = macro->fetchMicroop(microPc);
        const auto &descriptor = descriptors.get(micro);
        const bool internalBranch = micro->isControl();
        const MicroPC fallthrough = microPc + 1;
        context->setPc(
            inst.pc, static_cast<uint8_t>(inst.size), microPc, fallthrough);
        const Fault fault = micro->execute(context.get(), nullptr);
        fatal_if(fault != NoFault,
                 "retired QEMU instruction faults during gem5 microcode "
                 "execution at pc=%#x micro=%u mnemonic=%s",
                 inst.pc, microPc, descriptor.mnemonic.c_str());
        const MicroPC nextMicroPc = internalBranch
            ? context->nextMicroPc() : fallthrough;
        QemuDataRef consumed;
        const bool hasDataRef = context->takeConsumedDataRef(consumed);
        const size_t dataRefIndex = boundRefs.size();
        if (hasDataRef) {
            boundRefs.push_back(std::move(consumed));
        }
        expanded.push_back({
            &descriptor,
            internalBranch,
            internalBranch && nextMicroPc != fallthrough,
            hasDataRef ? dataRefIndex : std::numeric_limits<size_t>::max(),
        });
        if (micro->isLastMicroop()) {
            allEvidenceConsumed = context->allMemoryEvidenceConsumed();
            return true;
        }
        microPc = nextMicroPc;
    }
    fatal("gem5 microcode did not terminate at pc=%#x", inst.pc);
}

} // namespace X86ISA
} // namespace gem5
