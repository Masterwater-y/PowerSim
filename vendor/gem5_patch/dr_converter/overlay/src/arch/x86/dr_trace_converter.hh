/* Convert x86 DynamoRIO streams directly into FastSim FST v6. */

#ifndef __ARCH_X86_DR_TRACE_CONVERTER_HH__
#define __ARCH_X86_DR_TRACE_CONVERTER_HH__

#include <array>
#include <cinttypes>
#include <cstdio>
#include <map>
#include <optional>
#include <set>
#include <string>
#include <unordered_map>
#include <utility>
#include <vector>

#include "arch/x86/decoder.hh"
#include "base/types.hh"
#include "cpu/reg_class.hh"
#include "cpu/static_inst_fwd.hh"
#include "drmemtrace/memref.h"
#include "fastsim/fst_format.hpp"
#include "params/X86DrTraceConverter.hh"
#include "sim/eventq.hh"
#include "sim/sim_object.hh"

namespace gem5
{
namespace X86ISA
{

using dynamorio::drmemtrace::memref_t;

class DrTraceConverter : public SimObject
{
  public:
    DrTraceConverter(const X86DrTraceConverterParams &params);
    ~DrTraceConverter() override;

    void startup() override;

  private:
    struct DataRef
    {
        bool is_store = false;
        Addr vaddr = 0;
        Addr paddr = 0;
        uint64_t virtual_page = 0;
        uint32_t virtual_page_token = 0;
        uint64_t size = 0;
    };

    struct AddressSpaceState
    {
        std::map<uint64_t, uint64_t> virtual_to_physical_pages;
        std::map<uint64_t, uint32_t> virtual_page_tokens;
        uint32_t next_page_token = 1;
    };

    struct DivOperand
    {
        uint64_t sequence = 0;
        uint64_t pc = 0;
        uint64_t rax = 0;
        uint64_t rdx = 0;
        uint64_t divisor = 0;
        uint64_t postRax = 0;
        uint64_t postRdx = 0;
        uint8_t kind = 0;
        uint8_t width = 0;
        uint8_t operandKind = 0;
        uint8_t outcome = 0;
        uint16_t faultCode = 0;
        uint16_t evidenceFlags = 0;
    };

    struct PendingInst
    {
        Addr pc = 0;
        uint64_t size = 0;
        std::array<uint8_t, 16> bytes = {};
        bool is_control = false;
        bool is_cond = false;
        bool is_indirect = false;
        bool is_call = false;
        bool is_return = false;
        bool taken = false;
        Addr actual_next = 0;
        std::vector<DataRef> refs;
        std::optional<DivOperand> divOperand;
    };

    struct ExpandedMicroop
    {
        StaticInstPtr inst;
        bool internalBranch = false;
        bool internalTaken = false;
    };

    using EncodedReg = std::pair<uint8_t, uint32_t>;

    enum class RoiCallKind
    {
        None,
        Begin,
        End,
    };

    struct ThreadState
    {
        int64_t drProcessId = 0;
        int64_t drThreadId = 0;
        PendingInst pending;
        bool havePending = false;
        bool roiActive = false;
        bool roiCompleted = false;
        uint64_t pendingFuncId = 0;
        Addr pendingReturnAddr = 0;
        unsigned roiArgIndex = 0;
        RoiCallKind roiCallKind = RoiCallKind::None;
        bool waitingForRoiArg = false;
        uint64_t roiBeginMarkers = 0;
        uint64_t roiEndMarkers = 0;
        uint64_t logicalCoreId = 0;
        bool logicalCoreIdValid = false;
        uint64_t nextSeq = 1;
        std::unordered_map<uint64_t, uint64_t> lastWriter;
        std::set<uint64_t> usedVirtualPages;
        std::vector<ExpandedMicroop> expandedMicroops;
        std::vector<size_t> memoryMicroops;
        std::vector<DataRef> boundRefs;
        std::vector<EncodedReg> sourceRegs;
        std::vector<EncodedReg> destinationRegs;
        std::vector<fastsim::TraceRecord> outputBuffer;
        uint64_t recordCount = 0;
        uint64_t featureFlags = 0;
        std::FILE *out = nullptr;
    };

    X86ISA::Decoder *decoder;
    const std::string inputTrace;
    const std::string outputDir;
    const uint64_t roiBeginFuncId;
    const uint64_t roiEndFuncId;
    const uint32_t expectedNumCores;
    const std::string divSidecarDir;
    EventFunctionWrapper convertEvent;
    std::unordered_map<int64_t, ThreadState> threads;
    std::map<uint64_t, int64_t> coreThreads;
    std::map<uint64_t, int64_t> coreAddressSpaces;
    std::map<int64_t, AddressSpaceState> addressSpaces;
    using DivThreadKey = std::pair<int64_t, int64_t>;
    std::map<DivThreadKey, std::vector<DivOperand>> divOperands;
    std::map<DivThreadKey, size_t> nextDivOperand;
    bool pendingPhysicalAddressValid = false;
    uint64_t pendingPhysicalAddress = 0;
    int64_t pendingPhysicalAddressPid = 0;

    void convert();
    ThreadState &threadState(int64_t pid, int64_t tid);
    AddressSpaceState &addressSpace(int64_t pid);
    void recordPhysicalAddressMarker(const memref_t &memref);
    void recordVirtualAddressMarker(const memref_t &memref);
    void handleInstruction(ThreadState &state, const memref_t &memref);
    void handleData(ThreadState &state, const memref_t &memref, bool isStore);
    void handleMarker(ThreadState &state, const memref_t &memref);
    DataRef translateAddress(ThreadState &state, const memref_t &memref,
                              bool isStore);
    void emitInstruction(ThreadState &state, PendingInst &inst,
                         const StaticInstPtr &macro);
    void flushPending(ThreadState &state, Addr nextInstrPc);
    void openCoreOutput(ThreadState &state);
    void flushOutput(ThreadState &state);
    void closeOutputs();
    void writeAddressProvenance() const;
    StaticInstPtr decodeMacro(const PendingInst &inst);
    DivOperand consumeDivOperand(ThreadState &state, const PendingInst &inst,
                                 const StaticInstPtr &macro);
    void replayDivisionMicroops(const PendingInst &inst,
                                const StaticInstPtr &macro,
                                std::vector<ExpandedMicroop> &expanded);
    void loadDivOperands();
    void writeRecord(ThreadState &state, const PendingInst &inst,
                     const StaticInstPtr &micro, size_t microPc,
                     size_t microCount, const DataRef *dataRef,
                     bool internalBranch = false, bool internalTaken = false);
    void writeSyscallRecord(ThreadState &state, Addr pc, uint64_t sysnum);
    void finalizeOutput(ThreadState &state);
    void trackedRegs(const StaticInstPtr &inst, bool sources,
                     std::vector<EncodedReg> &regs) const;
    void fillProducerFacts(ThreadState &state,
                           const std::vector<EncodedReg> &sources,
                           std::array<uint64_t, 4> &distances,
                           std::array<uint8_t, 4> &classes);
    void updateWriters(ThreadState &state,
                       const std::vector<EncodedReg> &destinations);
    uint64_t regKey(uint8_t cls, uint32_t index) const;
};

} // namespace X86ISA
} // namespace gem5

#endif // __ARCH_X86_DR_TRACE_CONVERTER_HH__
