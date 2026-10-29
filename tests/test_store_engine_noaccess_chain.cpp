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
 std::optional<fst::MemoryRequestRole> current_memory_request_role()const override {return (next_-1)%4==1 ? fst::MemoryRequestRole::NoAccess : fst::MemoryRequestRole::Data;}
 std::string description()const override{return "store-engine-integration";}
};
void require(bool value,const char* message){if(!value)throw std::runtime_error(message);}
SimulationStats run(bool certificate,std::uint32_t sq=32,bool sparse=true,bool preview=false,bool special=false,unsigned groups=256) {
 SimulatorConfig c;c.cores=1;c.core_model="interval_weave";c.interval_scheduler="time_epoch";
 c.response_queue_feedback=true;c.response_sparse_scoreboard=sparse;c.interval_private_preview=preview;c.domain_min_events=1;c.needs_tso=true;
 c.tso_store=TsoStoreMode::kBuffer;c.tso_store_buffer_size=sq;
 c.store_request_engine=true;c.buffer_tso_residence_certificate=certificate;
 c.store_commit_to_iew_delay=1;c.l1d.store_hit_latency=1;c.l2.store_hit_latency=5;
 c.chunk_instructions=2048;c.interval_target_uops=2048;c.interval_max_cycles=100000;c.cha_count=1;c.dram.channels=1;
 c.cpi_attribution=true;c.response_frontier_audit_stride_uops=1;c.validate();
 std::vector<TraceRecord> records;
 for(unsigned group=0;group<(special?1u:groups);++group) {for(unsigned i=0;i<4;++i){TraceRecord r;r.flags=kRetires|kStore|kPhysicalAddress;r.op_class=57;
 r.address=0x8000+group*128+(i==0?0:(i==1?72:64));r.size=(special && i==0)?128:8;records.push_back(r);}}
 TraceRecord fence;fence.flags=kRetires|kSerialize;records.push_back(fence);
 std::vector<std::unique_ptr<TraceSource>> traces;traces.push_back(std::make_unique<Source>(records));
 Simulator sim(c,std::move(traces));return sim.run();
}
}
void test_store_engine_noaccess_chain() {
 const auto special=run(false,114,true,false,true);std::uint64_t local=0,prior=0;unsigned following=0;
 for(const auto& s:special.response_frontier_audit[0]) {
  if(s.sequence==0)prior=s.store_sq_release_cycle;
  if(s.sequence==1)local=s.store_sq_release_cycle;
  if(s.sequence>=2 && s.regular_store) {
   require(s.store_send_cycle>=local,"ordinary store cannot bypass the preceding local SQ completion");++following;
  }
 }
 require(local>=prior+1 && following==2,"split-to-local FIFO retains the response-to-next-tick edge");

 auto a=run(false,114);const auto x=a.total_core();
 const auto tight=run(false,2,true,false,false,8).total_core();
 require(tight.retired_uops==33 && tight.buffer_tso_head_demands==24,
         "same-cycle local SQ release must admit a waiting retired owner before advancing time");
 require(x.retired_uops==1025 && x.local_no_access_uops==256 && x.buffer_tso_head_demands==768,
         "long NO_ACCESS chain completes in one request lifecycle without extra memory requests");
}
#ifdef FASTSIM_STORE_ENGINE_NOACCESS_STANDALONE
int main(){try{test_store_engine_noaccess_chain();std::cout<<"store engine NO_ACCESS chain passed\n";}
 catch(const std::exception& e){std::cerr<<e.what()<<'\n';return 1;}}
#endif
