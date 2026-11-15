#include "fastsim/parent_service.hpp"
#include "fastsim/legacy_prepare_timing.hpp"
#include "fastsim/simulator.hpp"
#include "fastsim/trace.hpp"
#include <iostream>
#include <memory>
#include <stdexcept>
#include <vector>

using namespace fastsim;
static void require(bool value,const char* message) {
  if (!value) throw std::runtime_error(message);
}

class ReadSource final : public TraceSource {
 public:
  ReadSource(std::vector<TraceRecord> records,bool certified)
    : records_(std::move(records)), certified_(certified) {}
  bool next(TraceRecord& r) override {
    if (at_==records_.size()) return false;
    r=records_[at_++]; return true;
  }
  std::string description() const override { return "parent-service-functional-fixture"; }
  std::optional<bool> current_ordinary_load_eligible() const override { return certified_; }
 private:
  std::vector<TraceRecord> records_;
  std::size_t at_=0;
  bool certified_;
};

static SimulationStats run(const std::string& mode,bool certified=true,
                           bool boundary=false,bool buffer=true,bool reversed_source=false,
                           unsigned conflict=0) {
  SimulatorConfig c;
  c.cores=conflict==2 ? 2 : 1;
  c.core_model="interval_weave"; c.interval_scheduler="time_epoch";
  c.interval_max_cycles=1024; c.chunk_instructions=64; c.interval_target_uops=64;
  c.lookahead_chunks=2; c.response_queue_feedback=true; c.response_sparse_scoreboard=true;
  c.response_block_summary=true; c.response_memory_descriptor=true;
  c.response_frontier_audit_stride_uops=1; c.cpi_attribution=true;
  c.ruby_sequencer_max_outstanding=64;
  c.l1d.hit_latency=2; c.l1d.size_bytes=4096; c.l2.size_bytes=16384;
  c.llc.size_bytes=65536; c.cha_count=1; c.dram.channels=1; c.dram.banks_per_channel=1;
  c.llc_fill_response_latency=boundary?2048:100;
  c.unified_parent_service=mode;
  if (mode=="v2-observe" || mode=="v2-wait") {
    c.store_commit_to_iew_delay=1;
    c.legacy_prepare_timing=true;
  }
  if (buffer) c.tso_store=TsoStoreMode::kBuffer;
  // Small calendars force actual resource-reference use in the runtime.
  c.l1d_mshrs=1; c.l2_mshrs=1;
  c.validate();
  const std::size_t follower=boundary?1200:4;
  std::vector<TraceRecord> records(follower+2);
  for (std::size_t i=0;i<records.size();++i) {
    records[i].pc=0x1000+i*4;
    records[i].flags=kRetires|kPhysicalAddress;
    if (boundary && i>1 && i<follower) records[i].producer_dists[0]=1;
  }
  for (std::size_t i : {std::size_t(0),follower}) {
    records[i].flags|=kLoad; records[i].address=0x8000; records[i].size=8;
  }
  if (reversed_source) {
    records[0].address=0x9000;
    records[1].flags|=kLoad; records[1].address=0x8000; records[1].size=8;
    records[1].producer_dists[0]=1;
  }
  if (conflict==1) {
    records[2].flags|=kStore;
    records[2].address=0x8000;
    records[2].size=8;
  }
  records[follower+1].producer_dists[0]=1;
  std::vector<std::unique_ptr<TraceSource>> sources;
  sources.push_back(std::make_unique<ReadSource>(std::move(records),certified));
  if (conflict==2) {
    TraceRecord write{};
    write.pc=0x2000; write.address=0x8000; write.size=8;
    write.flags=kRetires|kPhysicalAddress|kStore;
    sources.push_back(std::make_unique<ReadSource>(
        std::vector<TraceRecord>{write},certified));
  }
  return Simulator(c,std::move(sources)).run();
}

static const ResponseFrontierAuditSample& sample(const SimulationStats& s,std::uint64_t seq) {
  for (const auto& r:s.response_frontier_audit.at(0)) if (r.sequence==seq) return r;
  throw std::runtime_error("missing parent-service runtime audit sample");
}

