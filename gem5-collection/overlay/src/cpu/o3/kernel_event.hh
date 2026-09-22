/*
 * Copyright (c) 2026
 * SPDX-License-Identifier: BSD-3-Clause
 */

#ifndef __CPU_O3_KERNEL_EVENT_HH__
#define __CPU_O3_KERNEL_EVENT_HH__

#include <cstdint>

#include "base/types.hh"
#include "sim/faults.hh"

namespace gem5
{
namespace o3
{

/** Entry into privileged execution accepted by the O3 commit stage. */
enum class KernelEntrySource : uint8_t
{
    SynchronousFault = 0,
    ExternalInterrupt = 1,
};

struct KernelEntryEvent
{
    uint32_t coreId = 0;
    ThreadID threadId = 0;
    Fault fault = NoFault;
    KernelEntrySource source = KernelEntrySource::SynchronousFault;
    Tick tick = 0;
    Tick clockPeriodTicks = 1;
    bool fromUser = false;
};

} // namespace o3
} // namespace gem5

#endif // __CPU_O3_KERNEL_EVENT_HH__
