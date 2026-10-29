#include "fastsim/trace.hpp"
#include "fastsim/simulator.hpp"
#include <array>
#include <algorithm>
#include <filesystem>
#include <fstream>
#include <stdexcept>
#include <unistd.h>

namespace {
void require_role(bool v, const char* message) {
    if (!v) throw std::runtime_error(message);
}
// Independent wire fixture: changing the producer and reader together must not
// silently change the portable ABI. All integers are little endian.
void put(std::ostream& out, std::uint64_t n, unsigned bytes) {
    for (unsigned i=0;i<bytes;++i) out.put(static_cast<char>(n >> (i*8)));
}
void role_fixture(const std::string& path, bool prefetch=false) {
    fastsim::TraceRecord r;
    r.pc=0x1000; r.address=0x2000; r.size=8; r.op_class=57;
    r.flags=fastsim::kRetires|(prefetch ? fastsim::kLoad : fastsim::kStore)|fastsim::kPhysicalAddress;
    { fastsim::BinaryTraceWriter w(path, 0); w.append(r); w.close(); }
    { std::fstream f(path,std::ios::binary|std::ios::in|std::ios::out);
      f.seekp(32); put(f,64,8); }
    std::uint64_t hash=14695981039346656037ull;
    std::uint32_t row_hash=2166136261u;
    const auto* bytes=reinterpret_cast<const unsigned char*>(&r);
    for (unsigned i=0;i<64;++i) {
        const auto b=bytes[i];
        hash=(hash^b)*1099511628211ull; row_hash=(row_hash^b)*16777619u;
    }
    std::ofstream f(path+".mreq",std::ios::binary);
    f.write("FSTMREQ1",8); put(f,prefetch ? 2 : 1,4); put(f,56,4);
    put(f,0,4); put(f,1,4); put(f,1,8); put(f,1,8); put(f,hash,8);
    put(f,24,4); put(f,0,4);
    put(f,0,8); put(f,row_hash,4); put(f,3,4); put(f,prefetch ? 2 : 1,1); put(f,0,7);
}
}
void test_memory_request_roles() {
    const auto dir=std::filesystem::path("tmp")/("mreq-test-"+std::to_string(getpid()));
    std::filesystem::create_directories(dir);
    // Wire value 2 denotes a read-only software prefetch, independent of
    // its ISA spelling. It is not an architectural load result.
    const auto sw=(dir/"prefetch.fst").string();
    fastsim::TraceRecord pf;
    pf.pc=0x8000; pf.address=0x9000; pf.size=8; pf.op_class=56;
    pf.flags=fastsim::kRetires|fastsim::kLoad|fastsim::kPhysicalAddress;
    { fastsim::BinaryTraceWriter w(sw,0); w.enable_memory_request_roles();
      w.append(pf,nullptr,{},static_cast<fastsim::fst::MemoryRequestRole>(2),7); w.close(); }
    { fastsim::BinaryTraceSource d(sw); fastsim::TraceRecord decoded;
      require_role(d.next(decoded) && d.current_memory_request_role()==
          static_cast<fastsim::fst::MemoryRequestRole>(2) &&
          d.current_memory_request_micro_pc()==7 && !d.next(decoded),
          "software-prefetch functional role must survive decoding"); }
    // Independent v2 bytes ensure producer and reader cannot silently change
    // role values together. Version 1 may never carry the new role.
    const auto independent=(dir/"independent-sw.fst").string();
    role_fixture(independent,true);
    { fastsim::BinaryTraceSource d(independent); fastsim::TraceRecord record;
      require_role(d.contains_software_prefetch() && d.next(record) &&
          d.current_memory_request_role()==static_cast<fastsim::fst::MemoryRequestRole>(2) &&
          !d.next(record), "independent v2 role2 fixture must decode"); }
    { std::fstream f(independent+".mreq",std::ios::binary|std::ios::in|std::ios::out);
      f.seekp(8); put(f,1,4); }
    { bool rejected=false;
      try { fastsim::BinaryTraceSource d(independent); }
      catch (const std::runtime_error&) { rejected=true; }
      require_role(rejected,"v1 companion must reject a role2 row"); }
    for (unsigned invalid : {0u,1u,2u}) {
      auto bad=pf;
      if (invalid==0) bad.size=0;
      if (invalid==1) bad.flags|=fastsim::kStore;
      if (invalid==2) bad.flags|=fastsim::kAtomic;
      fastsim::BinaryTraceWriter w((dir/("invalid-sw-"+std::to_string(invalid)+".fst")).string(),0);
      w.enable_memory_request_roles(); bool rejected=false;
      try { w.append(bad,nullptr,{},static_cast<fastsim::fst::MemoryRequestRole>(2)); }
      catch(const std::invalid_argument&) { rejected=true; }
      require_role(rejected,"software-prefetch role must be a nonempty read-only hint");
    }
    const auto p=(dir/"local.fst").string(); role_fixture(p);
    fastsim::BinaryTraceSource source(p);
    fastsim::TraceRecord r;
    require_role(source.next(r) && r.is_write(), "local requests must retain their store instruction");
    require_role(source.memory_request_roles_complete() &&
                 source.current_memory_request_role() == fastsim::fst::MemoryRequestRole::NoAccess && source.current_memory_request_micro_pc()==3,
                 "declared local role must survive decoding");
    require_role(!source.next(r),"request-role fixture must reach its exact end");
    // Complete streams with no sparse rows must declare Data; legacy streams
    // must not silently claim that all requests are known Data.
    const auto data=(dir/"data.fst").string();
    { fastsim::BinaryTraceWriter w(data,0); w.enable_memory_request_roles(); w.append(r); w.close(); }
    { fastsim::BinaryTraceSource d(data); require_role(d.next(r) &&
        d.current_memory_request_role()==fastsim::fst::MemoryRequestRole::Data && !d.next(r), "empty sparse table declares ordinary data"); }
    { fastsim::BinaryTraceWriter w((dir/"invalid.fst").string(),0); w.enable_memory_request_roles();
      auto bad=r; bad.size=0; bool rejected=false;
      try { w.append(bad,nullptr,{},fastsim::fst::MemoryRequestRole::NoAccess); }
      catch (const std::invalid_argument&) { rejected=true; }
      require_role(rejected,"NO_ACCESS must retain a nonempty address-check range"); }
    { std::fstream f(data,std::ios::binary|std::ios::in|std::ios::out); f.seekp(72); f.put('z'); }
    { bool rejected=false;
      try { fastsim::BinaryTraceSource d(data); d.next(r); }
      catch (const std::runtime_error&) { rejected=true; }
      require_role(rejected,"whole-stream binding must reject a changed ordinary record before exact EOF"); }
    const auto rewritten=(dir/"rewrite.fst").string();
    fastsim::upgrade_binary_trace_to_v7(p,rewritten);
    { fastsim::BinaryTraceSource d(rewritten); require_role(d.next(r) &&
        d.current_memory_request_role()==fastsim::fst::MemoryRequestRole::NoAccess && d.current_memory_request_micro_pc()==3 && !d.next(r), "upgrade must retain local request semantics"); }
    // A hot-record mutation must fail both at a special row and elsewhere in
    // the stream: a companion from another capture is not a valid substitute.
    auto rejects=[&](const std::string& message) {
        bool rejected=false;
        try { fastsim::BinaryTraceSource bad(p); while (bad.next(r)) {} }
        catch (const std::runtime_error&) { rejected=true; }
        require_role(rejected,message.c_str());
    };
    { std::fstream f(p,std::ios::binary|std::ios::in|std::ios::out); f.seekp(72); f.put('x'); }
    rejects("changed special record must reject its companion");
    role_fixture(p);
    { std::fstream f(p+".mreq",std::ios::binary|std::ios::in|std::ios::out); f.seekp(24); put(f,2,8); }
    rejects("companion record-count mismatch must reject");
    role_fixture(p); std::filesystem::remove(p+".mreq");
    rejects("declared capability must reject a missing companion");
    const auto local=(dir/"sim.fst").string();
    r={}; r.pc=0x1000; r.address=0x2000; r.size=8; r.op_class=57;
    r.flags=fastsim::kRetires|fastsim::kStore|fastsim::kPhysicalAddress;
    { fastsim::BinaryTraceWriter w(local,0); w.enable_memory_request_roles();
      w.append(r,nullptr,{},fastsim::fst::MemoryRequestRole::NoAccess,0);
      auto load=r; load.pc+=4; load.op_class=56;
      load.flags=fastsim::kRetires|fastsim::kLoad|fastsim::kPhysicalAddress;
      w.append(load); w.close(); }
    auto run=[&](bool fast, unsigned q, bool audit=true, bool one_sq=false, bool certificate=true, unsigned chunk=0) {
      fastsim::SimulatorConfig c;
      c.cores=1; c.core_model="interval_weave"; c.interval_scheduler="time_epoch";
      c.chunk_instructions=c.interval_target_uops=chunk ? chunk : q; c.interval_max_cycles=q;
      c.dtlb.enabled=true;
      c.response_queue_feedback=true; c.response_sparse_scoreboard=true;
      c.response_block_summary=true; c.response_memory_descriptor=true;
      c.response_monotone_iq_calendar=true; c.response_activity_certificate=certificate;
      c.cpi_attribution=!fast; c.response_frontier_audit_stride_uops=audit ? 1 : 0;
      c.response_interval_tail_audit=fast && audit;
      if (one_sq) { c.sq_entries=1; c.tso_store_buffer_size=1; }
      c.response_materialized_uop_fast_kernel=fast; c.interval_private_preview=fast;
      c.store_commit_to_iew_delay=1;
      c.tso_store=fastsim::TsoStoreMode::kBuffer; c.store_forwarding=true;
      c.l1d.size_bytes=4096; c.l2.size_bytes=16384; c.llc.size_bytes=65536;
      c.cha_count=c.dram.channels=c.dram.banks_per_channel=1;
      c.validate();
      std::vector<std::unique_ptr<fastsim::TraceSource>> traces;
      traces.push_back(std::make_unique<fastsim::BinaryTraceSource>(local));
      fastsim::Simulator sim(c,std::move(traces)); return sim.run();
    };
    for (bool fast : {false,true}) for (unsigned q : {1,64}) {
      const auto result=run(fast,q); const auto total=result.total_core();
      require_role(total.retired_uops==2 && total.retired_instructions==2,
                   "NO_ACCESS must retain UOP and macro populations");
      require_role(total.memory_accesses==1 && total.forwarded_loads==0,
                   "NO_ACCESS must not issue cache traffic or forward invented store data");
      require_role(total.dtlb.accesses==2,
                   "NO_ACCESS must retain address translation");
    }
    { fastsim::BinaryTraceWriter w(local,0); w.enable_memory_request_roles();
      for (unsigned i=0;i<128;++i) {
        r.pc=0x4000+4*i; r.address=0x8000;
        w.append(r,nullptr,{},fastsim::fst::MemoryRequestRole::NoAccess);
      }
      w.close(); }
    const auto local_segment=run(true,256,false,false,true,256);
    require_role(local_segment.response_activity_certified_segments==0,
                 "local SQ requests must not qualify as memory-free activity segments");
    require_role(local_segment.total_core().cycles ==
                 run(true,256,false,false,false,256).total_core().cycles,
                 "activity certificate must preserve local request timing");
    { fastsim::BinaryTraceWriter w(local,0); w.enable_memory_request_roles();
      for (unsigned group=0;group<256;++group) {
        r.pc=0x2000; r.address=0x8000+group*128; r.op_class=57; w.append(r);
        r.pc+=4; r.address+=72;
        w.append(r,nullptr,{},fastsim::fst::MemoryRequestRole::NoAccess,0);
        r.pc+=4; r.address-=8; r.op_class=59; w.append(r);
        r.pc+=4; r.address+=8; w.append(r);
      }
      w.close(); }
    for (bool fast : {false,true}) for (unsigned q : {1,64}) {
      auto result=run(fast,q);
      require_role(result.interval_steps>1,"local SQ fixture must cross actual timing epochs");
      require_role(result.total_core().cycles==run(fast,q,false).total_core().cycles,
                   "local SQ timing must be neutral to audit export and activity-certificate selection");
      auto row=[&](unsigned sequence)->const fastsim::ResponseFrontierAuditSample& {
        const auto& rows=result.response_frontier_audit.at(0);
        auto it=std::find_if(rows.begin(),rows.end(),[&](const auto& r){return r.sequence==sequence;});
        require_role(it!=rows.end(),"local SQ audit coverage must include every member"); return *it;
      };
      require_role(row(1).store_sq_release_cycle >= row(0).store_sq_release_cycle+1,
                   "local SQ entry must wait for the older FIFO store callback");
      require_role(row(1).memory_events.empty() && row(1).regular_store==0,
                   "local completion must not acquire a hierarchy service");
      require_role(row(2).store_send_cycle >= row(1).store_sq_release_cycle,
                   "younger mandatory store cannot pass a local SQ entry");
      if (row(2).buffer_tso_prepare_service_cycles) {
        require_role(row(2).buffer_tso_permission_ready_cycle >=
                     row(1).store_sq_release_cycle+row(2).buffer_tso_prepare_service_cycles,
                     "younger prepare cannot finish before local scan barrier and its own service");
      }
      bool witnessed=false;
      for (unsigned group=0;group<256;++group) {
        const auto& local_row=row(group*4+1);
        for (unsigned member=2;member<4;++member) {
          const auto& store_row=row(group*4+member);
          if (!store_row.buffer_tso_prepare_service_cycles) continue;
          witnessed=true;
          require_role(store_row.buffer_tso_permission_ready_cycle >=
              local_row.store_sq_release_cycle+store_row.buffer_tso_prepare_service_cycles,
              "an optional service must respect its local scan predecessor");
        }
      }
      require_role(witnessed,"at least one optional prepare must exercise the scan constraint");
    }
    { fastsim::BinaryTraceWriter w(local,0); w.enable_memory_request_roles();
      r.pc=0x3000; r.address=0xa000; r.op_class=57; w.append(r);
      r.pc+=4; r.address=0xb008; w.append(r,nullptr,{},fastsim::fst::MemoryRequestRole::NoAccess);
      r.pc+=4; r.address=0xa000; r.op_class=56;
      r.flags=fastsim::kRetires|fastsim::kLoad|fastsim::kPhysicalAddress;
      w.append(r); w.close(); }
    require_role(run(false,64,true,true).total_core().forwarded_loads==0,
                 "a local SQ entry must age older forwarding candidates without inventing data");
    { fastsim::BinaryTraceWriter w(local,0); w.enable_memory_request_roles();
      r.pc=0x5000; r.address=0xa000; r.op_class=57;
      r.flags=fastsim::kRetires|fastsim::kStore|fastsim::kPhysicalAddress; w.append(r);
      r.pc+=4; r.address=0xb008; r.flags|=fastsim::kSerialize;
      w.append(r,nullptr,{},fastsim::fst::MemoryRequestRole::NoAccess);
      r.pc+=4; r.address=0xa000; r.op_class=56;
      r.flags=fastsim::kRetires|fastsim::kLoad|fastsim::kPhysicalAddress;
      w.append(r); w.close(); }
    require_role(run(false,64).total_core().forwarded_loads==0,
                 "local bypass must preserve serialization of the forwarding window");
    // A cold prefetch is CPU-complete at acceptance while its copy remains
    // live until the data callback. A demand follower must wait for that fill.
    const auto prefetch_run=[&](unsigned capacity, bool fast, unsigned quantum, bool mshr=false, bool early=true, unsigned chunk=64) {
      fastsim::SimulatorConfig c;
      c.cores=1; c.core_model="interval_weave"; c.interval_scheduler="time_epoch";
      c.chunk_instructions=c.interval_target_uops=chunk; c.interval_max_cycles=quantum;
      c.response_queue_feedback=true; c.response_sparse_scoreboard=true;
      c.response_block_summary=true; c.response_memory_descriptor=true;
      c.response_materialized_uop_fast_kernel=fast;
      c.interval_private_preview=fast; c.response_frontier_audit_stride_uops=1;
      c.cpi_attribution=!fast; c.response_interval_tail_audit=fast;
      c.response_monotone_iq_calendar=true; c.needs_tso=true;
      c.ruby_sequencer_max_outstanding=capacity;
      c.software_prefetch_early_ack=early;
      if (mshr) c.l1d_mshrs=c.l2_mshrs=1;
      c.l1d.size_bytes=4096; c.l2.size_bytes=16384; c.llc.size_bytes=65536;
      c.cha_count=c.dram.channels=c.dram.banks_per_channel=1;
      c.validate();
      std::vector<std::unique_ptr<fastsim::TraceSource>> traces;
      traces.push_back(std::make_unique<fastsim::BinaryTraceSource>(sw));
      fastsim::Simulator sim(c,std::move(traces)); return sim.run();
    };
    { fastsim::BinaryTraceWriter w(sw,0); w.enable_memory_request_roles();w.enable_functional_retirement();
      for(unsigned i=0;i<3;++i) { pf.pc=0x8000+4*i;
        w.append(pf,nullptr,{},static_cast<fastsim::fst::MemoryRequestRole>(2)); }
      pf.pc+=4; w.append(pf); w.close(); }
    for (bool fast : {false,true}) for(unsigned quantum : {1,64}) {
      const auto result=prefetch_run(32,fast,quantum);
      const auto& rows=result.response_frontier_audit.at(0);
      require_role(rows.size()==4 && result.total_core().retired_uops==4,
                   "prefetch preserves committed population across epochs");
      require_role(rows[0].memory_events.size()==1,
                   "accepted prefetch must retain its cache service");
      const auto fill=rows[0].memory_events[0].response_cycle;
      require_role(rows[0].actual_completion_cycle < fill && rows[0].actual_retire_cycle < fill,
                   "accepted prefetch CPU completion must precede cold data fill");
      require_role(rows[3].actual_completion_cycle >= fill,
                   "demand follower cannot consume a prefetch acknowledgement as data");
      require_role(result.total_core().perf_retirement.physical.perf_retired_loads==4,
                   "SPR software prefetch instructions must count as retired loads");
      const auto& retirement=result.total_core().perf_retirement.physical;
      if (!(retirement.l1_hit_qualified>=3 &&
            retirement.estimated_l1_misses<=1 && retirement.estimated_l2_misses<=1 &&
            retirement.estimated_l3_misses<=1 && retirement.l1_miss_unknown<=1 &&
            retirement.l2_miss_unknown<=1 && retirement.l3_miss_unknown<=1))
        throw std::runtime_error("SPR hint classification: L1hit="+std::to_string(retirement.l1_hit_qualified)+
          " misses="+std::to_string(retirement.estimated_l1_misses)+","+
          std::to_string(retirement.estimated_l2_misses)+","+std::to_string(retirement.estimated_l3_misses)+
          " unknown="+std::to_string(retirement.l1_miss_unknown)+","+
          std::to_string(retirement.l2_miss_unknown)+","+std::to_string(retirement.l3_miss_unknown));
      require_role(result.total_core().perf_retirement.physical.l1_hit_qualified>=3 &&
                   result.total_core().perf_retirement.physical.estimated_l1_misses<=1 &&
                   result.total_core().perf_retirement.physical.estimated_l2_misses<=1 &&
                   result.total_core().perf_retirement.physical.estimated_l3_misses<=1 &&
                   result.total_core().perf_retirement.physical.l1_miss_unknown<=1 &&
                   result.total_core().perf_retirement.physical.l2_miss_unknown<=1 &&
                   result.total_core().perf_retirement.physical.l3_miss_unknown<=1,
                   "SPR hints are known L1-hit/no-miss members; physical fills stay request events");
      require_role(result.total_core().perf_retired_loads==1,
                   "legacy retired demand cache classifier excludes software prefetch");
      require_role(result.total_core().l1d_read_tag_misses==1 &&
                   result.total_core().l2_data_demand_tag_misses==1 &&
                   result.total_core().llc_read_tag_misses==1,
                   "accepted hint native diagnostic tag miss remains separate from retired demand PMU");
      if (result.total_core().perf_cache.physical.software_prefetch!=2)
        throw std::runtime_error("third same-line prefetch: admitted="+
          std::to_string(result.total_core().perf_cache.physical.software_prefetch)+
          " skipped="+std::to_string(result.sequencer.at(0).software_prefetch_skipped)+
          " q="+std::to_string(quantum)+" fast="+std::to_string(fast));
    }
    // Functional identity is always available, while unclosed early-ack
    // timing must not become the production default.
    for (bool fast : {false,true}) for(unsigned q : {1,64}) {
      const auto conservative=prefetch_run(32,fast,q,false,false);
      const auto& first=conservative.response_frontier_audit.at(0).at(0);
      require_role(first.actual_completion_cycle >= first.memory_events.at(0).response_cycle,
                   "role2 default must conservatively wait for full response");
      require_role(conservative.total_core().perf_retirement.physical.perf_retired_loads==4 &&
                   conservative.total_core().perf_retired_loads==1,
                   "SPR hint retirement membership is independent of physical completion timing");
      (void)prefetch_run(1,fast,q,false,false);
    }
    // With one Sequencer slot, a failed attempt must wait for the preceding
    // callback and recheck the line table. The old duplicate list is gone.
    { bool rejected=false;
      try { (void)prefetch_run(1,false,64); }
      catch(const std::runtime_error& e) {
        rejected=std::string(e.what()).find("canonical reclassification after parent callback")!=std::string::npos;
      }
      require_role(rejected,
                   "a retry invalidating the canonical duplicate list must fail closed"); }
    { fastsim::BinaryTraceWriter w(sw,0); w.enable_memory_request_roles();
      for(unsigned i=0;i<3;++i) { pf.pc=0x8000+4*i; pf.address=0x9000+64*i;
        w.append(pf,nullptr,{},static_cast<fastsim::fst::MemoryRequestRole>(2)); }
      w.close(); }
    { bool rejected=false;
      try { (void)prefetch_run(1,false,64); }
      catch(const std::runtime_error& e) {
        rejected=std::string(e.what()).find("corrected background service owner")!=std::string::npos;
      }
      require_role(rejected,"capacity-delayed copied service needs an online owner before exporting timing"); }
    { bool rejected=false;
      try { (void)prefetch_run(32,false,64,true); }
      catch(const std::runtime_error& e) {
        rejected=std::string(e.what()).find("corrected background service owner")!=std::string::npos;
      }
      require_role(rejected,"moved background copies must fail closed even without a resident demand"); }
    { fastsim::BinaryTraceWriter w(sw,0); w.enable_memory_request_roles();
      for(unsigned i=0;i<4;++i) { pf.pc=0xa000+4*i; pf.address=0xb000+64*i;
        w.append(pf,nullptr,{},static_cast<fastsim::fst::MemoryRequestRole>(2)); }
      w.close(); }
    { bool rejected=false;
      try { (void)prefetch_run(2,false,64,true); }
      catch(const std::runtime_error& e) {
        rejected=std::string(e.what()).find("corrected background service owner")!=std::string::npos;
      }
      require_role(rejected,"coupled MSHR/capacity copy movement requires an online service owner"); }
    { fastsim::BinaryTraceWriter w(sw,0); w.enable_memory_request_roles();
      pf.pc=0xc000; pf.address=0xd000;
      w.append(pf,nullptr,{},static_cast<fastsim::fst::MemoryRequestRole>(2));
      pf.pc+=4; pf.address+=64;
      w.append(pf,nullptr,{},static_cast<fastsim::fst::MemoryRequestRole>(2));
      pf.pc+=4; w.append(pf); w.close(); }
    for (bool fast : {false,true}) for(unsigned q : {1,64}) {
      bool rejected=false;
      try { (void)prefetch_run(32,fast,q,true,true); }
      catch(const std::runtime_error& e) {
        rejected=std::string(e.what()).find("corrected background service owner")!=std::string::npos;
      }
      require_role(rejected,"P(A), P(B), D(B) must fail closed when copied B service moves");
      rejected=false;
      try { (void)prefetch_run(32,fast,q,true,true,2); }
      catch(const std::runtime_error& e) {
        rejected=std::string(e.what()).find("corrected background service owner")!=std::string::npos;
      }
      require_role(rejected,"copied parent movement must reject before exporting a future-chunk demand");
      const auto conservative=prefetch_run(32,fast,q,true,false);
      const auto& rows=conservative.response_frontier_audit.at(0);
      require_role(rows.at(1).actual_completion_cycle >= rows.at(1).memory_events.at(0).response_cycle &&
                   rows.at(2).actual_retire_cycle >= rows.at(1).memory_events.at(0).response_cycle,
                   "default conservative hint completion must retain its full response and retirement order");
    }
    require_role(!fastsim::SimulatorConfig{}.software_prefetch_early_ack,
                 "software-prefetch early acknowledgement must default off");
    const auto cfg=(dir/"prefetch.cfg").string();
    { std::ofstream out(cfg); out << "core.model=interval_weave\nsim.interval_scheduler=time_epoch\n"
      "core.response_queue_feedback=true\ncore.response_sparse_scoreboard=true\n"
      "core.software_prefetch_early_ack=true\n"; }
    require_role(fastsim::load_simulator_config(cfg).software_prefetch_early_ack,
                 "explicit software-prefetch timing opt-in must parse");
    { auto invalid=fastsim::SimulatorConfig{}; invalid.software_prefetch_early_ack=true;
      bool rejected=false; try { invalid.validate(); } catch(const std::invalid_argument&) { rejected=true; }
      require_role(rejected,"early acknowledgement requires its bounded response profile"); }
    std::filesystem::remove_all(dir);
}