static void pool_tests() {
  ParentServicePool p;
  p.configure(4); p.begin_checkpoint(1,1);
  auto a=p.create(7,0,10,100,ParentServicePool::Time::kFinalLocal,true);
  p.attach_lease(a,0,10,100); p.attach_lease(a,1,12,120);
  require(p.decide(7,9).outcome==ParentServicePool::Outcome::kFuture,"future source attached");
  require(p.decide(7,20).id==a,"follower lost its stable parent");
  require(p.decide(7,100).outcome==ParentServicePool::Outcome::kCompleted,
          "equal-time completed fill remained a follower");
  require(p.get(a)->data_ready==100 && p.first_release(1)==120,"data and releases conflated");
  auto rejected=p;
  rejected.invalidate(7);
  require(p.decide(7,20).id==a,"rejected transaction changed original");
  p.expire_before(100);
  require(p.index_size()==0 && p.size()==1,"expired data discarded a live lease");
  bool reversed=false;
  try { p.expire_before(99); } catch (const std::logic_error&) { reversed=true; }
  require(reversed,"OoO query was allowed to reverse reclamation watermark");
  p.begin_checkpoint(1,1);
  require(p.size()==0,"unpinned completed service leaked");
  auto b=p.create(7,1,100,140,ParentServicePool::Time::kUnknown,true);
  require(b!=a && p.get(a)==nullptr,"stale generation alias");
  require(p.decide(7,110).outcome==ParentServicePool::Outcome::kUncertified,"unknown time invented");
  p.invalidate(7);
  for (std::uint64_t i=0;i<10000;++i) {
    auto id=p.create(8,i,100,100000,ParentServicePool::Time::kFinalLocal,true);
    require(id!=0 && p.size()<=4 && p.expiry_size()<=4,"ROI-dependent pool/index growth");
  }
  ParentServicePool saturated; saturated.configure(1);
  saturated.create(1,1,0,10,ParentServicePool::Time::kFinalLocal,true);
  require(saturated.create(2,2,0,10,ParentServicePool::Time::kFinalLocal,true)==0,"capacity silently exceeded");
}

static void snapshot_tests() {
  ServiceSnapshotHeap<std::uint64_t> heap;
  std::vector<std::uint64_t> reference;
  const auto less=[](std::uint64_t a,std::uint64_t b) { return a/8 < b/8; };
  const auto greater=[&](std::uint64_t a,std::uint64_t b) { return less(b,a); };
  std::uint64_t random=17;
  for (unsigned i=0;i<20000;++i) {
    random=random*6364136223846793005ULL+1;
    if (reference.empty() || (random&3)!=0) {
      const auto value=(random>>16)%2048;
      heap.push(value,less);
      reference.push_back(value);
      std::push_heap(reference.begin(),reference.end(),greater);
    } else {
      require(heap.top()==reference.front(),"paged heap changed equal-key ordering");
      std::pop_heap(reference.begin(),reference.end(),greater);
      require(heap.pop(less)==reference.back(),"paged pop differs from legacy heap");
      reference.pop_back();
    }
  }
  auto rejected=heap;
  const auto top=heap.top(), count=heap.size();
  while (!rejected.empty()) rejected.pop(less);
  require(heap.top()==top && heap.size()==count,"rejected heap snapshot changed accepted root");
  ServiceSnapshotLineIndex index;
  index.configure(128);
  index.put(0,1);
  index.put(std::numeric_limits<std::uint64_t>::max(),2);
  auto trial=index;
  trial.erase(0); trial.put(13,3);
  require(index.get(0)==1 && index.get(13)==0 &&
      index.get(std::numeric_limits<std::uint64_t>::max())==2,
      "line index snapshot leaked a tombstone or insertion");
  for (std::uint64_t i=0;i<20000;++i) {
    index.put(i+17,3); index.erase(i+17);
    require(index.size()==2 && index.get(0)==1,"bounded line index lost population during compaction");
  }
  require(index.rebuilds()>0,"line index churn did not compact tombstones");
  ParentServicePool p; p.configure(4096);
  for (std::uint64_t i=0;i<512;++i)
    p.create(i,i,0,100000,ParentServicePool::Time::kFinalLocal,true);
  auto read_only=p;
  const auto before=read_only.index_cost();
  for (std::uint64_t i=0;i<512;++i) read_only.decide(i,1);
  require(read_only.index_cost().bytes==before.bytes &&
      read_only.copied_record_blocks()==p.copied_record_blocks(),
      "read-only snapshot copied service or index pages");
  read_only.invalidate(17);
  const auto copied=read_only.index_cost().leaves-before.leaves;
  require(copied<=3 && p.indexed_id(17)!=0 && read_only.indexed_id(17)==0,
      "single-line change copied the whole index or altered accepted state");
  using P=ParentServicePool;
  P::TimeEdge zero{0,1,2,P::Evidence::kEstimated};
  require(zero.known() && !P::TimeEdge{}.known() &&
      zero.comparable({5,1,2,P::Evidence::kSelected}) &&
      !zero.comparable({0,1,3,P::Evidence::kEstimated}),
      "time evidence conflated zero with unknown or ignored clock epoch");
}

