#include "fastsim/simulator.hpp"
#include "fastsim/cache.hpp"
#include "fastsim/spr_sequencer.hpp"
#include "fastsim/fst_store_requests.hpp"
#include <iostream>
#include <algorithm>
#include <fstream>
#include <sstream>
#include <stdexcept>
using namespace fastsim;
namespace {
void check(bool v,const char* s){if(!v)throw std::runtime_error(s);}
class Source final:public TraceSource {
    std::vector<TraceRecord> records_;std::size_t cursor_=0;fst::StoreRequestFacts facts_;bool complete_,secure_;
public:
    explicit Source(std::vector<TraceRecord> r,bool complete=true,bool secure=false):records_(std::move(r)),complete_(complete),secure_(secure){}
    bool next(TraceRecord& r) override {
        if(cursor_==records_.size())return false;
        r=records_[cursor_++];
        facts_={};facts_.address=r.address;facts_.size=r.size;if(secure_)facts_.request_flags=0x10000000;
        facts_.byte_enable=r.size==64?~0ull:((1ull<<r.size)-1);
        facts_.attributes=fst::kStoreFactsComplete|(r.is_write()?fst::kStoreOrdinary:(fst::kMemoryRead|fst::kOrdinaryLoad));if(secure_)facts_.attributes|=fst::kStoreSecure;
        if(r.is_write() && r.address==0) {facts_.attributes=fst::kStoreFactsComplete|fst::kStoreNoAccess;facts_.request_flags=0x80000;}
        return true;
    }
    std::string description()const override{return "constructed-native-memory-functional-fixture";}
    bool complete_dependencies()const override{return true;}
    bool functional_retirement_complete()const override{return true;}
    bool memory_request_roles_complete()const override{return true;}
    std::optional<fst::MemoryRequestRole> current_memory_request_role()const override{return fst::MemoryRequestRole::Data;}
    std::optional<bool> current_ordinary_load_eligible()const override{return true;}
    const fst::StoreRequestFacts* current_store_facts()const override{return &facts_;}
    const fst::StoreRequestFacts* current_memory_facts()const override{return complete_?&facts_:nullptr;}
};
SimulatorConfig cfg(unsigned chunk=1) {
    SimulatorConfig c;c.core_model="interval_weave";c.cores=1;c.coherence=false;c.causal_runtime=true;
    c.causal_native_memory=true;c.store_commit_to_iew_delay=0;
    c.causal_mandatory_queue_latency=1;c.causal_l1_data_access_latency=3;c.causal_mandatory_queue_ordered=false;c.causal_l1_request_latency=2;
    c.l1d.replacement=c.l2.replacement=c.llc.replacement=ReplacementPolicy::kLru;
    c.causal_l1_response_latency=2;c.causal_l2_request_latency=2;c.causal_l2_response_latency=2;c.causal_llc_to_l2_latency=1;
    c.spr_profile.limits={16,16,16,48,256,256,256,112,4,4};
    c.chunk_instructions=chunk;c.interval_target_uops=1;
    c.fetch_to_decode=c.decode_to_rename=c.rename_to_dispatch=0;
    c.dispatch_to_issue=c.issue_to_execute=c.execute_to_commit=c.load_response_to_ready=0;
    return c;
}
TraceRecord store(std::uint64_t address){TraceRecord r;r.op_class=57;r.flags=kRetires|kStore|kPhysicalAddress;r.address=address;r.size=8;return r;}
SimulationStats run(SimulatorConfig c,std::vector<TraceRecord> r){std::vector<std::unique_ptr<TraceSource>> s;s.push_back(std::make_unique<Source>(std::move(r)));Simulator sim(c,std::move(s));return sim.run();}
}
namespace {
struct Event {std::string kind;std::uint64_t cycle,core,sequence,fragment,line,issue,completion,generation;};
std::vector<Event> events(const std::string& path) {
    std::ifstream in(path);std::string row;std::vector<Event> result;std::getline(in,row);
    while(std::getline(in,row)) {
        std::istringstream stream(row);std::vector<std::string> fields;std::string field;
        while(std::getline(stream,field,','))fields.push_back(field);
        check(fields.size()==9,"audit schema");
        result.push_back({fields[0],std::stoull(fields[1]),std::stoull(fields[2]),std::stoull(fields[3]),
            std::stoull(fields[4]),std::stoull(fields[5]),std::stoull(fields[6]),std::stoull(fields[7]),std::stoull(fields[8])});
    }
    return result;
}
std::string contents(const std::string& path) {
    // Synchronous refill can interleave same-cycle audit delivery; identity,
    // cycle and every lifecycle relation must remain identical.
    std::ifstream in(path);std::vector<std::string> rows;std::string row;
    while(std::getline(in,row))rows.push_back(row);
    std::sort(rows.begin(),rows.end());std::ostringstream out;
    for(const auto& value:rows)out<<value<<'\n';return out.str();
}
TraceRecord load(std::uint64_t address){auto r=store(address);r.op_class=56;r.flags=kRetires|kLoad|kPhysicalAddress;return r;}
void tests() {
    {
        auto entry=cfg();entry.causal_runtime_audit_path="tmp/native-mandatory-entry.csv";
        run(entry,{load(4096)});std::uint64_t accepted=0,lookup=0;
        for(const auto& e:events(entry.causal_runtime_audit_path)){if(e.kind=="sequencer_request")accepted=e.cycle;if(e.kind=="private_probe")lookup=e.cycle;}
        check(lookup==accepted+3,"native mandatory entry pipeline must precede tag lookup, not delay an already-classified hit");
    }
    for(bool secure:{false,true}) {
        auto contract=cfg();std::vector<std::unique_ptr<TraceSource>> inputs;
        inputs.push_back(std::make_unique<Source>(std::vector<TraceRecord>{load(4096)},secure,secure));
        bool rejected=false;try{Simulator model(contract,std::move(inputs));model.run();}
        catch(const std::invalid_argument& e){rejected=std::string(e.what()).find(secure?"security-domain":"complete functional request")!=std::string::npos;}
        check(rejected,"native request cannot silently assume absent flags or conflate secure cache domains");
    }
    {
        CacheConfig shape{128,2,64,4,ReplacementPolicy::kLru};
        PrivateHierarchy cache(shape,shape);CoreCounters counters;
        check(cache.reserve_native_l1(cache.prepare_native_l1(1),counters,0).has_value(),"first native tag reservation");
        check(cache.reserve_native_l1(cache.prepare_native_l1(2),counters,0).has_value(),"second native tag reservation");
        check(cache.complete_native_l1(2,false,1) && cache.complete_native_l1(1,false,2),"owned responses complete in model arrival order");
        check(cache.prepare_native_l1(3).victim.evicted_line==2,"native read completion must update MRU without selecting a new fill victim");
    }
    {
        CacheConfig shape{128,2,64,4,ReplacementPolicy::kLru};SetAssociativeCache cache(shape);CacheCounters counters;
        check(cache.reserve_native(cache.prepare_native_allocation(1),counters,10).has_value() && cache.reserve_native(cache.prepare_native_allocation(2),counters,10).has_value(),"same-cycle allocations own two actual ways");
        check(cache.complete_native(1,10,false,true) && cache.complete_native(2,10,false,true) && cache.touch_native(1,10),"same-cycle model touches");
        const auto tied=cache.prepare_native_allocation(3);
        check(tied.victim.evicted_line==1,"native timestamp LRU ties retain first way despite a later same-cycle touch");
        check(cache.touch_native(1,11) && cache.prepare_native_allocation(3).victim.evicted_line==2,"later model cycle changes native timestamp victim");
        check(!cache.reserve_native(tied,counters,11),"stale allocation cannot reuse victim chosen before a real touch");
    }
    {
        auto alternative=cfg();alternative.l2.replacement=ReplacementPolicy::kTreePlru;
        bool rejected=false;try{run(alternative,{load(4096)});}catch(const std::invalid_argument& e){rejected=std::string(e.what()).find("timestamp LRURP")!=std::string::npos;}
        check(rejected,"unqualified native replacement policy cannot silently use legacy ages");
    }
    {
        auto ties=cfg(8);ties.l1d.size_bytes=128;ties.l1d.associativity=2;
        ties.causal_runtime_audit_path="tmp/native-runtime-lru-ties.csv";
        auto hit_b=load(8192);hit_b.producer_dists[0]=1;hit_b.n_src=1;
        auto hit_a=load(4096);hit_a.producer_dists[0]=2;hit_a.n_src=1;
        auto replace=load(12288);replace.producer_dists[0]=1;replace.producer_dists[1]=2;replace.n_src=2;
        run(ties,{load(4096),load(8192),hit_b,hit_a,replace});
        std::uint64_t b=0,a=1,victim=0;
        for(const auto& e:events(ties.causal_runtime_audit_path)) {
            if(e.kind=="private_probe" && e.sequence==2)b=e.cycle;
            if(e.kind=="private_probe" && e.sequence==3)a=e.cycle;
            if(e.kind=="l1_writeback_created")victim=e.line;
        }
        check(a==b && victim==64,"actual native same-cycle B then A hits must choose first-way A on later eviction");
    }
    {
        auto transient=cfg(4);transient.l1d.size_bytes=64;transient.l1d.associativity=1;
        transient.causal_runtime_audit_path="tmp/native-request-tag-red.csv";
        run(transient,{load(4096),load(8192)});
        std::uint64_t first_fill=UINT64_MAX,second_probe=0;
        for(const auto& e:events(transient.causal_runtime_audit_path)) {
            if(e.kind=="l1_fill" && e.sequence==0)first_fill=e.cycle;
            if(e.kind=="private_probe" && e.sequence==1)second_probe=e.cycle;
        }
        check(second_probe>=first_fill,"native request must reserve its transient tag and stall a conflicting admission before fill");
    }
    {
        auto queue=cfg(4);queue.l1d.size_bytes=64;queue.l1d.associativity=1;
        queue.causal_runtime_audit_path="tmp/native-sequencer-before-tags.csv";
        run(queue,{load(4096),load(8192),load(4100)});
        bool follower=false;std::uint64_t alias_cycle=UINT64_MAX,first_response=UINT64_MAX;
        for(const auto& e:events(queue.causal_runtime_audit_path)) {
            if(e.kind=="sequencer_follower" && e.sequence==2){follower=true;alias_cycle=e.cycle;}
            if(e.kind=="response" && e.sequence==0)first_response=e.cycle;
        }
        check(follower && alias_cycle<first_response,"blocked mandatory cache tag must not prevent a younger Sequencer alias from owning credit");
    }
    {
        auto mixed=cfg(1);mixed.causal_runtime_audit_path="tmp/native-mixed-forward.csv";
        const auto result=run(mixed,{store(4096),load(4096),load(8192)});
        check(result.cores[0].retired_instructions==3,"mixed unified source keeps all retired instructions");
        bool forwarded=false;
        for(const auto& e:events(mixed.causal_runtime_audit_path))forwarded|=e.kind=="store_forward_response";
        check(forwarded,"overlapping ordinary load must use actual store forwarding path");
    }

    {
        auto partial=cfg(4);partial.causal_runtime_audit_path="tmp/native-partial-forward.csv";
        auto narrow=store(4096);narrow.size=4;
        const auto result=run(partial,{narrow,load(4096),load(8192)});
        check(result.cores[0].retired_instructions==3,"partial-overlap wait preserves full mixed population");
        std::uint64_t store_response=UINT64_MAX,overlap_probe=0,independent_probe=UINT64_MAX;
        for(const auto& e:events(partial.causal_runtime_audit_path)) {
            if(e.kind=="store_write_complete")store_response=e.cycle;
            if(e.kind=="private_probe" && e.sequence==1)overlap_probe=std::max(overlap_probe,e.cycle);
            if(e.kind=="private_probe" && e.sequence==2)independent_probe=e.cycle;
        }
        check(overlap_probe>=store_response && independent_probe<store_response,"partial overlap waits selectively for WB while independent load proceeds");
    }
    {
        auto alias_config=cfg(4);alias_config.integer_multiply_latency=50;
        alias_config.causal_runtime_audit_path="tmp/native-read-write-reissue.csv";
        TraceRecord delay;delay.op_class=2;
        const auto result=run(alias_config,{delay,store(4096),load(4104)});
        bool follower=false,reissued=false,full_entry=false;
        for(const auto& e:events(alias_config.causal_runtime_audit_path)) {
            follower|=e.kind=="sequencer_follower";reissued|=e.kind=="sequencer_reissue";full_entry|=e.kind=="mandatory_enqueued" && e.generation==2 && e.issue==3;
        }
        check(result.cores[0].retired_instructions==3 && follower && reissued && full_entry,"read callback must reissue queued write through fresh backend service");
    }
    {
        auto zero=cfg(4);
        zero.causal_runtime_audit_path="tmp/native-zero-forward.csv";
        auto dependent=load(8192);dependent.producer_dists[0]=1;dependent.n_src=1;
        const auto result=run(zero,{store(4096),load(4096),dependent});
        std::uint64_t forwarded=UINT64_MAX,dependent_probe=UINT64_MAX;
        for(const auto& e:events(zero.causal_runtime_audit_path)) {
            if(e.kind=="store_forward_response")forwarded=e.cycle;
            if(e.kind=="sequencer_request" && e.sequence==2)dependent_probe=e.cycle;
        }
        check(result.cores[0].retired_instructions==3 && dependent_probe==forwarded+zero.minimum_load_latency,"forwarded response must add no delay beyond the actual execution-ready lower bound");
    }
    {
        auto wb=cfg(2);wb.spr_profile.limits.shared_stores=1;wb.l1d.size_bytes=64;wb.l1d.associativity=1;
        wb.causal_runtime_audit_path="tmp/native-writeback-service.csv";
        const auto result=run(wb,{store(4096),store(8192)});
        check(result.cores[0].retired_instructions==2,"dirty eviction must finish through an owned writeback service");
    }
    // Dirty private victims transfer data and protocol ownership into an
    // explicit WB source. Their late acknowledgements never extend CPI.
    for(unsigned level:{1u,2u}) {
        auto victim_config=cfg(2);victim_config.spr_profile.limits.shared_stores=1;
        victim_config.l1d.size_bytes=64;victim_config.l1d.associativity=1;
        if(level==2){victim_config.l1d.size_bytes=128;victim_config.l1d.associativity=2;victim_config.l2.size_bytes=64;victim_config.l2.associativity=1;}
        victim_config.causal_runtime_audit_path="tmp/native-dirty-victim-l"+std::to_string(level)+".csv";
        const auto result=run(victim_config,{store(4096),store(8192)});
        unsigned created=0,sent=0,applied=0,acked=0;std::uint64_t retire=0,ack=0;
        for(const auto& e:events(victim_config.causal_runtime_audit_path)) {
            if(e.kind==(level==1?"l1_writeback_created":"l2_writeback_created"))++created;
            if(e.kind=="writeback_send")++sent;
            if(e.kind=="writeback_target_apply"){++applied;ack=std::max(ack,e.cycle);}
            if(e.kind=="writeback_ack"){++acked;ack=std::max(ack,e.cycle);}
            if(e.kind=="retire")retire=std::max(retire,e.cycle);
        }
        check(created==1 && sent==1 && applied==1 && acked==(level==1?0u:1u),"private dirty victim gets exactly one owned writeback lifecycle");
        check(ack>retire && result.cores[0].cycles==retire && result.elapsed_reference_q16==(retire<<16),"writeback drain must not extend retired core/common elapsed");
    }
    for(unsigned llc_ways:{1u,2u}) {
        auto limited=cfg(2);limited.spr_profile.limits.shared_stores=1;
        limited.l1d.size_bytes=limited.l2.size_bytes=64;limited.llc.size_bytes=64*llc_ways;
        limited.l1d.associativity=limited.l2.associativity=1;limited.llc.associativity=llc_ways;
        limited.causal_runtime_audit_path="tmp/native-llc-limit"+std::to_string(llc_ways)+".csv";
        bool rejected=false;
        try{run(limited,{store(4096),store(8192),store(12288)});}
        catch(const std::invalid_argument& e){rejected=std::string(e.what()).find("MemCtrl admission acknowledgement")!=std::string::npos;if(!rejected)throw;}
        check(rejected,"dirty LLC victim cannot use physical DRAM completion as native memory acknowledgement");
    }
    {
        auto replacement=cfg(4);replacement.l1d.size_bytes=replacement.l2.size_bytes=64;
        replacement.l1d.associativity=replacement.l2.associativity=1;
        replacement.causal_l2_response_latency=100;replacement.l1d.fill_response_latency=50;
        replacement.causal_runtime_audit_path="tmp/native-ordered-fill-write.csv";
        const auto result=run(replacement,{store(4096),store(8192),store(12288)});
        unsigned retried=0,completed=0;
        for(const auto& e:events(replacement.causal_runtime_audit_path)) {
            retried+=e.kind=="write_permission_retry";completed+=e.kind=="store_write_complete";
        }
        check(!retried && completed==3 && result.cores[0].retired_instructions==3,"ordered return channel and atomic native fill callback must avoid artificial write ping-pong");
    }
    {
        // A committed write waits in the entry pipeline while a real sibling
        // miss invalidates its formerly resident line. Lookup must use current
        // permission, not the state seen at Sequencer acceptance.
        auto permission=cfg(32);permission.l2.size_bytes=512;permission.l2.associativity=1;
        permission.cache_store_ports=8;permission.memory_units=8;
        permission.dispatch_width=permission.issue_width=permission.commit_width=8;
        permission.fetch_width=permission.decode_width=permission.rename_width=8;
        permission.causal_runtime_audit_path="tmp/native-pending-permission.csv";
        std::vector<TraceRecord> stream;
        for(unsigned n=0;n<6;++n)stream.push_back(load(4096+64*n));
        TraceRecord join;join.op_class=1;join.flags=kRetires;join.n_src=4;
        for(unsigned n=0;n<4;++n)join.producer_dists[n]=6-n;
        stream.push_back(join);join.producer_dists={1,2,3,0};join.n_src=3;stream.push_back(join);
        for(unsigned n=1;n<=6;++n) {
            auto write=store(n==6?4096:4096+64*n);write.producer_dists[0]=stream.size()-7;write.n_src=1;stream.push_back(write);
        }
        auto miss=load(8192);miss.producer_dists[0]=stream.size()-7;miss.n_src=1;stream.push_back(miss);
        const auto result=run(permission,stream);
        unsigned completions=0,invalidations=0,retries=0;std::uint64_t accepted=0,invalidated=0,probe=0,retired=0,write_done=0;
        for(const auto& e:events(permission.causal_runtime_audit_path)) {
            completions+=e.kind=="store_write_complete";
            if(e.kind=="sequencer_request" && e.generation==2 && e.line==64)accepted=e.cycle;
            if(e.kind=="l1_invalidation" && e.line==64){invalidated=e.cycle;++invalidations;}
            if(e.kind=="write_permission_retry")++retries;
            if(e.kind=="private_probe" && e.generation==2 && e.line==64)probe=e.cycle;
            if(e.kind=="retire")retired=std::max(retired,e.cycle);
            if(e.kind=="store_write_complete" && e.line==64)write_done=e.cycle;
        }
        check(invalidations==1 && retries==0,"ungranted L0 transient must not create a second parent invalidation or artificial permission retry");
        check(accepted<invalidated && invalidated<=probe && completions==6 && result.cores[0].retired_instructions==stream.size(),"invalidation during mandatory entry must force fresh lookup and exactly-once original writes");
        check(write_done>retired && result.cores[0].cycles==retired && result.elapsed_reference_q16==(retired<<16),"permission/WB tail after mixed retirement cannot extend core or common elapsed");
    }
    {
        auto race=cfg(8);race.l1d.size_bytes=128;race.l1d.associativity=2;
        race.l2.size_bytes=128;race.l2.associativity=1;
        race.integer_alu_latency=50;race.integer_multiply_latency=53;
        race.causal_runtime_audit_path="tmp/native-putx-invalidation-race.csv";
        auto write=store(4096);write.producer_dists[0]=1;write.n_src=1;
        TraceRecord earlier;earlier.op_class=1;earlier.flags=kRetires;earlier.producer_dists[0]=2;earlier.n_src=1;
        auto later=earlier;later.op_class=2;later.producer_dists[0]=3;
        auto b=load(8192);b.producer_dists[0]=2;b.n_src=1;
        auto c=load(8256);c.producer_dists[0]=2;c.n_src=1;
        const auto result=run(race,{load(4160),load(4096),write,earlier,later,b,c});
        std::uint64_t sent=0,put=0,inv=0,returned=0;unsigned data_messages=0,parent_writes=0;
        for(const auto& e:events(race.causal_runtime_audit_path))if(e.line==64) {
            if(e.kind=="l2_invalidation_send")sent=e.cycle;
            if(e.kind=="l1_writeback_created"){put=e.cycle;++data_messages;}
            if(e.kind=="l1_invalidation"){inv=e.cycle;check(e.issue==0,"evicted child must ACK without duplicating already-sent dirty data");}
            if(e.kind=="l2_invalidation_return")returned=e.cycle;
            if(e.kind=="l2_writeback_created")++parent_writes;
        }
        check(sent<put && put<inv && inv<returned && data_messages==1 && parent_writes==1 && result.cores[0].retired_instructions==7,"local PUTX racing an in-flight inclusion invalidation must transfer dirty data exactly once");
    }
    {
        auto selective=cfg(4);selective.l1d.size_bytes=128;selective.l1d.associativity=1;selective.memory_units=8;
        selective.causal_runtime_audit_path="tmp/native-mandatory-selective.csv";
        run(selective,{load(4096),load(4224),load(4160)});
        std::uint64_t first=0,independent=UINT64_MAX,conflict=0;
        for(const auto& e:events(selective.causal_runtime_audit_path))if(e.kind=="private_probe") {
            if(e.sequence==0)first=e.cycle;if(e.sequence==1)conflict=e.cycle;if(e.sequence==2)independent=e.cycle;
        }
        check(independent==first && conflict>first,"native stall-and-wait victim cannot serialize independent ready mandatory requests");
    }
    {
        auto overlap=cfg(4);overlap.causal_runtime_audit_path="tmp/native-ordered-entry-overlap.csv";
        auto a=store(4096);a.producer_dists[0]=1;a.n_src=1;
        auto b=store(8192);b.producer_dists[0]=2;b.n_src=1;
        run(overlap,{load(4096),load(8192),a,b});
        bool shortened=false;unsigned writes=0;
        for(const auto& e:events(overlap.causal_runtime_audit_path)) {
            shortened|=e.kind=="mandatory_enqueued" && e.generation==2 && e.issue==1;
            writes+=e.kind=="store_write_complete";
        }
        check(shortened && writes==2,"ordered-store head-rate reissue may overlap alias waiting but retains a positive entry cycle");
    }
    {
        auto cpu=cfg(4);cpu.spr_profile.limits.ordinary_requests=1;cpu.l1d.fill_response_latency=50;
        cpu.causal_runtime_audit_path="tmp/native-sequencer-before-cpu.csv";
        run(cpu,{load(4096),load(8192)});
        std::uint64_t release=0,next=0,returned=0;
        for(const auto& e:events(cpu.causal_runtime_audit_path)) {
            if(e.kind=="sequencer_response" && e.sequence==0)release=e.cycle;
            if(e.kind=="sequencer_request" && e.sequence==1)next=e.cycle;
            if(e.kind=="response" && e.sequence==0)returned=e.cycle;
        }
        check(release<=next && next<returned,"ordinary Sequencer credit release must not wait for CPU response propagation");
    }
    {
        auto configuration=cfg();spr::OnlineSequencer sequence(0,configuration.spr_profile.limits);
        const PendingOwner owner{0,1,0,2};
        const auto grant=sequence.grant(sequence.peek({owner,4096,spr::SequencerKind::Write,true,0}),true,0);
        const auto old=grant.actions.at(0).service;
        check(sequence.retry_service(old,1).callback_accepted && sequence.outstanding()==1,"permission retry preserves ordinary credit");
        check(!sequence.callback(old,spr::SequencerCallback::Write,2).callback_accepted,"invalidated old service cannot complete current owner");
        const auto fresh=sequence.peek_service();check(fresh && sequence.grant_service(*fresh,true,2),"retry requires fresh backend grant");
        check(sequence.callback(*fresh,spr::SequencerCallback::Write,3).wrote && sequence.outstanding()==0,"fresh callback releases owner exactly once");
    }
    // Protocol delays and ownership come from native message edges, not tag
    // hit latencies. In particular L1 PUTX neither takes a TBE nor gets ACK.
    for(unsigned level:{1u,2u}) {
        auto message=cfg(2);message.spr_profile.limits.shared_stores=1;
        message.causal_l1_response_latency=7;message.causal_l2_request_latency=11;
        message.causal_l2_response_latency=13;message.causal_llc_to_l2_latency=17;
        message.l1d.size_bytes=64;message.l1d.associativity=1;
        if(level==2){message.l1d.size_bytes=128;message.l1d.associativity=2;message.l2.size_bytes=64;message.l2.associativity=1;}
        message.causal_runtime_audit_path="tmp/native-message-l"+std::to_string(level)+".csv";
        run(message,{store(4096),store(8192)});
        std::uint64_t send=0,apply=0,ack=0,release=0,inv_send=0,inv_receive=0,inv_return=0;
        unsigned retained=0,released=0;
        for(const auto& e:events(message.causal_runtime_audit_path)) {
            if(e.kind=="writeback_send")send=e.cycle;
            if(e.kind=="writeback_target_apply")apply=e.cycle;
            if(e.kind=="writeback_ack")ack=e.cycle;
            if(e.kind=="l2_invalidation_send")inv_send=e.cycle;
            if(e.kind=="l1_invalidation")inv_receive=e.cycle;
            if(e.kind=="l2_invalidation_return")inv_return=e.cycle;
            if(e.kind=="l2_writeback_tbe_acquire")++retained;
            if(e.kind=="l2_credit_release" && e.generation==4){++released;release=e.cycle;}
        }
        check(apply-send==(level==1?7:13+message.noc_one_way_latency),"PUTX uses imported source response latency");
        check(level==1?(ack==0 && retained==0 && released==0):(ack-apply==17+message.noc_one_way_latency && retained==1 && released==1 && release==ack),"only private-L2 replacement retains TBE until imported LLC ACK");
        if(level==2)check(inv_receive-inv_send==11 && inv_return-inv_receive==7,"inclusion eviction waits actual invalidate and return message edges");
    }
    {
        auto clean=cfg(2);clean.l1d.size_bytes=64;clean.l1d.associativity=1;
        clean.causal_runtime_audit_path="tmp/native-clean-exclusive-putx.csv";
        const auto result=run(clean,{load(4096),load(8192)});
        unsigned puts=0;for(const auto& e:events(clean.causal_runtime_audit_path))if(e.kind=="writeback_target_apply"){++puts;check(e.completion==0,"clean exclusive PUTX must not create dirty data");}
        check(puts==1 && result.cores[0].l1d.writebacks==0,"clean exclusive L1 replacement still sends native PUTX");
    }
    {
        auto missing=cfg(2);missing.causal_l1_response_latency.reset();
        missing.l1d.size_bytes=64;missing.l1d.associativity=1;
        bool rejected=false;try{run(missing,{load(4096),load(8192)});}catch(const std::invalid_argument& e){rejected=std::string(e.what()).find("spr.memory.l1_response_latency")!=std::string::npos;}
        check(rejected,"missing native message edge cannot fall back to cache hit latency");
    }
    {
        std::string previous;
        for(unsigned batch:{1u,2u,4u}) {
            auto combined=cfg(batch);combined.spr_profile.limits.shared_stores=1;
            combined.l1d.size_bytes=combined.l2.size_bytes=64;
            combined.l1d.associativity=combined.l2.associativity=1;
            combined.causal_runtime_audit_path="tmp/native-mixed-wb-batch"+std::to_string(batch)+".csv";
            const auto result=run(combined,{store(4096),load(4096),store(8192),load(8192),store(12288),load(12288)});
            check(result.cores[0].retired_instructions==6,"combined forwarding/replacement preserves all source owners");
            const auto rows=contents(combined.causal_runtime_audit_path);
            if(!previous.empty())check(rows==previous,"mixed/forwarding/invalidation/WB owner times invariant to source batch");
            previous=rows;
        }
    }
    {
        auto one=cfg(2);one.spr_profile.limits.shared_stores=1;one.spr_profile.limits.l2_tbes=1;
        one.l1d.size_bytes=128;one.l1d.associativity=2;one.l2.size_bytes=64;one.l2.associativity=1;
        one.causal_runtime_audit_path="tmp/native-replacement-tbe-one.csv";
        run(one,{store(4096),store(8192)});
        std::uint64_t ack=0,demand=0;unsigned owned=0;
        for(const auto& e:events(one.causal_runtime_audit_path)) {
            if(e.kind=="l2_writeback_tbe_acquire")++owned;
            if(e.kind=="writeback_ack")ack=e.cycle;
            if(e.kind=="l2_credit_acquire" && e.line==128)demand=e.cycle;
        }
        check(owned==1 && ack && demand==ack,"one TBE must service replacement first then admit distinct demand at the same real ACK cycle");
    }
    // The opt-in guard stops before legacy dirty propagation into L2; the
    // default helper preserves the historical immediate writeback behavior.
    for(bool reject_dirty:{false,true}) {
        CacheConfig l1{64,1,64,4,ReplacementPolicy::kLru};
        CacheConfig l2{256,4,64,12,ReplacementPolicy::kLru};
        PrivateHierarchy hierarchy(l1,l2);CoreCounters counters;
        hierarchy.complete_fill_l2(64,counters.l2);
        hierarchy.complete_fill_l1(64,true,counters);
        hierarchy.complete_fill_l2(128,counters.l2);
        bool rejected=false;
        try {
            if(reject_dirty)hierarchy.complete_fill_l1(128,false,counters,nullptr,true);
            else hierarchy.complete_fill_l1(128,false,counters);
        } catch(const std::invalid_argument&){rejected=true;}
        check(rejected==reject_dirty,"dirty-victim rejection remains opt-in");
        check(hierarchy.contains(64),"dirty victim still has resident inclusive L2 parent");
        check(hierarchy.dirty(64)==!reject_dirty,"guard must precede immediate dirty propagation into L2");
    }
    // Detached completion must never extend the last architectural retirement.
    auto c=cfg();c.causal_runtime_audit_path="tmp/native-store-tail.csv";
    const auto result=run(c,{store(4096)});
    check(result.cores[0].retired_instructions==1,"ordinary store retained");
    std::uint64_t last_response=0,last_retire=0;bool explicit_drain=false;
    for(const auto& e:events(c.causal_runtime_audit_path)) {
        if(e.kind=="store_drain_source")explicit_drain=true;
        if(e.kind=="response")last_response=e.cycle;
        if(e.kind=="retire")last_retire=e.cycle;
    }
    check(explicit_drain,"stores must publish a dedicated coordinator-returned drain source");
    check(last_response>last_retire && result.cores[0].cycles==last_retire,"store callback tail must not extend core CPI");
    check(result.elapsed_reference_q16==(last_retire<<16),"tail drain must not extend common elapsed");
    // The 17th same-line request must wait despite an existing line alias.
    c=cfg(32);c.causal_runtime_audit_path="tmp/native-alias-capacity.csv";
    std::vector<TraceRecord> aliases(17,load(4096));const auto alias=run(c,aliases);
    check(alias.cores[0].retired_instructions==17 && alias.cores[0].memory_accesses==17,"followers preserve original population");
    std::size_t admitted=0;std::uint64_t first_response=UINT64_MAX,request17=0;
    for(const auto& e:events(c.causal_runtime_audit_path)) {
        if(e.kind=="response")first_response=std::min(first_response,e.cycle);
        if(e.kind=="sequencer_request" || e.kind=="sequencer_follower") {
            ++admitted;if(e.sequence==16)request17=e.cycle;
        }
    }
    check(admitted==17 && request17==first_response,"ordinary credit checked before alias and reused at same-cycle callback");
    check(alias.cha[0].dram_reads==1 && alias.cores[0].l1d.misses==1,"local aliases have one physical service");
    // Typed fill capacity is independent of historical MSHR configuration.
    c=cfg(4);c.l1d_mshrs=1;c.l2_mshrs=1;c.spr_profile.limits.l1_data_fills=2;
    c.causal_runtime_audit_path="tmp/native-typed-fills.csv";
    run(c,{load(4096),load(8192)});
    std::uint64_t second_probe=UINT64_MAX;first_response=UINT64_MAX;
    for(const auto& e:events(c.causal_runtime_audit_path)) {
        if(e.kind=="private_probe" && e.sequence==1)second_probe=e.cycle;
        if(e.kind=="response")first_response=std::min(first_response,e.cycle);
    }
    check(second_probe<first_response,"native typed credits must not use old MSHR proxy");
    // Fills release at level data visibility, independently of delayed CPU reply.
    c=cfg(4);c.spr_profile.limits.l1_data_fills=1;c.l1d.fill_response_latency=17;
    c.causal_runtime_audit_path="tmp/native-fill-release.csv";run(c,{load(4096),load(8192)});
    std::uint64_t first_fill=UINT64_MAX;second_probe=UINT64_MAX;first_response=UINT64_MAX;
    for(const auto& e:events(c.causal_runtime_audit_path)) {
        if(e.kind=="l1_fill" && e.sequence==0)first_fill=e.cycle;
        if(e.kind=="private_probe" && e.sequence==1)second_probe=e.cycle;
        if(e.kind=="response" && e.sequence==0)first_response=e.cycle;
    }
    check(first_fill==second_probe && second_probe<first_response,"L1 data-fill lease released before CPU response");
    // Persistent buffer/Sequencer state is independent of source batching.
    std::vector<TraceRecord> stores;
    for(unsigned n=0;n<12;++n)stores.push_back(store(4096+(n/3)*64+(n%3)*8));
    std::string prior;
    for(unsigned chunk:{1u,2u,16u}) {
        c=cfg(chunk);c.causal_runtime_audit_path="tmp/native-store-chunk"+std::to_string(chunk)+".csv";
        const auto out=run(c,stores);check(out.cores[0].retired_instructions==12,"store full population");
        auto text=contents(c.causal_runtime_audit_path);if(!prior.empty())check(text==prior,"cross-chunk store lifecycle invariant");prior=text;
    }
    std::size_t writes=0;
    for(const auto& e:events("tmp/native-store-chunk1.csv"))if(e.kind=="store_write_accepted")++writes;
    check(writes<12,"tail merge reduces synthetic writes without losing originals");
    c=cfg(16);c.causal_runtime_audit_path="tmp/native-store-four-lines.csv";
    run(c,{store(4096),store(8192),store(12288),store(16384),store(20480)});
    std::size_t before_return=0;bool returned=false;
    for(const auto& e:events(c.causal_runtime_audit_path)) {
        if(e.kind=="response")returned=true;
        if(e.kind=="store_write_accepted" && !returned)++before_return;
    }
    check(before_return==4,"four distinct in-flight store lines gate fifth request");
    // L2 fills and LLC TBEs use independent domains and real release edges.
    for(unsigned domain=0;domain<2;++domain) {
        c=cfg(4);if(domain==0)c.spr_profile.limits.l2_data_fills=1;else c.spr_profile.limits.llc_tbes=1;
        c.causal_runtime_audit_path="tmp/native-protocol-"+std::to_string(domain)+".csv";
        const auto out=run(c,{load(4096),load(8192)});
        check(out.cores[0].retired_instructions==2,"protocol backpressure preserves full population");
        std::uint64_t first_fill=UINT64_MAX,second_acquire=0;
        for(const auto& e:events(c.causal_runtime_audit_path)) {
            if(e.kind==(domain==0?"l2_fill":"llc_tbe_release") && e.sequence==0)first_fill=e.cycle;
            if(e.kind==(domain==0?"l2_credit_acquire":"llc_tbe_acquire") && e.sequence==1)second_acquire=e.cycle;
        }
        check(second_acquire==first_fill,"per-level protocol credit resumes on actual fill without tick");
    }
    // Every core publishes its full same-cycle candidate prefix before shared
    // arbitration. Source batching cannot select a different owner/winner.
    prior.clear();
    for(unsigned chunk:{1u,2u,4u}) {
        c=cfg(chunk);c.cores=2;c.cha_count=1;c.spr_profile.limits.llc_tbes=1;
        c.causal_runtime_audit_path="tmp/native-multicore-chunk"+std::to_string(chunk)+".csv";
        std::vector<std::unique_ptr<TraceSource>> inputs;
        inputs.push_back(std::make_unique<Source>(std::vector<TraceRecord>{load(4096),load(8192),load(12288)}));
        inputs.push_back(std::make_unique<Source>(std::vector<TraceRecord>{load(16384),load(20480)}));
        Simulator simulation(c,std::move(inputs));const auto out=simulation.run();
        check(out.cores[0].retired_instructions==3 && out.cores[1].retired_instructions==2,"multicore full read population");
        auto text=contents(c.causal_runtime_audit_path);
        if(!prior.empty())check(text==prior,"shared constrained-credit winners/timing/retire tuples must be source-batch invariant");
        prior=text;
    }
    c=cfg(4);c.noc_one_way_latency=0;c.spr_profile.limits.llc_tbes=1;
    const auto zero_transport=run(c,{load(4096),load(8192)});
    check(zero_transport.cores[0].retired_instructions==2,"zero transport preserves LLC-release before private-fill phase order");
    c=cfg(4);c.spr_profile.limits.llc_tbes=1;c.integer_multiply_latency=84;
    c.causal_runtime_audit_path="tmp/native-blocked-before-new.csv";
    TraceRecord delay;delay.op_class=2;auto later=load(12288);later.producer_dists[0]=1;later.n_src=1;
    run(c,{load(4096),load(8192),delay,later});
    std::uint64_t older_grant=UINT64_MAX,newer_grant=0;
    for(const auto& e:events(c.causal_runtime_audit_path))if(e.kind=="llc_tbe_acquire") {
        if(e.sequence==1)older_grant=e.cycle;if(e.sequence==3)newer_grant=e.cycle;
    }
    check(older_grant<newer_grant,"released shared TBE must wake queued older request before new same-cycle arrival");
    // Shared SQ+committed capacity blocks dispatch until actual callback release.
    c=cfg(4);c.spr_profile.limits.shared_stores=1;c.causal_runtime_audit_path="tmp/native-store-capacity.csv";
    run(c,{store(4096),store(8192)});
    std::uint64_t release=UINT64_MAX,allocate2=0;
    for(const auto& e:events(c.causal_runtime_audit_path)) {
        if(e.kind=="store_credit_released")release=std::min(release,e.cycle);
        if(e.kind=="store_allocate" && e.sequence==1)allocate2=e.cycle;
    }
    check(allocate2==release,"offload must not release shared SQ+buffer credit; actual callback wakes dispatch same cycle");
    c=cfg();c.causal_runtime_audit_path="tmp/native-no-access.csv";
    auto no_access_store=store(0);no_access_store.size=0;no_access_store.flags=kRetires|kStore;
    const auto noaccess=run(c,{no_access_store});
    check(noaccess.cores[0].memory_accesses==0 && noaccess.cores[0].retired_instructions==1,"NO_ACCESS retires without a cache request");
    check(run(cfg(),{store(4096),load(4096)}).cores[0].retired_instructions==2,"mixed store/load retained");
    check(run(cfg(),{load(4096),store(4096)}).cores[0].retired_instructions==2,"mixed read/write retained");
    bool rejected=false;
    rejected=false;c=cfg();c.spr_profile.early_store_prepare=true;
    try{c.validate();}catch(const std::invalid_argument&){rejected=true;}
    check(rejected,"missing address/data role facts must not masquerade as early prepare support");
}
}
int main(){try{tests();std::cout<<"native memory runtime tests passed\n";}catch(const std::exception& e){std::cerr<<e.what()<<'\n';return 1;}}
