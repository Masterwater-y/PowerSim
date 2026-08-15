#include "arch/x86/dr_trace_converter.hh"

#include <algorithm>
#include <array>
#include <cerrno>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <map>
#include <regex>
#include <utility>

#include "arch/x86/insts/macroop.hh"
#include "arch/x86/pcstate.hh"
#include "arch/x86/insts/microldstop.hh"
#include "base/logging.hh"
#include "cpu/exec_context.hh"
#include "cpu/static_inst.hh"
#include "debug/Decode.hh"
#include "sim/sim_exit.hh"

#include "fastsim/div_sidecar.h"

#include "drmemtrace/memref.h"
#include "drmemtrace/scheduler.h"
#include "drmemtrace/trace_entry.h"

namespace gem5
{
namespace X86ISA
{
namespace
{

using dynamorio::drmemtrace::memref_t;
using dynamorio::drmemtrace::scheduler_t;
using dynamorio::drmemtrace::trace_type_t;

constexpr uint64_t kFastSimPageSize = 4096;
constexpr size_t kFstOutputRecordsPerBlock = 16 * 1024;

/* Executes one decoded DIV/IDIV macro-op without a CPU model.  This is
 * deliberately a register/PC shell: the ISA-generated micro-op execute()
 * methods remain the sole owner of division semantics and loop control. */
class DivReplayContext final : public ExecContext
{
  public:
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
        return &registers[inst->destRegIdx(index)];
    }

    void setRegOperand(const StaticInst *inst, int index, RegVal value) override
    {
        registers[inst->destRegIdx(index)] = value;
    }

    void setRegOperand(const StaticInst *inst, int index,
                       const void *value) override
    {
        setRegOperand(inst, index, *static_cast<const RegVal *>(value));
    }

    RegVal readMiscRegOperand(const StaticInst *, int) override { return 0; }
    void setMiscRegOperand(const StaticInst *, int, RegVal) override {}
    RegVal readMiscReg(int) override { return 0; }
    void setMiscReg(int, RegVal) override {}
    const PCStateBase &pcState() const override { return pc; }
    void pcState(const PCStateBase &value) override { pc = value.as<PCState>(); }
    Fault initiateMemMgmtCmd(Request::Flags) override { return NoFault; }
    Fault writeMem(uint8_t *, unsigned int, Addr, Request::Flags, uint64_t *,
                   const std::vector<bool> &) override { return NoFault; }
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

    void set(const RegId &reg, RegVal value) { registers[reg] = value; }
    RegVal get(const RegId &reg) const
    {
        const auto found = registers.find(reg);
        return found == registers.end() ? 0 : found->second;
    }

    void setPc(Addr addr, MicroPC upc, MicroPC nupc)
    {
        pc = PCState(addr);
        pc.upc(upc);
        pc.nupc(nupc);
    }

    MicroPC nextMicroPc() const { return pc.nupc(); }