static void source_ledger_tests() {
  using P=ParentServicePool;
  P pool; pool.configure(32); pool.configure_owned(1,1);
  LegacyPrepareTiming timing(32,32);
  UnifiedParentCounters counters;
  auto attempt=[&](std::uint64_t line,P::Facts facts,bool certified,bool target,
                   P::SourceReject reason=P::SourceReject::kAdmissionMismatch) {
    return timing.store_unified({line,0,line},10,20,100,90,2,
        facts,certified,true,target,pool,counters,reason);
  };
  const P::Facts fixed{P::State::kInvalid,P::Action::kFetchExclusive,true,70,true};
  attempt(1,fixed,false,true);
  attempt(2,fixed,false,true,P::SourceReject::kCarried);
  attempt(3,fixed,true,false);
  attempt(4,{P::State::kUnknown,P::Action::kUnknown,false},true,true);
  attempt(5,{P::State::kInvalid,P::Action::kFetchExclusive,true},true,true);
  attempt(6,{P::State::kInvalid,P::Action::kFetchExclusive,true,5,true},true,true);
  attempt(7,{P::State::kInvalid,P::Action::kFetchExclusive,true,110,true},true,true);
  attempt(8,fixed,true,true);
  attempt(9,fixed,true,true);
  const auto& getx=counters.source_ledger[1][static_cast<std::size_t>(P::Action::kFetchExclusive)];
  for (auto reason : {P::SourceDecision::kAdmissionMismatch,P::SourceDecision::kCarried,
       P::SourceDecision::kTargetUnknown,P::SourceDecision::kLowerMissing,
       P::SourceDecision::kBeforeAdmission,P::SourceDecision::kAfterLegacyDone,
       P::SourceDecision::kSplit,P::SourceDecision::kResourceCapacity})
    require(getx[static_cast<std::size_t>(reason)]==1,"GETX first-rejection ledger is not mutually exclusive");
  std::uint64_t total=0;
  for (const auto& role:counters.source_ledger)
    for (const auto& action:role)
      for (auto count:action) total+=count;
  require(total==counters.services && total==9,"source ledger dropped or duplicated a candidate");
  UnifiedParentCounters aggregate; aggregate+=counters; aggregate+=counters;
  require(aggregate.source_ledger[1][static_cast<std::size_t>(P::Action::kFetchExclusive)]
      [static_cast<std::size_t>(P::SourceDecision::kCarried)]==2,
      "per-core reduction dropped source reasons");
  require(pool.own_decision(false,9)==P::SourceDecision::kResourceNonmonotone,
      "resource monotonicity was folded into capacity");
}

