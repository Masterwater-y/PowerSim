#include "fastsim/simulator.hpp"
#include "fastsim/cache.hpp"
#include <iostream>
#include <stdexcept>
using namespace fastsim;
namespace {
void require(bool ok,const char* message){if(!ok)throw std::runtime_error(message);}
class Source final:public TraceSource {
  public:
    std::vector<TraceRecord> records;std::size_t index=0;
    VirtualPageMapping mapping{7,1,0x12345,8,true};
    bool has_mapping=false;
    bool next(TraceRecord& r) override {if(index==records.size())return false;r=records[index++];return true;}
    std::uint64_t current_address_space_id() const override{return 1;}
    std::string description() const override{return "perf-cache-functional-fixture";}
    const VirtualPageMapping* virtual_page_mapping(std::uint32_t token)const override{
        return has_mapping && token==mapping.token?&mapping:nullptr;
    }
};
SimulatorConfig config(){SimulatorConfig c;c.cores=1;c.cha_count=1;c.core_model="interval_weave";
    c.interval_scheduler="time_epoch";c.chunk_instructions=8;c.interval_target_uops=8;
    c.interval_max_cycles=1024;c.lookahead_chunks=2;c.response_queue_feedback=true;
    c.response_sparse_scoreboard=true;c.l1d.size_bytes=4096;c.l2.size_bytes=16384;
    c.llc.size_bytes=65536;c.dram.size_bytes=64ull<<20;c.dram.channels=1;c.dram.banks_per_channel=1;
    return c;}
SimulationStats run(SimulatorConfig c,std::unique_ptr<Source> source){
    std::vector<std::unique_ptr<TraceSource>> traces;traces.push_back(std::move(source));
    Simulator sim(c,std::move(traces));return sim.run();}
TraceRecord data(std::uint64_t address,bool write=false){
    TraceRecord r;r.pc=0x1000;r.address=address;r.size=8;
    r.flags=kRetires|kPhysicalAddress|(write?kStore:kLoad);return r;
}
void non_power_of_two_cha(){
    auto c=config();c.cha_count=52;c.cha_xor_hash=false;
    c.llc.size_bytes=52ull*2048*15*64;c.llc.associativity=15;
    c.llc.replacement=ReplacementPolicy::kLru;c.validate();
    SetAssociativeCache llc(c.llc);
    constexpr std::uint64_t sets=52ull*2048;
    for(std::uint64_t line=0;line<sets;++line){
        const auto bank=line%52;
        const auto local_set=(line/52)%2048;
        const auto expected=bank+52*local_set;
        require(cache_set_index(line,sets)==expected &&
                llc.prepare_lookup(line).set==expected &&
                cache_set_index(line+sets,sets)==expected &&
                llc.prepare_lookup(line+sets).set==expected,
                "LLC conflict sets must match the 52-bank producer geometry");
    }
    require(cache_set_index(0,sets)==cache_set_index(sets,sets),
            "lines 0 and 106496 share the producer LLC set");
    // Hash both bank and set from the same transformed physical line. A
    // conflict must evict the original physical identity, including dirty
    // writeback and transaction rollback paths.
    SetAssociativeCache mixed_llc(c.llc, true);
    CacheCounters mixed_counts;
    std::uint64_t first_line = 0;
    for (std::uint64_t i=0;i<16;++i) {
        const auto mapped = 777 + i*sets;
        const auto physical = spr_llc_unmix_line(mapped);
        if (!i) first_line = physical;
        require(spr_llc_mix_line(physical)==mapped,
                "LLC line mix must be bijective");
        const auto expected = (mapped%52) + 52*((mapped/52)%2048);
        require(mixed_llc.prepare_lookup(physical).set==expected,
                "mixed LLC set must agree with 52 bank/2048 set mapping");
        const auto result = mixed_llc.access(physical, i==0, mixed_counts);
        if (i==15) require(result.evicted && result.evicted_dirty &&
                           result.evicted_line==first_line,
                           "mixed LLC eviction must return original dirty physical line");
    }
    const auto present = spr_llc_unmix_line(777+sets);
    auto tx = mixed_llc.begin_transaction();
    bool was_dirty = true;
    require(mixed_llc.invalidate(present, &was_dirty, &tx) && !was_dirty,
            "mixed LLC must find the original physical line to invalidate");
    mixed_llc.restore(tx);
    require(mixed_llc.contains(present), "mixed LLC rollback restores original identity");
    for(unsigned cha_count:{52u,8u})for(bool hashed:{false,true}){
        c.cha_count=cha_count;
        c.cha_xor_hash=hashed;
        auto source=std::make_unique<Source>();
        std::vector<std::uint64_t> expected(cha_count);
        for(std::uint64_t i=0;i<104;++i){
            const auto line=0x10000+i;
            source->records.push_back(data(line*64));
            const auto mixed=hashed ? line^(line>>11)^(line>>23) : line;
            ++expected[mixed%cha_count];
        }
        const auto stats=run(c,std::move(source));
        require(stats.cha.size()==cha_count,"configured CHA counters must exist");
        for(std::size_t i=0;i<cha_count;++i)
            require(stats.cha[i].requests==expected[i],
                    "actual shared requests must reach the expected CHA");
    }
    c.cha_count=0;
    bool rejected=false;
    try{c.validate();}catch(const std::invalid_argument&){rejected=true;}
    require(rejected,"zero CHA count must be rejected");
}
SimulationStats run_pair(SimulatorConfig c,std::vector<TraceRecord> a,
                         std::vector<TraceRecord> b){
    c.cores=2;std::vector<std::unique_ptr<TraceSource>> traces;
    for(auto records:{a,b}){auto source=std::make_unique<Source>();
        source->records=std::move(records);traces.push_back(std::move(source));}
    Simulator sim(c,std::move(traces));return sim.run();
}
void native_l2_mechanisms(){
    auto c=config();c.interval_private_preview=false;
    std::vector<TraceRecord> delayed_reader;
    for(unsigned i=0;i<128;++i){TraceRecord r;r.pc=0x3000+4*i;r.flags=kRetires;delayed_reader.push_back(r);}
    delayed_reader.push_back(data(0x8000));
    const auto remote=run_pair(c,{data(0x8000,true)},delayed_reader);
    require(remote.cha.at(0).remote_supplies>0,
            "remote fixture must execute a remote supply");
    require(remote.total_core().l2_data_demand_tag_misses==2,
            "both cold private L2 lookups miss even when final supplier is remote");
    require(remote.cores[1].l2_data_demand_tag_misses==1 &&
            remote.cores[1].l2_read_tag_misses==0,
            "remote miss is distinct from historical read-only final-path bucket");
    const auto merge=run_pair(c,{data(0x9000)},{data(0x9000)});
    require(merge.cha.at(0).llc_merged_misses>0,
            "cross-core fixture must merge at LLC while both private tags miss");
    require(merge.total_core().l2_data_demand_tag_misses==2,
            "LLC merge must not erase either local private L2 tag miss");
    std::vector<TraceRecord> writer{data(0xa000)};
    for(unsigned i=0;i<128;++i){TraceRecord r;r.pc=0x2000+4*i;r.flags=kRetires;writer.push_back(r);}
    writer.push_back(data(0xa000,true));
    const auto upgrade=run_pair(c,writer,{data(0xa000)});
    require(upgrade.cha.at(0).upgrades>0,
            "shared read then store fixture must exercise a permission upgrade");
    require(upgrade.total_core().l2_data_demand_tag_misses==2,
            "permission upgrade of resident private line must not be a tag miss");
    c.ruby_sequencer_max_outstanding=16;c.ruby_sequencer_line_coalescing=true;
    auto source=std::make_unique<Source>();source->records={data(0xb000),data(0xb008)};
    const auto aliases=run(c,std::move(source));
    require(aliases.total_sequencer().coalesced_requests>0,
            "same-core fixture must execute an actual sequencer alias");
    require(aliases.total_core().l2_data_demand_tag_misses==1,
            "sequencer aliases issue only one private-L2 lookup");
}
}
int main(){try{
    auto c=config();c.fetch_supply_model=true;c.l1i_enabled=true;
    c.fetch_supply_physical_request_ledger=true;c.fetch_supply_lower_hierarchy=true;
    c.instruction_address_mode="modeled";c.instruction_physical_address_bits=38;
    c.instruction_mapping_seed=23;c.fetch_buffer_bytes=64;c.l1i.size_bytes=64;
    c.l1i.associativity=1;c.l1i_miss_penalty=0;c.l2.size_bytes=64;c.l2.associativity=1;
    auto code=std::make_unique<Source>();
    for(auto pc:{0x1000ull,0x1040ull,0x1080ull,0x1000ull}){
        TraceRecord r;r.pc=pc;r.flags=kRetires;code->records.push_back(r);
    }
    const auto fetch=run(c,std::move(code));const auto f=fetch.total_core();
    require(f.perf_cache.physical.code_reads==4 && f.perf_cache.physical.l2_demand_misses==4,
            "actual I-side L2 lookups must count as demand misses");
    require(f.perf_cache.physical.llc_misses==3 &&
            f.perf_retirement.physical.perf_retired_loads==0,
            "code requests retain independent LLC lookup and never become retired loads");
    c=config();c.ruby_sequencer_max_outstanding=16;c.dtlb.enabled=true;c.dtlb.entries=64;c.dtlb.hit_latency=0;
    c.dtlb.miss_model="timing_walk";c.dtlb.page_walkers=1;c.dtlb.coalesce_misses=false;
    c.dtlb.page_walk_levels=4;c.dtlb.page_walk_address_mode="physical_sidecar";
    c.dtlb.page_walk_latency=c.dtlb.page_walk_levels*c.l1d.hit_latency+c.dtlb.page_walk_restart_latency;
    c.dtlb.hierarchy_walk=true;
    auto pte=std::make_unique<Source>();pte->has_mapping=true;
    pte->mapping.initial_page_table_path.valid=true;pte->mapping.initial_page_table_path.levels=4;
    pte->mapping.initial_page_table_path.page_size_bits=12;
    const std::uint64_t addresses[]{0x1008,0x2040,0x3180,0x4a28};
    for(unsigned i=0;i<4;++i)pte->mapping.initial_page_table_path.pte_physical_addresses[i]=addresses[i];
    pte->mapping.roi_entry_page_table_path=pte->mapping.initial_page_table_path;
    TraceRecord load;load.pc=0x1000;load.address=0x8000;load.size=8;load.reserved=7;
    load.flags=kRetires|kLoad|kPhysicalAddress|kVirtualPageToken;pte->records.push_back(load);
    const auto walk=run(c,std::move(pte)).total_core();
    require(walk.dtlb_hierarchy_requests==4 && walk.perf_cache.physical.page_table_requests==4,
            "actual PTE requests observed on timing-walk replay");
    require(walk.perf_cache.physical.l2_demand_misses==1 && walk.perf_cache.physical.data_reads==1,
            "unknown target PTE eligibility cannot be silently counted as a data-read event");
    require(walk.l2_data_demand_tag_misses==1,
            "native private-L2 data population excludes all PTE traffic");
    require(f.l2_data_demand_tag_misses==0,
            "native private-L2 data population excludes all code traffic");
    native_l2_mechanisms();
    non_power_of_two_cha();
    std::cout<<"perf cache integration tests passed\n";
}catch(const std::exception& e){std::cerr<<e.what()<<'\n';return 1;}}
