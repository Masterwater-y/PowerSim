/*
 * Copyright (c) 2026
 * All rights reserved.
 */

#ifndef __CPU_O3_PROBE_BRANCH_EVENTS_HH__
#define __CPU_O3_PROBE_BRANCH_EVENTS_HH__

#include <string>

#include "cpu/o3/dyn_inst_ptr.hh"
#include "params/BranchEvents.hh"
#include "sim/probe/probe_listener_object.hh"

namespace gem5
{

namespace o3
{

class BranchEvents : public ProbeListenerObject
{
  public:
    BranchEvents(const BranchEventsParams &params) :
        ProbeListenerObject(params)
    {
    }

    void regProbeListeners() override;

    std::string
    name() const override
    {
        return ProbeListenerObject::name() + ".branch_events";
    }

  private:
    void traceCommit(const DynInstPtr &dynInst);
    void traceMispredict(const DynInstPtr &dynInst);
    void emitEvent(const char *event_name, const DynInstPtr &dynInst);
};

} // namespace o3
} // namespace gem5

#endif  // __CPU_O3_PROBE_BRANCH_EVENTS_HH__