static void v2_tests() {
  using P=ParentServicePool;
  P pool; pool.configure(32); pool.begin_checkpoint(1,1);
  UnifiedParentCounters counters;
  LegacyPrepareTiming timing(1,16);
  auto fetch=timing.store_unified({1,0,7},10,20,100,90,2,
      {P::State::kInvalid,P::Action::kFetchExclusive,true},
      true,true,true,pool,counters);
  const auto id=pool.indexed_id(7);
  require(id && fetch.role==LegacyPrepareRole::Takeover && fetch.response==100,
          "I prepare did not retain its GETX on takeover");
  require(pool.get(id)->origin==P::Role::kPrepare &&
          pool.get(id)->consumer==P::Role::kStore &&
          pool.get(id)->has_data_fill,"takeover changed the immutable service origin");
  pool.attach_lease(id,0,10,120);
  require(pool.read_gate(id,25)==100 && pool.first_release(0)==120,
          "data gate and lease release were conflated");
  auto rejected=pool;
  rejected.invalidate(7);
  require(pool.indexed_id(7)==id && pool.get(id)->pins==1,
          "abandoned v2 trial changed original references");
  auto upgrade=timing.store_unified({2,0,8},30,40,70,40,2,
      {P::State::kShared,P::Action::kUpgrade,true},
      true,true,true,pool,counters);
  const auto up=pool.indexed_id(8);
  require(up && upgrade.role==LegacyPrepareRole::Takeover && counters.credit_releases==1,
          "takeover did not independently return prepare credit");
  require(pool.get(up)->data_ready==30 && !pool.get(up)->has_data_fill &&
          pool.read_gate(up,35)==70 &&
          pool.decide(8,35).outcome==P::Outcome::kFollower,
          "S upgrade lost its controller gate or fabricated a data fill");
  auto exclusive=timing.store_unified({3,0,9},50,60,999,949,2,
      {P::State::kExclusive,P::Action::kNone,false},
      true,true,true,pool,counters);
  require(exclusive.response==62 && pool.indexed_id(9)==P::none,
          "E confirmation fabricated a transaction or delayed publish");
  require(pool.indexed_id(7)==id && pool.first_release(0)==120,
          "credit completion released the GETX lease");
  // Observe migrates credit ownership while reproducing legacy timing,
  // including the legacy credit behavior for E/M.
  LegacyPrepareTiming legacy(1,16), observed(1,16);
  P observed_pool; observed_pool.configure(32);
  UnifiedParentCounters ignored;
  for (std::uint64_t i=0;i<8;++i) {
    const auto a=legacy.store({i,0,20+i},10+i*3,20+i*3,100+i,90,2);
    const auto b=observed.store_unified({i,0,20+i},10+i*3,20+i*3,100+i,90,2,
        {P::State::kExclusive,P::Action::kNone,false},
        false,false,false,observed_pool,ignored);
    require(a.role==b.role && a.response==b.response && a.permission==b.permission,
            "v2 observe changed legacy admission or credit behavior");
  }
  // Uncertified selected source remains unavailable to read consumers.
  LegacyPrepareTiming unknown;
  const auto u=unknown.store_unified({99,0,30},80,90,180,100,2,
      {P::State::kInvalid,P::Action::kFetchExclusive,true},
      false,true,true,pool,counters);
  (void)u;
  require(pool.decide(30,100).outcome==P::Outcome::kUncertified &&
          pool.read_gate(pool.indexed_id(30),100)==100,
          "a delayed, uncertified source was used as a leader");
}

static void boundary_resource_tests() {
  using P=ParentServicePool;
  P p; p.configure(16); p.configure_owned(2,1);
  const auto id=p.create(1,1,10,100,P::Time::kSelectedFixed,false);
  p.set_service(id,P::Action::kFetchExclusive,P::Role::kPrepare,80,90,100,
      P::Time::kSelectedFixed);
  p.pin(id); p.index_service(id,100);
  p.own(id,true,10);
  // A second indexing call must not reclaim the record through its own ID.
  p.index_service(id,100);
  require(!p.can_own(true,20) && p.can_own(false,20),
      "TBE and fill capacity were conflated");
  bool duplicate=false;
  try { p.own(id,true,10); } catch(const std::logic_error&) { duplicate=true; }
  require(duplicate && p.owned_size(P::Resource::kFill)==1,
      "takeover duplicated the fill allocation");
  p.takeover(id,152,7,150);
  require(p.read_gate(id,110,6)==110 && p.read_gate(id,110,8)==150,
      "unrelated future store callback blocked an older read");
  require(p.read_gate(id,151,8)==151 && p.get(id)->request_callback==152,
      "read waited for callback after write data became available");
  auto rejected=p;
  rejected.expire_before(100);
  require(p.owned_size(P::Resource::kFill)==1 &&
      p.get(id)->origin==P::Role::kPrepare,
      "rejected lease trial modified the accepted transaction");
  p.expire_before(80);
  require(p.owned_size(P::Resource::kFill)==0 &&
      p.owned_size(P::Resource::kTbe)==1 && p.get(id)!=nullptr,
      "data arrival prematurely released controller TBE");
  p.expire_before(100);
  require(p.owned_size(P::Resource::kTbe)==0 && p.get(id)!=nullptr,
      "service leases and outstanding write consumer were conflated");
  p.unpin(id); p.expire_before(150);
  require(p.get(id)==nullptr,"completed typed service leaked");
  LegacyPrepareTiming timing(1,16);
  UnifiedParentCounters counters;
  P split; split.configure(16); split.configure_owned(2,1);
  P::Facts facts{P::State::kInvalid,P::Action::kFetchExclusive,true,70,true};
  const auto a=timing.store_unified({1,0,3},10,90,100,90,2,
      facts,true,true,true,split,counters);
  const auto parent=split.indexed_id(3);
  require(a.response==100 && split.get(parent)->data_ready==70 &&
      split.get(parent)->permission_ready==70 &&
      split.owned_release(parent,1)==70 && counters.split_services==1,
      "lower return was not separated from request callback");
  require(split.read_gate(parent,95,2)==95 && split.get(parent)->request_callback==100,
      "split store callback remained a read data gate");
  const auto b=timing.store_unified({2,0,4},100,110,140,40,2,
      {P::State::kShared,P::Action::kUpgrade,true,130,true},
      true,true,true,split,counters);
  (void)b;
  require(counters.fill_allocations==1 && counters.tbe_allocations==2 &&
      !split.get(split.indexed_id(4))->has_data_fill,
      "resident upgrade allocated a new data fill");
}