  private:
    std::map<RegId, RegVal> registers;
    PCState pc;
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
isMarker(trace_type_t type)
{
    return type == dynamorio::drmemtrace::TRACE_TYPE_MARKER;
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

DrTraceConverter::DrTraceConverter(const X86DrTraceConverterParams &params)
    : SimObject(params),
      decoder(params.decoder),
      inputTrace(params.input_trace),
      outputDir(params.output_dir),
      roiBeginFuncId(params.roi_begin_func_id),
      roiEndFuncId(params.roi_end_func_id),
      expectedNumCores(params.expected_num_cores),
      divSidecarDir(params.div_sidecar_dir),
      convertEvent([this] { convert(); }, name() + ".convert")
{
    fatal_if(!decoder, "X86DrTraceConverter requires a decoder");
    fatal_if(roiBeginFuncId == roiEndFuncId,
             "x86 drmemtrace ROI function ids must differ");
    fatal_if(expectedNumCores == 0,
             "x86 drmemtrace conversion requires at least one core");
    fatal_if(!std::filesystem::is_directory(outputDir),
             "x86 drmemtrace output directory does not exist: %s", outputDir);
    fatal_if(!std::filesystem::is_directory(divSidecarDir),
             "DIV operand sidecar directory does not exist: %s", divSidecarDir);
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

void
DrTraceConverter::loadDivOperands()
{
    const auto manifest = std::filesystem::path(divSidecarDir) / "manifest.json";
    fatal_if(!std::filesystem::is_regular_file(manifest),
             "canonical DIV sidecar manifest is missing: %s; recollect trace",
             manifest.c_str());
    const std::regex filePattern(R"(^div\.([0-9]+)\.([0-9]+)\.bin$)");
    bool haveCaptureId = false;
    uint64_t captureIdHi = 0;
    uint64_t captureIdLo = 0;
    for (const auto &entry : std::filesystem::directory_iterator(divSidecarDir)) {
        if (!entry.is_regular_file()) continue;
        const std::string name = entry.path().filename().string();
        if (name == "manifest.json") {
            continue;
        }
        std::smatch match;
        fatal_if(!std::regex_match(name, match, filePattern),
                 "unexpected file in DIV sidecar directory: %s",
                 entry.path().c_str());
        int64_t pid = 0;
        int64_t tid = 0;
        try {
            pid = std::stoll(match[1].str());
            tid = std::stoll(match[2].str());
        } catch (const std::exception &) {
            fatal("invalid DIV sidecar file name: %s", entry.path().c_str());
        }
        std::ifstream input(entry.path(), std::ios::binary);
        fastsim_div_sidecar_header_t header{};
        input.read(reinterpret_cast<char *>(&header), sizeof(header));
        fatal_if(!input || header.magic != FASTSIM_DIV_SIDECAR_MAGIC ||
                     header.version != FASTSIM_DIV_SIDECAR_VERSION ||
                     header.header_size != sizeof(header) ||
                     header.record_size != sizeof(fastsim_div_sidecar_record_t) ||
                     header.flags != 0 ||
                     header.pid != pid || header.tid != tid,
                 "invalid DIV sidecar header: %s", entry.path().c_str());
        if (!haveCaptureId) {
            captureIdHi = header.capture_id_hi;
            captureIdLo = header.capture_id_lo;
            haveCaptureId = true;
        }
        fatal_if(header.capture_id_hi != captureIdHi ||
                     header.capture_id_lo != captureIdLo,
                 "DIV sidecar capture ids differ: %s", entry.path().c_str());
        const DivThreadKey key{pid, tid};
        fatal_if(divOperands.count(key),
                 "duplicate DIV sidecar pid=%" PRId64 " tid=%" PRId64,
                 pid, tid);
        auto &records = divOperands[key];
        while (true) {
            fastsim_div_sidecar_record_t raw{};
            input.read(reinterpret_cast<char *>(&raw), sizeof(raw));
            if (!input) {
                fatal_if(input.gcount() != 0,
                         "truncated DIV sidecar record in %s",
                         entry.path().c_str());
                break;
            }
            fatal_if(raw.sequence != records.size() ||
                         (raw.kind != FASTSIM_DIV_KIND_DIV &&
                          raw.kind != FASTSIM_DIV_KIND_IDIV) ||
                         (raw.width != 1 && raw.width != 2 &&
                          raw.width != 4 && raw.width != 8) ||
                         (raw.operand_kind != FASTSIM_DIV_OPERAND_REGISTER &&
                          raw.operand_kind != FASTSIM_DIV_OPERAND_MEMORY) ||
                         (raw.outcome != FASTSIM_DIV_OUTCOME_RETIRED &&
                          raw.outcome != FASTSIM_DIV_OUTCOME_FAULTED) ||
                         (raw.flags & ~FASTSIM_DIV_EVIDENCE_DIVISOR_VALID) ||
                         (raw.outcome == FASTSIM_DIV_OUTCOME_RETIRED &&
                          raw.fault_code != 0),
                     "invalid DIV sidecar record in %s", entry.path().c_str());
            DivOperand operand;
            operand.sequence = raw.sequence;
            operand.pc = raw.pc;
            operand.rax = raw.rax;
            operand.rdx = raw.rdx;
            operand.divisor = raw.divisor;
            operand.postRax = raw.post_rax;
            operand.postRdx = raw.post_rdx;
            operand.kind = raw.kind;
            operand.width = raw.width;
            operand.operandKind = raw.operand_kind;
            operand.outcome = raw.outcome;
            operand.faultCode = raw.fault_code;
            operand.evidenceFlags = raw.flags;
            records.push_back(operand);
        }
    }
    fatal_if(!haveCaptureId || (captureIdHi == 0 && captureIdLo == 0),
             "DIV sidecar directory has no canonical capture: %s",
             divSidecarDir);
}

DrTraceConverter::~DrTraceConverter()
{
    closeOutputs();
}

void
DrTraceConverter::startup()
{
    schedule(convertEvent, curTick());
}


StaticInstPtr
DrTraceConverter::decodeMacro(const PendingInst &inst)
{
    PCState pc(inst.pc);
    decoder->reset();
    const size_t chunk_size = decoder->moreBytesSize();
    for (uint64_t offset = 0;
         offset < inst.size && decoder->needMoreBytes();
         offset += chunk_size) {
        std::memset(decoder->moreBytesPtr(), 0, chunk_size);
        const uint64_t remaining = std::min<uint64_t>(chunk_size,
                                                      inst.size - offset);
        std::memcpy(decoder->moreBytesPtr(), inst.bytes.data() + offset,
                    remaining);
        decoder->moreBytes(pc, inst.pc + offset);
    }
    if (decoder->instReady()) {
        auto decoded = decoder->decode(pc);
        if (decoded) {
            return decoded;
        }
    }
    fatal("failed to decode x86 instruction at %#x", inst.pc);
}

DrTraceConverter::DivOperand
DrTraceConverter::consumeDivOperand(ThreadState &state, const PendingInst &inst,
                                    const StaticInstPtr &macro)
{
    const DivThreadKey key{state.drProcessId, state.drThreadId};
    auto found = divOperands.find(key);
    auto &cursor = nextDivOperand[key];
    fatal_if(found == divOperands.end() || cursor >= found->second.size(),
             "missing DIV sidecar record pid=%" PRId64 " tid=%" PRId64
             " sequence=%zu trace_pc=%#x",
             key.first, key.second, cursor, inst.pc);
    const DivOperand operand = found->second[cursor];
    fatal_if(operand.sequence != cursor || operand.pc != inst.pc,
             "DIV lockstep mismatch pid=%" PRId64 " tid=%" PRId64
             " sequence=%zu trace_pc=%#x sidecar_pc=%#x",
             key.first, key.second, cursor, inst.pc, operand.pc);
    if (macro) {
        const std::string mnemonic = macro->getName();
        const bool signedDiv = mnemonic == "idiv";
        fatal_if(!signedDiv && mnemonic != "div",
                 "internal-control macro is not scalar DIV/IDIV at pc=%#x: %s",
                 inst.pc, mnemonic);
        fatal_if(operand.kind != (signedDiv ? FASTSIM_DIV_KIND_IDIV :
                                             FASTSIM_DIV_KIND_DIV),
                 "DIV kind mismatch at pc=%#x sequence=%zu", inst.pc, cursor);
        const auto *macroop = dynamic_cast<const MacroopBase *>(macro.get());
        fatal_if(!macroop,
                 "DIV instruction is not an x86 MacroopBase at pc=%#x", inst.pc);
        auto *mutableMacroop = const_cast<MacroopBase *>(macroop);
        const auto env = mutableMacroop->getEmulEnv();
        const auto machInst = mutableMacroop->getExtMachInst();
        fatal_if(env.dataSize != operand.width,
                 "DIV width mismatch at pc=%#x sequence=%zu trace=%d sidecar=%u",
                 inst.pc, cursor, env.dataSize, unsigned(operand.width));
        const uint8_t expectedKind = machInst.modRM.mod == 3 ?
            FASTSIM_DIV_OPERAND_REGISTER : FASTSIM_DIV_OPERAND_MEMORY;
        fatal_if(operand.operandKind != expectedKind,
                 "DIV operand-kind mismatch at pc=%#x sequence=%zu",
                 inst.pc, cursor);
    }
    ++cursor;
    return operand;
}

void
DrTraceConverter::replayDivisionMicroops(
    const PendingInst &inst, const StaticInstPtr &macro,
    std::vector<ExpandedMicroop> &expanded)
{
    fatal_if(!inst.divOperand,
             "DIV instruction has no lockstep sidecar record at pc=%#x", inst.pc);
    const auto &operand = *inst.divOperand;
    const auto *macroop = dynamic_cast<const MacroopBase *>(macro.get());
    fatal_if(!macroop, "DIV instruction is not an x86 MacroopBase at pc=%#x",
             inst.pc);
    auto *mutableMacroop = const_cast<MacroopBase *>(macroop);
    const auto env = mutableMacroop->getEmulEnv();
    const auto machInst = mutableMacroop->getExtMachInst();

    DivReplayContext context;
    context.set(intRegClass[int_reg::Rax], operand.rax);
    context.set(intRegClass[int_reg::Rdx], operand.rdx);
    if (operand.operandKind == FASTSIM_DIV_OPERAND_REGISTER) {
        context.set(intRegFolded(env.reg,
                    operand.width == 1 && !machInst.rex.present ?
                        IntFoldBit : 0), operand.divisor);
    }

    expanded.clear();
    MicroPC microPc = 0;
    size_t branches = 0;
    for (size_t guard = 0; guard < 4096; ++guard) {
        auto micro = macro->fetchMicroop(microPc);
        const bool internalBranch = micro->isControl();
        const MicroPC fallthrough = microPc + 1;
        context.setPc(inst.pc, microPc, fallthrough);
        if (operand.operandKind == FASTSIM_DIV_OPERAND_MEMORY &&
            micro->isLoad()) {
            fatal_if(micro->numDestRegs() != 1,
                     "DIV memory operand load has unexpected destinations at pc=%#x",
                     inst.pc);
            context.set(micro->destRegIdx(0), operand.divisor);
        } else {
            const Fault fault = micro->execute(&context, nullptr);
            fatal_if(fault != NoFault,
                     "retired DIV faults during gem5 micro-op replay at pc=%#x micro=%u",
                     inst.pc, microPc);
        }
        const MicroPC nextMicroPc = internalBranch ? context.nextMicroPc() :
                                                     fallthrough;
        const bool internalTaken = internalBranch && nextMicroPc != fallthrough;
        expanded.push_back({micro, internalBranch, internalTaken});
        if (internalBranch)
            ++branches;
        if (micro->isLastMicroop()) {
            fatal_if(branches == 0,
                     "DIV microcode has no internal branch at pc=%#x", inst.pc);
            const uint64_t width_mask = operand.width == 8 ? UINT64_MAX :
                ((uint64_t(1) << (operand.width * 8)) - 1);
            const auto verify = [&](uint64_t actual, uint64_t expected,
                                    const char *name) {
                if (operand.width == 4) {
                    fatal_if(uint32_t(actual) != uint32_t(expected),
                             "DIV replay %s mismatch at pc=%#x", name, inst.pc);
                } else if (operand.width == 1) {
                    fatal_if(uint16_t(actual) != uint16_t(expected),
                             "DIV replay %s mismatch at pc=%#x", name, inst.pc);
                } else {
                    fatal_if((actual & width_mask) != (expected & width_mask),
                             "DIV replay %s mismatch at pc=%#x", name, inst.pc);
                }
            };
            verify(context.get(intRegClass[int_reg::Rax]), operand.postRax, "RAX");
            verify(context.get(intRegClass[int_reg::Rdx]), operand.postRdx, "RDX");
            return;
        }
        microPc = nextMicroPc;
    }
    fatal("DIV microcode did not terminate at pc=%#x", inst.pc);
}

void
DrTraceConverter::trackedRegs(const StaticInstPtr &inst, bool sources,
                              std::vector<EncodedReg> &regs) const
{
    regs.clear();
    const int count = sources ? inst->numSrcRegs() : inst->numDestRegs();
    regs.reserve(count);
    for (int i = 0; i < count; ++i) {
        const RegId &reg = sources ? inst->srcRegIdx(i) : inst->destRegIdx(i);
        uint8_t cls = 255;
        switch (reg.classValue()) {
          case IntRegClass: cls = 0; break;
          case FloatRegClass: cls = 1; break;
          case VecRegClass: cls = 2; break;
          case CCRegClass: cls = 3; break;
          default: continue;
        }
        regs.emplace_back(cls, static_cast<uint32_t>(reg.index()));
    }
    std::sort(regs.begin(), regs.end());
    regs.erase(std::unique(regs.begin(), regs.end()), regs.end());
}

void
DrTraceConverter::fillProducerFacts(ThreadState &state,
                                    const std::vector<EncodedReg> &sources,
                                    std::array<uint64_t, 4> &distances,
                                    std::array<uint8_t, 4> &classes)
{
    distances.fill(0);
    classes.fill(255);
    std::vector<std::pair<uint64_t, uint8_t>> producers;
    producers.reserve(sources.size());
    for (const auto &[cls, index] : sources) {
        const uint64_t key = regKey(cls, index);
        auto it = state.lastWriter.find(key);
        if (it == state.lastWriter.end()) {
            continue;
        }
        producers.emplace_back(state.nextSeq - it->second, cls);
    }
    std::sort(producers.begin(), producers.end());
    for (size_t i = 0; i < producers.size() && i < distances.size(); ++i) {
        distances[i] = producers[i].first;
        classes[i] = producers[i].second;
    }
}

void
DrTraceConverter::updateWriters(
    ThreadState &state,
    const std::vector<EncodedReg> &destinations)
{
    for (const auto &[cls, index] : destinations) {
        const uint64_t key = regKey(cls, index);
        state.lastWriter[key] = state.nextSeq;
    }
}

uint64_t
DrTraceConverter::regKey(uint8_t cls, uint32_t index) const
{
    return (uint64_t(cls) << 32) | uint64_t(index);
}

DrTraceConverter::ThreadState &
DrTraceConverter::threadState(int64_t pid, int64_t tid)
{
    auto [it, inserted] = threads.try_emplace(tid);
    if (inserted) {
        it->second.drProcessId = pid;
        it->second.drThreadId = tid;
    }
    fatal_if(it->second.drProcessId != pid,
             "DR thread id %" PRId64 " appears in multiple processes", tid);
    return it->second;
}

DrTraceConverter::AddressSpaceState &
DrTraceConverter::addressSpace(int64_t pid)
{
    return addressSpaces[pid];
}

void
DrTraceConverter::recordPhysicalAddressMarker(const memref_t &memref)
{
    fatal_if(pendingPhysicalAddressValid,
             "physical-address marker is not followed by a virtual-address marker");
    pendingPhysicalAddress = memref.marker.marker_value;
    pendingPhysicalAddressPid = memref.marker.pid;
    pendingPhysicalAddressValid = true;
}

void
DrTraceConverter::recordVirtualAddressMarker(const memref_t &memref)
{
    fatal_if(!pendingPhysicalAddressValid,
             "virtual-address marker has no preceding physical-address marker");
    const uint64_t vaddr = memref.marker.marker_value;
    const uint64_t paddr = pendingPhysicalAddress;
    pendingPhysicalAddressValid = false;
    fatal_if(pendingPhysicalAddressPid != memref.marker.pid,
             "physical/virtual marker pair crosses address spaces");
    fatal_if((vaddr & (kFastSimPageSize - 1)) !=
                 (paddr & (kFastSimPageSize - 1)),
             "physical/virtual marker page offsets differ vaddr=%#x paddr=%#x",
             vaddr, paddr);
    const uint64_t virtualPage = vaddr / kFastSimPageSize;
    const uint64_t physicalPage = paddr / kFastSimPageSize;
    fatal_if(physicalPage == 0,
             "physical-address marker has a masked or zero PFN for virtual page %#x",
             virtualPage);
    auto &space = addressSpace(memref.marker.pid);
    auto [it, inserted] = space.virtual_to_physical_pages.emplace(
        virtualPage, physicalPage);
    fatal_if(!inserted && it->second != physicalPage,
             "virtual page %#x remapped from physical page %#x to %#x",
             virtualPage, it->second, physicalPage);
}

void
DrTraceConverter::openCoreOutput(ThreadState &state)
{
    fatal_if(!state.logicalCoreIdValid,
             "ROI logical core was not observed for DR thread %" PRId64,
             state.drThreadId);
    if (state.out) {
        return;
    }
    const std::string path = outputDir + "/core" +
        std::to_string(state.logicalCoreId) + ".fst";
    state.out = std::fopen(path.c_str(), "w+b");
    fatal_if(!state.out, "failed to open FastSim FST output %s: %s",
             path, std::strerror(errno));
    fastsim::FstHeader header;
    header.core_id = static_cast<uint32_t>(state.logicalCoreId);
    fatal_if(std::fwrite(&header, sizeof(header), 1, state.out) != 1,
             "failed to write FastSim FST header %s", path);
    state.outputBuffer.reserve(kFstOutputRecordsPerBlock);
}

void
DrTraceConverter::flushOutput(ThreadState &state)
{
    if (state.outputBuffer.empty()) return;
    const auto written = std::fwrite(state.outputBuffer.data(),
                                     sizeof(fastsim::TraceRecord),
                                     state.outputBuffer.size(), state.out);
    fatal_if(written != state.outputBuffer.size(),
             "failed writing FastSim FST output core=%" PRIu64,
             state.logicalCoreId);
    state.outputBuffer.clear();
}

void
DrTraceConverter::writeRecord(ThreadState &state, const PendingInst &inst,
                               const StaticInstPtr &micro, size_t microPc,
                               size_t microCount, const DataRef *dataRef,
                               bool internalBranch, bool internalTaken)
{
    std::array<uint64_t, 4> distances;
    std::array<uint8_t, 4> classes;
    trackedRegs(micro, true, state.sourceRegs);
    trackedRegs(micro, false, state.destinationRegs);
    const auto &sources = state.sourceRegs;
    const auto &destinations = state.destinationRegs;
    fillProducerFacts(state, sources, distances, classes);

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
    setFlag(fastsim::kAtomic, is_memory && micro->isAtomic());
    setFlag(fastsim::kPhysicalAddress, is_memory);
    setFlag(fastsim::kBranch, branch);
    setFlag(fastsim::kConditional,
            internalBranch ? micro->isCondCtrl() : branch && inst.is_cond);
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
    fatal_if(sources.size() > UINT8_MAX || destinations.size() > UINT8_MAX,
             "register count overflow at pc=%#x", inst.pc);
    record.n_src = static_cast<uint8_t>(sources.size());
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
        fatal_if(crossPage,
                 "cross-page memory reference cannot be represented in FST v6");
        const auto token = dataRef->virtual_page_token;
        fatal_if(!crossPage && (token == 0 ||
                 token >= fastsim::kDestinationClassCountsMarker),
                 "invalid virtual page token at pc=%#x", inst.pc);
        if (!crossPage) {
            record.reserved = fastsim::kDestinationClassCountsMarker | token;
            record.flags = record.flags | fastsim::kVirtualPageToken;
            state.featureFlags |= fastsim::kFstFeatureVirtualPageTokens;
        }
    }
    state.outputBuffer.push_back(record);
    if (state.outputBuffer.size() == kFstOutputRecordsPerBlock) {
        flushOutput(state);
    }
    state.featureFlags |= fastsim::kFstFeatureDestinationClassCounts;
    ++state.recordCount;

    updateWriters(state, destinations);
    ++state.nextSeq;
}

void
DrTraceConverter::writeSyscallRecord(ThreadState &state, Addr pc,
                                     uint64_t sysnum)
{
    openCoreOutput(state);
    fastsim::TraceRecord record;
    record.pc = pc;
    record.op_class = fastsim::kSyscallOpClass;
    record.flags = record.flags | fastsim::kSerialize |
                   fastsim::kLastMicroOp;
    record.set_syscall_number(sysnum);
    std::array<uint8_t, fastsim::kTrackedRegisterClasses> producers{
        255, 255, 255, 255};
    std::array<uint8_t, fastsim::kTrackedRegisterClasses> destinations{};
    record.set_register_class_metadata(producers, destinations);
    state.outputBuffer.push_back(record);
    if (state.outputBuffer.size() == kFstOutputRecordsPerBlock) {
        flushOutput(state);
    }
    state.featureFlags |= fastsim::kFstFeatureSyscallMarkers |
                          fastsim::kFstFeatureDestinationClassCounts;
    ++state.recordCount;
    ++state.nextSeq;
}

void
DrTraceConverter::finalizeOutput(ThreadState &state)
{
    if (!state.out) return;
    flushOutput(state);
    fastsim::FstHeader header;
    header.core_id = static_cast<uint32_t>(state.logicalCoreId);
    header.record_count = state.recordCount;
    header.feature_flags = state.featureFlags;
    fatal_if(std::fseek(state.out, 0, SEEK_SET) != 0 ||
             std::fwrite(&header, sizeof(header), 1, state.out) != 1,
             "failed finalizing FastSim FST core=%" PRIu64,
             state.logicalCoreId);
    std::fflush(state.out);
    std::fclose(state.out);
    state.out = nullptr;
}

void
DrTraceConverter::emitInstruction(ThreadState &state, PendingInst &inst,
                                  const StaticInstPtr &macro)
{
    auto &ops = state.expandedMicroops;
    ops.clear();
    if (inst.divOperand) {
        replayDivisionMicroops(inst, macro, ops);
    } else {
        if (!macro->isMacroop()) {
            ops.push_back({macro, false, false});
        } else {
            for (MicroPC microPc = 0;; ++microPc) {
                auto micro = macro->fetchMicroop(microPc);
                fatal_if(!inst.is_control && micro->isControl(),
                         "FASTSIM_UNSUPPORTED reason_code=dynamic_internal_microcode_control "
                         "pc=%#x: dynamic internal microcode needs dedicated evidence",
                         inst.pc);
                ops.push_back({micro, false, false});
                if (micro->isLastMicroop()) break;
            }
        }
    }
    openCoreOutput(state);
    auto &mem_ops = state.memoryMicroops;
    mem_ops.clear();
    for (size_t i = 0; i < ops.size(); ++i) {
        if (ops[i].inst->isLoad() || ops[i].inst->isStore() ||
            ops[i].inst->isAtomic()) {
            fatal_if(ops[i].inst->isAtomic(),
                     "atomic memory UOP is unsupported at pc=%#x micro=%zu",
                     inst.pc, i);
            mem_ops.push_back(i);
        }
    }
    auto &bound_refs = state.boundRefs;
    bound_refs.clear();
    if (mem_ops.size() == inst.refs.size()) {
        for (size_t index = 0; index < mem_ops.size(); ++index) {
            const auto *mem_op = dynamic_cast<const MemOp *>(
                ops[mem_ops[index]].inst.get());
            fatal_if(!mem_op,
                     "memory micro-op has no x86 data size at "
                     "pc=%#x micro=%zu", inst.pc, mem_ops[index]);
            const auto &ref = inst.refs[index];
            bound_refs.push_back(
                {ref.is_store, ref.vaddr, ref.paddr, ref.virtual_page,
                 ref.virtual_page_token, mem_op->dataSize});
        }
    } else if (inst.refs.size() == 1 && mem_ops.size() > 1) {
        const auto &ref = inst.refs.front();
        uint64_t total_size = 0;
        uint64_t min_disp = UINT64_MAX;
        for (const size_t micro_index : mem_ops) {
            const auto *mem_op = dynamic_cast<const MemOp *>(
                ops[micro_index].inst.get());
            const auto *addr_op = dynamic_cast<const AddrOp *>(
                ops[micro_index].inst.get());
            fatal_if(!mem_op || !addr_op,
                     "memory micro-op has no x86 size/address operands "
                     "at pc=%#x micro=%zu", inst.pc, micro_index);
            total_size += mem_op->dataSize;
            min_disp = std::min<uint64_t>(min_disp, addr_op->disp);
        }
        fatal_if(total_size != ref.size,
                 "memory reference size mismatch at pc=%#x: "
                 "ref_size=%zu micro_total=%zu",
                 inst.pc, ref.size, total_size);
        for (const size_t micro_index : mem_ops) {
            const auto *mem_op = dynamic_cast<const MemOp *>(
                ops[micro_index].inst.get());
            const auto *addr_op = dynamic_cast<const AddrOp *>(
                ops[micro_index].inst.get());
            const uint64_t offset = addr_op->disp - min_disp;
            fatal_if(offset + mem_op->dataSize > ref.size,
                     "memory micro-op slice exceeds reference at "
                     "pc=%#x micro=%zu", inst.pc, micro_index);
            bound_refs.push_back(
                {ref.is_store, ref.vaddr + offset, ref.paddr + offset,
                 (ref.vaddr + offset) / kFastSimPageSize,
                 ref.virtual_page_token, mem_op->dataSize});
        }
    } else {
        fatal("memory reference count mismatch at pc=%#x: "
              "refs=%zu mem_uops=%zu",
              inst.pc, inst.refs.size(), mem_ops.size());
    }
    for (size_t index = 0; index < mem_ops.size(); ++index) {
        const auto &op = ops[mem_ops[index]].inst;
        const auto &ref = bound_refs[index];
        fatal_if(ref.is_store ? !op->isStore() : !op->isLoad(),
                 "memory reference type mismatch at pc=%#x micro=%zu: "
                 "ref=%s load=%u store=%u",
                 inst.pc, mem_ops[index], ref.is_store ? "write" : "read",
                 unsigned(op->isLoad()), unsigned(op->isStore()));
    }
    size_t ref_idx = 0;
    for (size_t i = 0; i < ops.size(); ++i) {
        const DataRef *ref = nullptr;
        if (ref_idx < mem_ops.size() && mem_ops[ref_idx] == i) {
            ref = &bound_refs[ref_idx++];
        }
        writeRecord(state, inst, ops[i].inst, i, ops.size(), ref,
                    ops[i].internalBranch, ops[i].internalTaken);
    }
}

void
DrTraceConverter::flushPending(ThreadState &state, Addr nextInstrPc)
{
    if (!state.havePending) {
        return;
    }
    if (nextInstrPc != 0) {
        state.pending.actual_next = nextInstrPc;
    }
    const auto macro = decodeMacro(state.pending);
    const std::string mnemonic = macro->getName();
    if (mnemonic == "div" || mnemonic == "idiv") {
        state.pending.divOperand = consumeDivOperand(state, state.pending, macro);
        fatal_if(state.pending.divOperand->outcome != FASTSIM_DIV_OUTCOME_RETIRED,
                 "FASTSIM_UNSUPPORTED reason_code=faulted_div pc=%#x: "
                 "DIV/IDIV did not retire (signal=%u)", state.pending.pc,
                 state.pending.divOperand->faultCode);
        fatal_if(!(state.pending.divOperand->evidenceFlags &
                   FASTSIM_DIV_EVIDENCE_DIVISOR_VALID),
                 "DIV operand evidence is invalid at pc=%#x", state.pending.pc);
    }
    fatal_if(macro->isSyscall(),
             "FASTSIM_UNSUPPORTED reason_code=missing_syscall_marker "
             "pc=%#x: syscall instruction has no DynamoRIO sysnum marker",
             state.pending.pc);
    emitInstruction(state, state.pending, macro);
    state.havePending = false;
}

void
DrTraceConverter::handleInstruction(ThreadState &state, const memref_t &memref)
{
    flushPending(state, memref.instr.addr);
    auto &pending = state.pending;
    pending.pc = memref.instr.addr;
    pending.size = memref.instr.size;
    fatal_if(pending.size > pending.bytes.size(),
             "x86 instruction at %#x has unsupported encoding length %zu",
             pending.pc, pending.size);
    std::memcpy(pending.bytes.data(), memref.instr.encoding, pending.size);
    pending.is_control = isControl(memref.instr.type);
    pending.is_cond = isCond(memref.instr.type);
    pending.is_indirect =
        memref.instr.type ==
            dynamorio::drmemtrace::TRACE_TYPE_INSTR_INDIRECT_JUMP ||
        memref.instr.type ==
            dynamorio::drmemtrace::TRACE_TYPE_INSTR_INDIRECT_CALL ||
        memref.instr.type == dynamorio::drmemtrace::TRACE_TYPE_INSTR_RETURN;
    pending.is_call =
        memref.instr.type ==
            dynamorio::drmemtrace::TRACE_TYPE_INSTR_DIRECT_CALL ||
        memref.instr.type ==
            dynamorio::drmemtrace::TRACE_TYPE_INSTR_INDIRECT_CALL;
    pending.is_return =
        memref.instr.type == dynamorio::drmemtrace::TRACE_TYPE_INSTR_RETURN;
    pending.taken = isTaken(memref.instr.type);
    pending.actual_next = 0;
    pending.refs.clear();
    pending.divOperand.reset();
    if (!state.roiActive) {
        /* Evidence covers the entire trace.  Decode outside ROI too so a
         * missing DIV record cannot be hidden by the ROI filter. */
        const auto macro = decodeMacro(pending);
        const std::string mnemonic = macro->getName();
        if (mnemonic == "div" || mnemonic == "idiv")
            (void)consumeDivOperand(state, pending, macro);
        return;
    }
    state.havePending = true;
}

DrTraceConverter::DataRef
DrTraceConverter::translateAddress(ThreadState &state, const memref_t &memref,
                                   bool isStore)
{
    const Addr vaddr = memref.data.addr;
    const uint64_t size = memref.data.size;
    const uint64_t pageOffset = vaddr & (kFastSimPageSize - 1);
    const uint64_t virtualPage = vaddr / kFastSimPageSize;
    const bool crossesPage = pageOffset + size > kFastSimPageSize;
    fatal_if(crossesPage,
             "cross-page memory reference cannot be represented in strict FST v6");
    fatal_if(!state.logicalCoreIdValid,
             "DR memory reference has no logical-core binding tid=%" PRId64,
             state.drThreadId);
    auto [coreSpace, spaceInserted] = coreAddressSpaces.emplace(
        state.logicalCoreId, memref.data.pid);
    fatal_if(!spaceInserted && coreSpace->second != memref.data.pid,
             "logical core %" PRIu64
             " crosses address spaces in FST v6 (pid=%" PRId64
             " then pid=%" PRId64 ")",
             state.logicalCoreId, coreSpace->second, memref.data.pid);
    auto &space = addressSpace(memref.data.pid);
    const auto mapping = space.virtual_to_physical_pages.find(virtualPage);
    fatal_if(mapping == space.virtual_to_physical_pages.end(),
             "memory reference at %#x has no physical-address marker mapping",
             vaddr);
    const uint64_t physicalPage = mapping->second;
    const Addr paddr = physicalPage * kFastSimPageSize + pageOffset;
    auto [token, inserted] = space.virtual_page_tokens.emplace(
        virtualPage, space.next_page_token);
    if (inserted) {
        fatal_if(space.next_page_token == 0 ||
                 space.next_page_token >= fastsim::kDestinationClassCountsMarker,
                 "virtual page token overflow");
        ++space.next_page_token;
    }
    state.usedVirtualPages.insert(virtualPage);
    return {isStore, vaddr, paddr, virtualPage, token->second, size};
}

void
DrTraceConverter::handleData(ThreadState &state, const memref_t &memref,
                             bool isStore)
{
    if (!state.roiActive) {
        return;
    }
    const auto physical = translateAddress(state, memref, isStore);
    fatal_if(!state.havePending,
             "data reference appears before instruction for DR thread %" PRId64,
             state.drThreadId);
    fatal_if(!isValidDataRef(memref.data.addr, memref.data.size),
             "invalid data reference at pc=%#x addr=%#x size=%zu",
             state.pending.pc, memref.data.addr, memref.data.size);
    state.pending.refs.push_back(physical);
}

void
DrTraceConverter::handleMarker(ThreadState &state, const memref_t &memref)
{
    using namespace dynamorio::drmemtrace;
    const auto markerType = memref.marker.marker_type;
    if (markerType == TRACE_MARKER_TYPE_PHYSICAL_ADDRESS) {
        recordPhysicalAddressMarker(memref);
        return;
    }
    if (markerType == TRACE_MARKER_TYPE_VIRTUAL_ADDRESS) {
        recordVirtualAddressMarker(memref);
        return;
    }
    if (markerType == TRACE_MARKER_TYPE_PHYSICAL_ADDRESS_NOT_AVAILABLE) {
        if (!state.roiActive) {
            return;
        }
        fatal("DR trace has an unavailable physical-address marker "
              "vaddr=%#x tid=%" PRId64 " pid=%" PRId64
              " roi_active=%d roi_begin=%" PRIu64 " roi_end=%" PRIu64
              " roi_completed=%d; strict conversion requires PA markers",
              memref.marker.marker_value, memref.marker.tid, memref.marker.pid,
              state.roiActive, state.roiBeginMarkers, state.roiEndMarkers,
              state.roiCompleted);
    }
    if (markerType == TRACE_MARKER_TYPE_PAGE_SIZE) {
        fatal_if(memref.marker.marker_value != kFastSimPageSize,
                 "DR trace page size=%#x is incompatible with strict FST v6",
                 memref.marker.marker_value);
        return;
    }
    if (markerType == TRACE_MARKER_TYPE_SYSCALL) {
        if (!state.roiActive) return;
        fatal_if(!state.havePending,
                 "syscall marker has no gateway instruction for DR thread %" PRId64,
                 state.drThreadId);
        const auto macro = decodeMacro(state.pending);
        fatal_if(!macro->isSyscall(),
                 "syscall marker follows non-syscall instruction at pc=%#x",
                 state.pending.pc);
        fatal_if(!state.pending.refs.empty(),
                 "syscall gateway unexpectedly owns data references at pc=%#x",
                 state.pending.pc);
        writeSyscallRecord(state, state.pending.pc,
                           memref.marker.marker_value);
        state.pending = PendingInst{};
        state.havePending = false;
        return;
    }
    if (markerType == TRACE_MARKER_TYPE_FUNC_ID) {
        const uint64_t funcId = memref.marker.marker_value;
        const bool isReturnSide =
            state.waitingForRoiArg && state.roiArgIndex == 2 &&
            state.pendingFuncId == funcId;
        if (!isReturnSide) {
            state.pendingFuncId = funcId;
            state.pendingReturnAddr = 0;
            state.roiArgIndex = 0;
            state.roiCallKind = funcId == roiBeginFuncId
                ? RoiCallKind::Begin
                : funcId == roiEndFuncId
                    ? RoiCallKind::End : RoiCallKind::None;
            state.waitingForRoiArg =
                state.roiCallKind != RoiCallKind::None;
        }
        return;
    }
    if (!state.waitingForRoiArg) {
        return;
    }
    if (markerType == TRACE_MARKER_TYPE_FUNC_RETADDR) {
        state.pendingReturnAddr = memref.marker.marker_value;
        return;
    }
    if (markerType == TRACE_MARKER_TYPE_FUNC_ARG) {
        if (state.roiArgIndex == 0) {
            fatal_if(memref.marker.marker_value != 0,
                     "ROI work id must be zero for DR thread %" PRId64,
                     state.drThreadId);
        } else if (state.roiArgIndex == 1) {
            const uint64_t core = memref.marker.marker_value;
            fatal_if(core >= expectedNumCores,
                     "ROI logical core %" PRIu64 " exceeds expected core count %u",
                     core, expectedNumCores);
            if (!state.logicalCoreIdValid) {
                auto [it, inserted] = coreThreads.emplace(
                    core, state.drThreadId);
                fatal_if(!inserted && it->second != state.drThreadId,
                         "ROI logical core %" PRIu64
                         " is claimed by DR threads %" PRId64 " and %" PRId64,
                         core, it->second, state.drThreadId);
                state.logicalCoreId = core;
                state.logicalCoreIdValid = true;
            } else {
                fatal_if(state.logicalCoreId != core,
                         "DR thread %" PRId64
                         " changed logical core from %" PRIu64 " to %" PRIu64,
                         state.drThreadId, state.logicalCoreId, core);
            }
            auto [space, spaceInserted] = coreAddressSpaces.emplace(
                core, memref.marker.pid);
            fatal_if(!spaceInserted && space->second != memref.marker.pid,
                     "logical core %" PRIu64
                     " crosses address spaces in FST v6 (pid=%" PRId64
                     " then pid=%" PRId64 ")",
                     core, space->second, memref.marker.pid);
            if (state.roiCallKind == RoiCallKind::Begin) {
                fatal_if(state.roiBeginMarkers != 0 || state.roiCompleted,
                         "DR thread %" PRId64 " has multiple ROI begin markers",
                         state.drThreadId);
                ++state.roiBeginMarkers;
            } else {
                fatal_if(state.roiEndMarkers != 0 || !state.roiActive,
                         "DR thread %" PRId64 " has unmatched ROI end marker",
                         state.drThreadId);
                state.havePending = false;
                state.pending = PendingInst{};
                state.roiActive = false;
                state.roiCompleted = true;
                ++state.roiEndMarkers;
            }
        }
        ++state.roiArgIndex;
        return;
    }
    if (markerType == TRACE_MARKER_TYPE_FUNC_RETVAL) {
        fatal_if(state.roiArgIndex != 2,
                 "ROI marker for DR thread %" PRId64
                 " expected 2 arguments, observed %u",
                 state.drThreadId, state.roiArgIndex);
        if (state.roiCallKind == RoiCallKind::Begin) {
            fatal_if(state.roiActive || state.roiCompleted,
                     "DR thread %" PRId64 " has nested or repeated ROI",
                     state.drThreadId);
            state.havePending = false;
            state.pending = PendingInst{};
            state.nextSeq = 1;
            state.lastWriter.clear();
            state.roiActive = true;
        }
        state.waitingForRoiArg = false;
        state.pendingFuncId = 0;
        state.pendingReturnAddr = 0;
        state.roiArgIndex = 0;
        state.roiCallKind = RoiCallKind::None;
        return;
    }
    state.waitingForRoiArg = false;
    state.pendingFuncId = 0;
    state.pendingReturnAddr = 0;
    state.roiArgIndex = 0;
    state.roiCallKind = RoiCallKind::None;
}

void
DrTraceConverter::closeOutputs()
{
    for (auto &[tid, state] : threads) {
        (void)tid;
        finalizeOutput(state);
    }
    fatal_if(pendingPhysicalAddressValid,
             "physical-address marker is not followed by a virtual-address marker");
    writeAddressProvenance();
}

void
DrTraceConverter::writeAddressProvenance() const
{
    fatal_if(coreAddressSpaces.size() != expectedNumCores,
             "address provenance covers logical cores=%zu expected=%u",
             coreAddressSpaces.size(), expectedNumCores);
    const std::string path = outputDir + "/address-provenance.json";
    std::ofstream out(path);
    fatal_if(!out, "failed to open address provenance output %s", path);
    out << "{\n  \"schema\": \"fastsim-dr-address-provenance-v1\",\n"
        << "  \"cores\": [\n";
    for (uint64_t core = 0; core < expectedNumCores; ++core) {
        const auto coreSpace = coreAddressSpaces.find(core);
        fatal_if(coreSpace == coreAddressSpaces.end(),
                 "missing address provenance for logical core %" PRIu64, core);
        const auto spaces = addressSpaces.find(coreSpace->second);
        fatal_if(spaces == addressSpaces.end(),
                 "missing address-space state for pid=%" PRId64, coreSpace->second);
        const auto &space = spaces->second;
        const auto thread = coreThreads.find(core);
        fatal_if(thread == coreThreads.end(),
                 "missing thread ownership for logical core %" PRIu64, core);
        const auto state = threads.find(thread->second);
        fatal_if(state == threads.end(),
                 "missing thread state for logical core %" PRIu64, core);
        out << "    {\"core\": " << core << ", \"pid\": "
            << coreSpace->second << ", \"mappings\": [";
        bool first = true;
        for (const uint64_t vpage : state->second.usedVirtualPages) {
            const auto ppage = space.virtual_to_physical_pages.find(vpage);
            const auto token = space.virtual_page_tokens.find(vpage);
            fatal_if(ppage == space.virtual_to_physical_pages.end() ||
                         token == space.virtual_page_tokens.end() ||
                         ppage->second == 0 || token->second == 0,
                     "incomplete address provenance for pid=%" PRId64
                     " virtual_page=%#x", coreSpace->second, vpage);
            if (!first) out << ',';
            out << "{\"virtual_page\": " << vpage
                << ", \"physical_page\": " << ppage->second
                << ", \"token\": " << token->second << '}';
            first = false;
        }
        out << "]}" << (core + 1 == expectedNumCores ? "\n" : ",\n");
    }
    out << "  ]\n}\n";
    fatal_if(!out, "failed writing address provenance output %s", path);
}

void
DrTraceConverter::convert()
{
    loadDivOperands();
    std::vector<scheduler_t::input_workload_t> inputs;
    inputs.emplace_back(inputTrace);
    scheduler_t scheduler;
    auto schedulerOptions = scheduler_t::make_scheduler_serial_options();
    schedulerOptions.flags = static_cast<scheduler_t::scheduler_flags_t>(
        schedulerOptions.flags |
        scheduler_t::SCHEDULER_PROVIDE_PHYSICAL_ADDRESSES);
    auto status = scheduler.init(
        inputs, 1, std::move(schedulerOptions));
    fatal_if(status != scheduler_t::STATUS_SUCCESS,
             "failed to initialize drmemtrace scheduler for %s", inputTrace);
    auto *stream = scheduler.get_stream(0);
    fatal_if(!stream, "failed to get drmemtrace stream");

    memref_t memref;
    while (true) {
        auto stream_status = stream->next_record(memref);
        if (stream_status == scheduler_t::STATUS_EOF) {
            break;
        }
        if (stream_status != scheduler_t::STATUS_OK) {
            fatal("drmemtrace stream error while reading %s", inputTrace);
        }
        const auto type = memref.instr.type;
        const int64_t pid = memref.instr.pid;
        const int64_t tid = memref.instr.tid;
        auto &state = threadState(pid, tid);
        if (isInstr(type)) {
            handleInstruction(state, memref);
        } else if (isRead(type) || isWrite(type)) {
            handleData(state, memref, isWrite(type));
        } else if (isPrefetch(type)) {
            continue;
        } else if (isMarker(type)) {
            handleMarker(state, memref);
        }
    }
    for (auto &[tid, state] : threads) {
        fatal_if(state.roiActive,
                 "DR thread %" PRId64 " ended inside ROI", tid);
        fatal_if(state.roiBeginMarkers != state.roiEndMarkers,
                 "DR thread %" PRId64 " has unmatched ROI markers", tid);
        fatal_if(state.roiBeginMarkers > 1,
                 "DR thread %" PRId64 " has multiple ROI intervals", tid);
        if (state.roiBeginMarkers == 1) {
            fatal_if(state.havePending,
                     "DR thread %" PRId64 " has unfinished ROI instruction", tid);
            fatal_if(state.nextSeq == 1,
                     "DR thread %" PRId64 " produced an empty ROI", tid);
        }
    }
    for (const auto &[key, records] : divOperands) {
        const size_t consumed = nextDivOperand[key];
        fatal_if(consumed != records.size(),
                 "unconsumed DIV sidecar records pid=%" PRId64
                 " tid=%" PRId64 " consumed=%zu records=%zu",
                 key.first, key.second, consumed, records.size());
    }
    fatal_if(coreThreads.size() != expectedNumCores,
             "converted logical cores=%zu expected=%u",
             coreThreads.size(), expectedNumCores);
    for (uint64_t core = 0; core < expectedNumCores; ++core) {
        fatal_if(!coreThreads.count(core),
                 "missing ROI for logical core %" PRIu64, core);
    }
    closeOutputs();
    exitSimLoop("x86 drmemtrace conversion complete", 0);
}

} // namespace X86ISA
} // namespace gem5
