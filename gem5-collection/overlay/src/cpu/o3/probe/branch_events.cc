/*
 * Copyright (c) 2026
 * All rights reserved.
 */

#include "cpu/o3/probe/branch_events.hh"

#include <memory>
#include <string>

#include "base/trace.hh"
#include "cpu/o3/dyn_inst.hh"
#include "debug/BranchEvents.hh"

namespace gem5
{

namespace o3
{

namespace
{

std::string
formatPc(const PCStateBase &pc)
{
    return csprintf("%s", pc);
}

} // namespace

void
BranchEvents::emitEvent(const char *event_name, const DynInstPtr &dynInst)
{
    const auto pc_addr = dynInst->pcState().instAddr();
    const auto disasm = dynInst->staticInst->disassemble(pc_addr);
    const auto pred_target = formatPc(dynInst->readPredTarg());
    std::unique_ptr<PCStateBase> next_pc(dynInst->pcState().clone());
    dynInst->staticInst->advancePC(*next_pc);
    const auto target = formatPc(*next_pc);

    DPRINTFR(
        BranchEvents,
        "[%s]: %s seq=%llu tid=%u pc=0x%08x pred_taken=%d mispred=%d "
        "is_cond=%d is_direct=%d is_indirect=%d pred_target=%s target=%s "
        "disasm=%s.\n",
        name(),
        event_name,
        dynInst->seqNum,
        dynInst->threadNumber,
        pc_addr,
        dynInst->readPredTaken() ? 1 : 0,
        dynInst->mispredicted() ? 1 : 0,
        dynInst->isCondCtrl() ? 1 : 0,
        dynInst->isDirectCtrl() ? 1 : 0,
        dynInst->isIndirectCtrl() ? 1 : 0,
        pred_target,
        target,
        disasm);
}

void
BranchEvents::traceCommit(const DynInstPtr &dynInst)
{
    if (!dynInst->isControl()) {
        return;
    }

    emitEvent("Commit", dynInst);
}

void
BranchEvents::traceMispredict(const DynInstPtr &dynInst)
{
    emitEvent("Mispredict", dynInst);
}

void
BranchEvents::regProbeListeners()
{
    using DynInstListener = ProbeListenerArg<BranchEvents, DynInstPtr>;

    connectListener<DynInstListener>(this, "Commit", &BranchEvents::traceCommit);
    connectListener<DynInstListener>(
        this, "Mispredict", &BranchEvents::traceMispredict);
}

} // namespace o3
} // namespace gem5