int main() {
  try {
    pool_tests();
    snapshot_tests();
    source_ledger_tests();
    v2_tests();
    boundary_resource_tests();
    const auto off=run("off"), observe=run("observe"), wait=run("wait");
    require(off.total_core().cycles==observe.total_core().cycles,"S0 changed cycles");
    require(off.total_core().l1d.misses==observe.total_core().l1d.misses,"S0 changed cache");
    require(sample(off,4).selected_memory_response_cycle <
            sample(off,0).selected_memory_response_cycle,"fixture has no premature hit");
    require(sample(wait,4).selected_memory_response_cycle >=
            sample(wait,0).selected_memory_response_cycle,"S1 returned before data");
    require(sample(wait,5).actual_issue_cycle >= sample(wait,4).actual_completion_cycle,
            "S1 did not propagate into consumer");
    require(wait.unified_parent[0].waits>0 && wait.pending_fill[0].requests==0,
            "S1 duplicated old pending fill owner");
    require(wait.total_core().retired_uops==off.total_core().retired_uops &&
            wait.total_core().l1d.misses==off.total_core().l1d.misses &&
            wait.sequencer_functional_replay_passes==0,"S1 changed functional population");
    auto missing=run("wait",false);
    require(missing.total_core().cycles==off.total_core().cycles &&
            missing.unified_parent[0].waits==0 &&
            missing.unified_parent[0].special_fallbacks>0,"missing metadata was guessed");
    auto carried=run("wait",true,true);
    require(carried.interval_steps>1 && carried.unified_parent[0].carried>0 &&
            sample(carried,1200).selected_memory_response_cycle>=
            sample(carried,0).selected_memory_response_cycle,"cross-window parent lost");
    auto reverse_off=run("off",true,false,true,true);
    auto reverse_wait=run("wait",true,false,true,true);
    require(reverse_wait.unified_parent[0].source_fallbacks>0 ||
            reverse_wait.unified_parent[0].future_fallbacks>0,
            "inverted source was not reported");
    require(sample(reverse_wait,4).selected_memory_response_cycle==
            sample(reverse_off,4).selected_memory_response_cycle,
            "uncertified source was attached by a max-time patch");
    for (unsigned conflict : {1u,2u}) {
      const auto hazard=run("wait",true,false,true,false,conflict);
      require(hazard.unified_parent[0].certified_services==0 &&
              hazard.unified_parent[0].waits==0,
              "same-line local or cross-core write retained read certificate");
    }
    // Configuration rejects a parallel authoritative pending-fill table.
    auto c=SimulatorConfig{};
    c.unified_parent_service="wait"; c.response_pending_fill=true;
    bool rejected=false;
    try { c.validate(); } catch (const std::invalid_argument&) { rejected=true; }
    require(rejected,"competing service owners accepted");
    const auto v2_observe=run("v2-observe"), v2_wait=run("v2-wait");
    require(v2_observe.total_core().cycles==off.total_core().cycles,
            "v2 observe changed the read-only baseline");
    require(sample(v2_wait,4).selected_memory_response_cycle >
            sample(v2_wait,0).selected_memory_response_cycle,
            "v2 local follower skipped the post-gate local path");
    require(v2_wait.total_core().retired_uops==off.total_core().retired_uops,
            "v2 changed retired population");
    std::cout<<"parent service pool and real two-stage runtime tests passed\n";
    return 0;
  } catch(const std::exception& e) { std::cerr<<e.what()<<'\n'; return 1; }
}
