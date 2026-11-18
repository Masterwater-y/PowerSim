"""Add FST boundary adapters to a copy of the already-prepared Sniper tree."""
import argparse
import hashlib
import json
import pathlib
import shutil


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source', type=pathlib.Path, required=True)
    p.add_argument('--output', type=pathlib.Path, required=True)
    a = p.parse_args()
    shutil.copytree(a.source, a.output, symlinks=True,
        ignore=shutil.ignore_patterns('build', '.git', '__pycache__'))
    patches = []

    def edit(relative, old, new):
        path = a.output / relative
        text = path.read_text()
        if text.count(old) != 1:
            raise ValueError('expected unique patch anchor: ' + relative)
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        path.write_text(text.replace(old, new))
        patches.append(dict(file=relative, before_sha256=before,
            after_sha256=hashlib.sha256(path.read_bytes()).hexdigest()))

    base = 'common/performance_model/performance_models/'
    timer = base + 'interval_performance_model/interval_timer'
    edit(timer + '.h', '   void free(); // Early-delete of members', '''   void free(); // Early-delete of members
   boost::tuple<uint64_t,uint64_t> drain();
   uint64_t m_fstDispatchedUops = 0, m_fstDispatchedMacros = 0;
   uint64_t m_fstExtraDeclared = 0, m_fstSerialDeclared = 0;
   uint64_t m_fstEofFlushCycles = 0;
   bool m_drain_pending = false;''')
    edit(timer + '.cc', '   m_uops_total = 0;', '''   registerStatsMetric("interval_timer", core->getId(), "fst_dispatched_uops", &m_fstDispatchedUops);
   registerStatsMetric("interval_timer", core->getId(), "fst_dispatched_macros", &m_fstDispatchedMacros);
   registerStatsMetric("interval_timer", core->getId(), "fst_extra_declared", &m_fstExtraDeclared);
   registerStatsMetric("interval_timer", core->getId(), "fst_serial_declared", &m_fstSerialDeclared);
   registerStatsMetric("interval_timer", core->getId(), "fst_eof_flush_cycles", &m_fstEofFlushCycles);
   m_uops_total = 0;''')
    edit(timer + '.cc', '      m_windows->add(*i);', '''      m_drain_pending = true;
      if ((*i)->getMicroOp()->fst_op_class != MicroOp::FST_OP_UNSET)
      {
         m_fstExtraDeclared += (*i)->getMicroOp()->fst_extra_dependencies.size();
         m_fstSerialDeclared += (*i)->getMicroOp()->isSerializing();
      }
      m_windows->add(*i);''')
    edit(timer + '.cc', '      if (micro_op.getDynMicroOp()->isLast())', '''      if (micro_op.getMicroOp()->fst_op_class != MicroOp::FST_OP_UNSET)
      {
         ++m_fstDispatchedUops;
         m_fstDispatchedMacros += micro_op.getDynMicroOp()->isLast() && micro_op.getMicroOp()->fst_counts_macro;
      }
      if (micro_op.getDynMicroOp()->isLast() && micro_op.getMicroOp()->fst_counts_macro)''')
    edit(timer + '.cc', 'boost::tuple<uint64_t, uint64_t> IntervalTimer::dispatchWindow() {', '''// Finish the existing native interval window without fabricated instructions.
// The final implicit flush uses the model's critical-path / memory completion
// coordinates, like its serialization handling. It is an interval endpoint
// estimate, not a cycle-accurate ROB retirement simulation.
boost::tuple<uint64_t,uint64_t> IntervalTimer::drain()
{
   if (!m_drain_pending) return boost::make_tuple(uint64_t(0), uint64_t(0));
   uint64_t instructions = 0, latency = 0;
   while (!m_windows->wIsEmpty())
   {
      uint64_t ins, cycles;
      boost::tie(ins, cycles) = dispatchWindow();
      instructions += ins;
      latency += cycles;
   }
   const uint64_t completion = std::max(m_windows->getCriticalPathTail(),
      std::max(m_max_load_completion_time, m_max_store_completion_time));
   const uint64_t head = m_windows->getCriticalPathHead();
   const uint64_t pending = completion > head ? completion - head : 0;
   const uint64_t tail = std::max(pending,
      m_windows->getEffectiveCriticalPathLength(m_windows->getCriticalPathLength(), false));
   latency += tail;
   m_fstEofFlushCycles += tail;
   m_windows->clearOldWindow(completion);
   m_drain_pending = false;
   return boost::make_tuple(instructions, latency);
}

boost::tuple<uint64_t, uint64_t> IntervalTimer::dispatchWindow() {''')
    perf = base + 'interval_performance_model'
    edit(perf + '.h', '   virtual void notifyElapsedTimeUpdate();',
         '   virtual void notifyElapsedTimeUpdate();\n   virtual boost::tuple<uint64_t,uint64_t> drainTimer();')
    edit(perf + '.cc', 'void IntervalPerformanceModel::notifyElapsedTimeUpdate()', '''boost::tuple<uint64_t,uint64_t> IntervalPerformanceModel::drainTimer()
{
   return interval_timer.drain();
}

void IntervalPerformanceModel::notifyElapsedTimeUpdate()''')
    window = base + 'interval_performance_model/windows'
    edit(window + '.h', '  uint64_t m_next_sequence_number;',
         '  uint64_t m_next_sequence_number;\n  uint64_t m_fstDependenciesAdded = 0;')
    edit(window + '.cc', '#include "log.h"', '#include "log.h"\n#include "stats.h"')
    edit(window + '.cc', '   m_window_size = window_size;', '''   registerStatsMetric("interval_timer", core->getId(), "fst_dependencies_added", &m_fstDependenciesAdded);
   m_window_size = window_size;''')
    edit(window + '.cc', '   m_memory_dependencies->setDependencies(*micro_op, lowestValidSequenceNumber);', '''   m_memory_dependencies->setDependencies(*micro_op, lowestValidSequenceNumber);
   if (micro_op->getMicroOp()->fst_op_class != MicroOp::FST_OP_UNSET)
   {
      const uint64_t seq = micro_op->getSequenceNumber();
      auto add = [&](uint32_t distance) {
         if (distance && distance <= seq && seq-distance >= lowestValidSequenceNumber)
         {
            micro_op->addDependency(seq-distance);
            ++m_fstDependenciesAdded;
         }
      };
      for (unsigned k = 0; k < 4; ++k) add(micro_op->getMicroOp()->fst_producer_dists[k]);
      for (uint32_t distance : micro_op->getMicroOp()->fst_extra_dependencies) add(distance);
   }''')
    # The previous factory returned Nehalem contention for DynamicMicroOpFastSim,
    # which fails the native type check. Use the same SPR pool descriptors as ROB.
    resource = base + 'core_model/interval_contention_fst.h'
    (a.output / resource).write_text('''#ifndef INTERVAL_CONTENTION_FST_H
#define INTERVAL_CONTENTION_FST_H
#include "interval_contention.h"
#include "dynamic_micro_op_fastsim.h"
#include "log.h"
#include <algorithm>
#include <array>

// Native interval old-window resource-pressure approximation using the same
// aggregate SPR capacities and lane occupancy descriptors as the ROB adapter.
// This is not the FastSim timing engine or a Nehalem port reinterpret_cast.
class IntervalContentionFst : public IntervalContention {
   std::array<uint64_t, fastsim_platform::POOL_COUNT> occupancy{};
   uint64_t loads = 0, stores = 0;
public:
   void clearFunctionalUnitStats() override { occupancy.fill(0); loads = stores = 0; }
   void addFunctionalUnitStats(const DynamicMicroOp *uop) override {
      const auto *info = uop->getCoreSpecificInfo<DynamicMicroOpFastSim>();
      occupancy[info->fu_pool] += std::max(1u, info->occupancy);
      if (info->is_memory_uop) { if (info->mem_write) ++stores; else ++loads; }
   }
   void removeFunctionalUnitStats(const DynamicMicroOp *uop) override {
      const auto *info = uop->getCoreSpecificInfo<DynamicMicroOpFastSim>();
      LOG_ASSERT_ERROR(occupancy[info->fu_pool] >= std::max(1u, info->occupancy), "FST interval FU occupancy underflow");
      occupancy[info->fu_pool] -= std::max(1u, info->occupancy);
      if (info->is_memory_uop) { if (info->mem_write) --stores; else --loads; }
   }
   uint64_t getEffectiveCriticalPathLength(uint64_t cp, bool) override {
      for (unsigned p = 0; p < fastsim_platform::POOL_COUNT; ++p) {
         const uint64_t lanes = fastsim_platform::kPoolUnits[p];
         cp = std::max(cp, (occupancy[p] + lanes - 1) / lanes);
      }
      cp = std::max(cp, (loads + fastsim_platform::kLoadPorts - 1) / fastsim_platform::kLoadPorts);
      cp = std::max(cp, (stores + fastsim_platform::kStorePorts - 1) / fastsim_platform::kStorePorts);
      return cp;
   }
};
#endif
''')
    core = base + 'core_model/core_model_fastsim.cc'
    edit(core, '#include "interval_contention_nehalem.h"', '#include "interval_contention_fst.h"')
    edit(core, '''   // The ROB performance model is used for FST runs; this is provided only to
   // satisfy the factory interface.
   return new IntervalContentionNehalem(core, this);''',
         '   return new IntervalContentionFst();')
    files = {r['file'] for r in patches} | {resource}
    (a.output / 'interval-adapter-receipt.json').write_text(json.dumps(dict(
        source=str(a.source.resolve()), patches=patches,
        final_files={f: hashlib.sha256((a.output/f).read_bytes()).hexdigest() for f in sorted(files)},
        source_reader_sha256=hashlib.sha256((a.source/'fst-adapter-include/fst_v7.hpp').read_bytes()).hexdigest(),
    ), indent=2) + '\n')


if __name__ == '__main__':
    main()
