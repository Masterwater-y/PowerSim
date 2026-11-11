#include "fastsim/simulator.hpp"
#include <stdexcept>
#include <vector>

namespace {
using namespace fastsim;

// Each row is one non-split functional store UOP/request. The second address
// conflicts with the first in the default L1 set, so both the ordinary and
// legacy buffered-prepare paths are exercised when two records are provided.
class SimpleStoreTrace final : public TraceSource {
 public:
  explicit SimpleStoreTrace(unsigned count):count_(count) {}
  bool next(TraceRecord& record) override {
    if(index_==count_)return false;
    record.pc=0x1000+4*index_;record.address=0x8000+0x10000*index_;
    record.size=8;record.flags=kRetires|kStore|kPhysicalAddress;
    record.op_class=56;++index_;return true;
  }
  std::string description() const override{return "legacy-store-perf-functional";}
 private:
  unsigned count_,index_=0;
};
std::uint64_t modeled_store_events(TsoStoreMode mode,bool lifecycle,unsigned n) {
  SimulatorConfig c;c.cores=1;c.cha_count=1;c.core_model="interval_weave";
  c.interval_scheduler="time_epoch";c.chunk_instructions=16;c.interval_target_uops=16;
  c.response_queue_feedback=true;c.response_sparse_scoreboard=true;c.tso_store=mode;
  if(lifecycle) {
    c.buffer_tso_prepare_lifecycle=true;c.store_commit_to_iew_delay=1;
    c.l1d.store_hit_latency=1;c.l2.store_hit_latency=12;
  }
  std::vector<std::unique_ptr<TraceSource>> traces;
  traces.push_back(std::make_unique<SimpleStoreTrace>(n));
  Simulator simulator(c,std::move(traces));
  return simulator.run().cores.at(0).perf_retired_stores;
}
void require(bool value,const char* message) {
  if(!value)throw std::runtime_error(message);
}
}

void test_spr_legacy_store_perf() {
  for(unsigned n: {1u,2u}) {
    require(modeled_store_events(TsoStoreMode::kSerial,false,n)==n,
            "serial path must count each ordinary store request once");
    require(modeled_store_events(TsoStoreMode::kBuffer,true,n)==n,
            "prepare-lifecycle path must count each ordinary store request once");
    require(modeled_store_events(TsoStoreMode::kBuffer,false,n)==n,
            "legacy buffer path must count each ordinary store request once");
  }
}
