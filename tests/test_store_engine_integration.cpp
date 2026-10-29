#include "fastsim/config.hpp"
#include "fastsim/simulator.hpp"
#include "fastsim/trace.hpp"
#include <stdexcept>
#include <iostream>
using namespace fastsim;
namespace {
class Source final:public TraceSource {
 std::vector<TraceRecord> records_;std::size_t next_=0;
public:
 explicit Source(std::vector<TraceRecord> records):records_(std::move(records)){}
 bool next(TraceRecord& r) override {if(next_==records_.size())return false;r=records_[next_++];return true;}
 std::string description()const override{return "store-engine-integration";}
};
void require(bool value,const char* message){if(!value)throw std::runtime_error(message);}
SimulationStats run(bool certificate,std::uint32_t sq=32,bool preview=false,std::uint32_t cores=1,bool external_load=false) {
 SimulatorConfig c;c.cores=cores;c.domain_min_events=1;c.core_model="interval_weave";c.interval_scheduler="time_epoch";
 c.response_queue_feedback=true;c.response_sparse_scoreboard=true;c.needs_tso=true;
 c.tso_store=TsoStoreMode::kBuffer;c.tso_store_buffer_size=sq;
 c.interval_private_preview=preview;c.store_request_engine=true;c.buffer_tso_residence_certificate=certificate;
 c.store_commit_to_iew_delay=1;c.l1d.store_hit_latency=1;c.l2.store_hit_latency=5;
 c.chunk_instructions=128;c.interval_target_uops=128;c.interval_max_cycles=4096;c.cha_count=1;c.dram.channels=1;
 c.cpi_attribution=true;c.response_frontier_audit_stride_uops=1;c.validate();
 std::vector<TraceRecord> records;
 for(unsigned i=0;i<24;++i) {TraceRecord r;r.flags=kRetires|kStore|kPhysicalAddress;
  r.address=0x10000+64*i;r.size=8;records.push_back(r);}
 TraceRecord fence;fence.flags=kRetires|kSerialize;records.push_back(fence);
 std::vector<std::unique_ptr<TraceSource>> traces;
 for(std::uint32_t core=0;core<cores;++core) {
  if(external_load && core==1) {
   std::vector<TraceRecord> other;
   for(unsigned i=0;i<64;++i){TraceRecord r;r.flags=kRetires;other.push_back(r);}
   TraceRecord load;load.flags=kRetires|kLoad|kPhysicalAddress;load.address=0x10000;load.size=8;
   other.push_back(load);other.push_back(fence);traces.push_back(std::make_unique<Source>(other));
  } else traces.push_back(std::make_unique<Source>(records));
 }
 Simulator sim(c,std::move(traces));return sim.run();
}
}
void test_store_engine_integration() {
 const auto a=run(false),b=run(true);const auto x=a.total_core(),y=b.total_core();
 require(x.retired_instructions==25 && x.memory_accesses==24,"new bridge conserves architecture population");
 require(x.buffer_tso_head_demands==24 && x.buffer_tso_prepares>0,"real backend receives mandatory and parallel prepare services");
 require(x.cycles==y.cycles && x.l1d.accesses==y.l1d.accesses && x.buffer_tso_prepares==y.buffer_tso_prepares,
         "certificate only changes execution strategy, never prepare eligibility");
 std::uint64_t previous=0;unsigned stores=0;
 for(const auto& s:a.response_frontier_audit[0])if(s.regular_store) {
  require(s.store_send_cycle>=s.actual_retire_cycle+2,"canWB precedes actual CPU store enqueue");
  require(!stores || s.store_send_cycle>previous,"mandatory requests retain FIFO callback edge");
  require(s.store_sq_release_cycle>=s.store_send_cycle,"SQ releases on actual mandatory response");
  previous=s.store_sq_release_cycle;++stores;
 }
 require(stores==24 && a.total_o3().sq_max_occupancy<=32,"all stores have actual response feedback");
 const auto preview=run(false,32,true).total_core();
 require(preview.cycles==x.cycles && preview.l1d.accesses==x.l1d.accesses,
         "existing private preview cannot warm or double-count the new backend replay");
 const auto shared=run(false,32,false,2).total_core();
 require(shared.retired_instructions==50 && shared.memory_accesses==48 && shared.perf_retired_stores==48,
         "same-line coherence and protocol retry cannot duplicate architectural stores");
 const auto waiting=run(false,32,false,2,true);unsigned loads=0;
 for(const auto& sample:waiting.response_frontier_audit[1])if(sample.has_load) {
  ++loads;
  for(const auto& event:sample.memory_events)
   require(event.response_cycle>=event.shared_response_cycle,
           "a load held behind store permission must not return before actual backend response");
 }
 require(loads==1,"backend wait regression observes the held load");
 const auto serial=run(false,1);
 require(serial.total_core().buffer_tso_prepares==0 && serial.total_core().buffer_tso_head_demands==24,
         "one SQ entry permits no optional follower");
}
#ifdef FASTSIM_STORE_ENGINE_INTEGRATION_STANDALONE
int main(){try{test_store_engine_integration();std::cout<<"store engine integration passed\n";}
 catch(const std::exception& e){std::cerr<<e.what()<<'\n';return 1;}}
#endif
